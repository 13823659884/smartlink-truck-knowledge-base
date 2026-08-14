"""单文档增量导入：上传 → 解析 → 切片 → SQLite → 豆包向量 → 任务分类。

复用离线构建流水线（build_kb / doubao_vision_store / build_task_index）的
解析与索引逻辑，在服务进程内完成单份文档的增量入库，导入后立即可检索。

说明：
- 向量化写入服务进程已打开的 Qdrant 本地库（doubao_vision_store._client），
  避免独立进程导致的存储目录锁冲突。
- 同名文档按 build_kb 的版本管理规则处理：同 logical_key 的旧启用文档会被
  置为停用（enabled=0）并删除其向量，新文档成为当前版本。
- 知识图谱（entities/triples）不在导入范围内，仍由离线 build_kb.py 构建。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import build_kb
import build_task_index
import doubao_vision_store

BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "output" / "knowledge_base.db"
TASK_DATABASE_PATH = BASE_DIR / "output" / "task_index.db"
IMPORT_SUBDIR = "导入"
MAX_UPLOAD_BYTES = 10_000_000
MAX_UPLOAD_CHUNKS = 4000


class DocumentImportError(ValueError):
    """导入失败，携带面向用户的错误说明。"""


def import_document(
    upload_bytes: bytes,
    file_name: str,
    scene: str = "",
    config: dict[str, object] | None = None,
) -> dict[str, Any]:
    """导入一份文档并返回处理报告。

    scene 为空时按路径规则自动识别；显式传入则使用传入值（需在配置
    scenes 取值集合内，或为“其他”）。
    """
    if config is None:
        config = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    started = time.perf_counter()
    steps: list[str] = []
    steps.append("校验文件")
    if not upload_bytes:
        raise DocumentImportError("上传内容为空")
    if len(upload_bytes) > MAX_UPLOAD_BYTES:
        raise DocumentImportError("单个文档不能超过10MB")
    file_name = Path(file_name).name.strip()
    if not file_name or file_name.startswith("~$"):
        raise DocumentImportError("文件名无效")
    extension = Path(file_name).suffix.lower()
    if extension not in build_kb.SUPPORTED_EXTENSIONS:
        raise DocumentImportError(
            "不支持的文档格式，支持："
            + "、".join(sorted(build_kb.SUPPORTED_EXTENSIONS))
        )
    allowed_scenes = set(config.get("scenes", {}).values()) | {"其他"}
    if scene and scene not in allowed_scenes:
        raise DocumentImportError(f"场景参数无效：{scene}")

    steps.append("保存文件")
    source_root = (BASE_DIR / str(config["source_root"])).resolve()
    import_dir = source_root / IMPORT_SUBDIR
    import_dir.mkdir(parents=True, exist_ok=True)
    target_path = _resolve_conflict_name(import_dir, file_name)
    target_path.write_bytes(upload_bytes)
    sha256 = build_kb.sha256_file(target_path)
    relative_path = target_path.relative_to(source_root)

    steps.append("解析文档")
    ocr_config = dict(config.get("ocr", {}))
    ocr_runtime = build_kb.ocr_status()
    ocr_config["runtime_available"] = bool(ocr_runtime.get("available"))
    extraction_cache_dir = BASE_DIR / str(config["output_dir"]) / "extraction_cache"
    try:
        (
            units,
            _cache_hit,
            ocr_used,
            ocr_processed,
            ocr_pages,
        ) = build_kb.cached_extract_units(
            target_path,
            sha256,
            extraction_cache_dir,
            ocr_config,
        )
    except Exception as exc:
        target_path.unlink(missing_ok=True)
        raise DocumentImportError(f"文档解析失败：{type(exc).__name__}: {exc}") from exc
    if not units:
        target_path.unlink(missing_ok=True)
        raise DocumentImportError(
            "未提取到可检索文本，可能是扫描件、图纸或图片型文档"
            + ("（已完成OCR但识别文字过少）" if ocr_processed else "")
        )

    steps.append("切片与标签")
    chunk_size = int(config["chunk_size"])
    chunk_overlap = int(config["chunk_overlap"])
    if scene:
        resolved_scene = scene
    else:
        resolved_scene = build_kb.detect_scene(relative_path, config["scenes"])
    preview_text = "\n".join(text for _, text in units)[:20000]
    text_vehicle_tags, text_energy_tags = build_kb.detect_tags(
        preview_text, config["vehicle_series"]
    )
    path_vehicle_tags, path_energy_tags = build_kb.detect_tags(
        relative_path.as_posix(), config["vehicle_series"]
    )
    vehicle_tags = sorted(set(path_vehicle_tags) | set(text_vehicle_tags))
    energy_tags = sorted(set(path_energy_tags) | set(text_energy_tags))

    document_id = build_kb.stable_id("document", relative_path.as_posix())
    document_chunks: list[dict[str, Any]] = []
    ordinal = 0
    for locator, text in units:
        for part in build_kb.split_text(text, chunk_size, chunk_overlap):
            if len(part) < (4 if ocr_used else 20):
                continue
            ordinal += 1
            if ordinal > MAX_UPLOAD_CHUNKS:
                target_path.unlink(missing_ok=True)
                raise DocumentImportError(f"切片数量超过上限（{MAX_UPLOAD_CHUNKS}）")
            chunk_id = build_kb.stable_id(
                "chunk", f"{relative_path.as_posix()}|{ordinal}|{part}"
            )
            tokens = build_kb.search_tokens(
                f"{relative_path.as_posix()} {part}"
            )
            document_chunks.append(
                {
                    "id": chunk_id,
                    "document_id": document_id,
                    "ordinal": ordinal,
                    "source_locator": locator,
                    "content": part,
                    "search_terms": " ".join(tokens[:1800]),
                    "vehicle_tags": ",".join(vehicle_tags),
                    "scene": resolved_scene,
                    "energy_tags": ",".join(energy_tags),
                    "token_count": len(tokens),
                }
            )
    if not document_chunks:
        target_path.unlink(missing_ok=True)
        raise DocumentImportError("解析结果为空，未形成有效检索分块")

    steps.append("写入知识库")
    status = (
        "parsed_ocr"
        if ocr_used
        else "parsed_legacy"
        if extension in {".doc", ".xls"}
        else "parsed"
    )
    logical_key = build_kb.logical_key(relative_path)
    version = build_kb.parse_version(file_name)[0]
    effective_date = build_kb.parse_date(file_name)[0]
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 版本管理：同 logical_key 的旧启用文档停用，其向量随后删除。
        superseded = connection.execute(
            """
            SELECT id FROM documents
            WHERE logical_key = ? AND enabled = 1
            """,
            (logical_key,),
        ).fetchall()
        superseded_ids = [str(row["id"]) for row in superseded]
        if superseded_ids:
            connection.execute(
                """
                UPDATE documents SET enabled = 0
                WHERE id IN (%s)
                """ % ",".join("?" * len(superseded_ids)),
                superseded_ids,
            )
        # 若目标 relative_path 已存在（重复导入同一文件），先清理旧记录。
        existing = connection.execute(
            "SELECT id FROM documents WHERE relative_path = ?",
            (relative_path.as_posix(),),
        ).fetchone()
        if existing:
            existing_id = str(existing["id"])
            if existing_id in superseded_ids:
                superseded_ids.remove(existing_id)
            connection.execute(
                "DELETE FROM chunks WHERE document_id = ?", (existing_id,)
            )
            connection.execute(
                "DELETE FROM documents WHERE id = ?", (existing_id,)
            )
        connection.execute(
            """
            INSERT INTO documents
            (id, relative_path, file_name, extension, sha256, size_bytes,
             modified_at, logical_key, version, effective_date, scene,
             vehicle_tags, energy_tags, status, enabled, chunk_count,
             error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                relative_path.as_posix(),
                target_path.name,
                extension,
                sha256,
                len(upload_bytes),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                logical_key,
                version,
                effective_date,
                resolved_scene,
                ",".join(vehicle_tags),
                ",".join(energy_tags),
                status,
                1,
                len(document_chunks),
                "",
            ),
        )
        _insert_chunk_vectors(connection, document_chunks)
        for chunk in document_chunks:
            connection.execute(
                """
                INSERT INTO chunks
                (id, document_id, ordinal, source_locator, content, search_terms,
                 vehicle_tags, scene, energy_tags, token_count)
                VALUES (:id, :document_id, :ordinal, :source_locator, :content,
                        :search_terms, :vehicle_tags, :scene, :energy_tags,
                        :token_count)
                """,
                chunk,
            )
            connection.execute(
                "INSERT INTO chunks_fts(chunk_id, search_terms, content) VALUES (?, ?, ?)",
                (chunk["id"], chunk["search_terms"], chunk["content"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    steps.append("任务分类")
    task_stats = _index_task_categories(
        document_chunks,
        relative_path.as_posix(),
        document_id,
    )
    chunk_tasks = task_stats.pop("chunk_tasks", {})

    steps.append("向量化")
    client = doubao_vision_store._client()
    vector_config = doubao_vision_store.vision_config()
    collection = vector_config["collection"]
    if not client.collection_exists(collection):
        raise DocumentImportError(
            "向量集合不存在，请先运行向量化构建（doubao_vision_store.build_index）"
        )
    # 停用版本与重复导入的旧点需要从向量库移除，避免检索命中旧内容。
    for stale_document_id in superseded_ids:
        try:
            _delete_document_points(client, collection, stale_document_id)
        except Exception as exc:
            raise DocumentImportError(
                f"清理旧版本文档向量失败：{type(exc).__name__}: {exc}"
            ) from exc
    from qdrant_client.http.models import PointStruct

    indexed = 0
    failed = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    for start in range(0, len(document_chunks), 16):
        batch = document_chunks[start : start + 16]
        points: list[Any] = []
        for chunk in batch:
            # 与离线构建 doubao_vision_store.build_index 一致：故障码和图纸类
            # 切片走精确匹配/视觉向量，不写入文字语义向量库。
            if chunk_tasks.get(str(chunk["id"]), "") in {"fault_code", "drawing"}:
                skipped += 1
                continue
            try:
                vector = doubao_vision_store.embed_text(
                    str(chunk["content"])
                )
            except Exception as exc:
                failed += 1
                errors.append(
                    {
                        "chunk_id": str(chunk["id"]),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            points.append(
                PointStruct(
                    id=doubao_vision_store.point_id(str(chunk["id"])),
                    vector=vector,
                    payload=doubao_vision_store._payload(
                        {
                            "id": chunk["id"],
                            "document_id": chunk["document_id"],
                            "source_locator": chunk["source_locator"],
                            "vehicle_tags": chunk["vehicle_tags"],
                            "scene": chunk["scene"],
                            "energy_tags": chunk["energy_tags"],
                            "relative_path": relative_path.as_posix(),
                            "file_name": target_path.name,
                        }
                    ),
                )
            )
        if points:
            client.upsert(collection_name=collection, points=points, wait=True)
            indexed += len(points)
    if failed and indexed == 0 and not skipped:
        target_path.unlink(missing_ok=True)
        raise DocumentImportError("全部切片向量化失败，请检查豆包向量接口配置")

    elapsed = round(time.perf_counter() - started, 2)
    return {
        "ok": True,
        "document_id": document_id,
        "file_name": target_path.name,
        "relative_path": relative_path.as_posix(),
        "scene": resolved_scene,
        "vehicle_tags": vehicle_tags,
        "energy_tags": energy_tags,
        "chunks": len(document_chunks),
        "vectorized": indexed,
        "vector_failed": failed,
        "vector_skipped": skipped,
        "ocr_used": ocr_used,
        "ocr_pages": ocr_pages,
        "status": status,
        "superseded": len(superseded_ids),
        "task": task_stats,
        "steps": steps,
        "elapsed_seconds": elapsed,
        "errors": errors[:10],
    }


def _resolve_conflict_name(directory: Path, file_name: str) -> Path:
    """同目录下文件名冲突时追加序号后缀，保证 relative_path 唯一。"""
    candidate = directory / file_name
    if not candidate.exists():
        return candidate
    stem = Path(file_name).stem
    extension = Path(file_name).suffix
    for index in range(2, 1000):
        candidate = directory / f"{stem}({index}){extension}"
        if not candidate.exists():
            return candidate
    raise DocumentImportError("同名文件过多，请清理导入目录后重试")


def _insert_chunk_vectors(
    connection: sqlite3.Connection, document_chunks: list[dict[str, Any]]
) -> None:
    """写入词频向量（chunk_vectors），供 sqlite_hybrid 后端兜底检索使用。

    IDF 沿用离线构建时的全局统计（vector_meta 表）；该表缺失时跳过，
    不影响豆包向量后端（qdrant_hybrid）的语义检索。
    """
    try:
        meta = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT key, value_json FROM vector_meta"
            ).fetchall()
        }
        if "idf" not in meta:
            return
        idf = json.loads(meta["idf"])
        dimensions = int(json.loads(meta["dimensions"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return
    for chunk in document_chunks:
        counts = build_kb.vector_counts(str(chunk["content"]), dimensions)
        vector = build_kb.normalize_vector(counts, idf)
        connection.execute(
            "INSERT INTO chunk_vectors(chunk_id, vector_json) VALUES (?, ?)",
            (
                chunk["id"],
                json.dumps(vector, separators=(",", ":")),
            ),
        )


def _delete_document_points(
    client: Any, collection: str, document_id: str
) -> None:
    """按 payload 中的 document_id 删除该文档在向量库中的全部点。"""
    from qdrant_client.http import models

    client.delete(
        collection_name=collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="document_id",
                        match=models.MatchValue(value=document_id),
                    )
                ]
            )
        ),
    )


def _index_task_categories(
    document_chunks: list[dict[str, Any]],
    relative_path: str,
    document_id: str,
) -> dict[str, Any]:
    """把新切片的任务分类写入 task_index.db（增量，不影响已有记录）。

    返回统计信息，其中 chunk_tasks 为「切片ID → 主任务类型」映射，
    供向量化阶段按离线构建规则过滤 fault_code / drawing 类切片。
    """
    task_connection = sqlite3.connect(TASK_DATABASE_PATH)
    try:
        build_task_index.create_schema(task_connection)
        votes: Counter[str] = Counter()
        fault_count = 0
        chunk_tasks: dict[str, str] = {}
        for chunk in document_chunks:
            primary, secondary, confidence, reasons = build_task_index.classify(
                relative_path, str(chunk["content"])
            )
            votes[primary] += 1
            chunk_tasks[str(chunk["id"])] = primary
            task_connection.execute(
                """
                INSERT OR REPLACE INTO chunk_categories
                (chunk_id, document_id, primary_task, secondary_tasks_json,
                 confidence, reasons_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk["id"], document_id, primary,
                    json.dumps(secondary, ensure_ascii=False), confidence,
                    json.dumps(reasons, ensure_ascii=False),
                ),
            )
            if primary == "fault_code" or "fault_code" in secondary:
                for entry in build_task_index.extract_spn_entries(
                    str(chunk["content"])
                ):
                    identity = "|".join(
                        [
                            str(entry["spn"]), str(entry["fmi"]),
                            str(entry["description_zh"]), relative_path,
                            str(chunk["source_locator"]),
                        ]
                    )
                    entry_id = build_task_index.hashlib.sha1(
                        identity.encode("utf-8")
                    ).hexdigest()
                    excerpt = str(chunk["content"])[:2200]
                    task_connection.execute(
                        """
                        INSERT OR IGNORE INTO fault_code_entries
                        (id, spn, fmi, controller_code, description_zh,
                         description_en, severity, chunk_id, relative_path,
                         source_locator, vehicle_tags, raw_excerpt)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry_id, entry["spn"], entry["fmi"],
                            entry["controller_code"], entry["description_zh"],
                            entry["description_en"], entry["severity"],
                            chunk["id"], relative_path,
                            chunk["source_locator"], chunk["vehicle_tags"], excerpt,
                        ),
                    )
                    if task_connection.execute("SELECT changes()").fetchone()[0]:
                        task_connection.execute(
                            "INSERT INTO fault_code_fts VALUES (?, ?, ?, ?)",
                            (
                                entry_id, entry["description_zh"],
                                entry["description_en"], excerpt,
                            ),
                        )
                        fault_count += 1
        if votes:
            primary, count = votes.most_common(1)[0]
            secondary = [task for task, _ in votes.most_common()[1:4]]
            task_connection.execute(
                """
                INSERT OR REPLACE INTO document_categories
                (document_id, primary_task, secondary_tasks_json,
                 confidence, relative_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    document_id, primary,
                    json.dumps(secondary, ensure_ascii=False),
                    round(count / max(1, sum(votes.values())), 3),
                    relative_path,
                ),
            )
        task_connection.commit()
        return {
            "primary_task": primary if votes else "general",
            "task_label": build_task_index.TASK_LABELS.get(
                primary if votes else "general", "通用资料"
            ),
            "chunks": len(document_chunks),
            "fault_code_entries": fault_count,
            "chunk_tasks": chunk_tasks,
        }
    finally:
        task_connection.close()
