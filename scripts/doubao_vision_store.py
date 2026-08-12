from __future__ import annotations

import argparse
import atexit
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.json"
DATABASE_PATH = BASE_DIR / "output" / "knowledge_base.db"

_CLIENT: Any = None
_CLIENT_LOCK = threading.Lock()
_DLL_HANDLES: list[Any] = []


def _enable_local_packages() -> None:
    paths = [
        BASE_DIR / "tools" / "python_packages",
        BASE_DIR / "tools" / "python_packages" / "win32",
        BASE_DIR / "tools" / "python_packages" / "win32" / "lib",
    ]
    for path in reversed(paths):
        value = str(path)
        if path.exists() and value not in sys.path:
            sys.path.insert(0, value)
    dll_path = BASE_DIR / "tools" / "python_packages" / "pywin32_system32"
    if dll_path.exists() and hasattr(os, "add_dll_directory") and not _DLL_HANDLES:
        _DLL_HANDLES.append(os.add_dll_directory(str(dll_path)))


def _config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _load_local_env() -> None:
    """Load ignored .env values without printing or persisting secrets."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def vision_config() -> dict[str, Any]:
    _load_local_env()
    configured = _config().get("multimodal_retrieval", {})
    base_url = str(
        os.getenv(
            "DOUBAO_EMBEDDING_URL",
            configured.get(
                "embedding_url",
                "https://ark.cn-beijing.volces.com/api/plan/v3/embeddings/multimodal",
            ),
        )
    ).rstrip("/")
    return {
        "path": str(
            (
                BASE_DIR
                / os.getenv(
                    "DOUBAO_VISION_QDRANT_PATH",
                    configured.get("qdrant_path", "output/qdrant_doubao_vision"),
                )
            ).resolve()
        ),
        "collection": str(
            os.getenv(
                "DOUBAO_VISION_QDRANT_COLLECTION",
                configured.get(
                    "qdrant_collection", "truck_knowledge_chunks_doubao_vision"
                ),
            )
        ),
        "model": str(
            os.getenv(
                "DOUBAO_EMBEDDING_MODEL",
                configured.get("embedding_model", "doubao-embedding-vision"),
            )
        ),
        "dimensions": int(
            os.getenv(
                "DOUBAO_EMBEDDING_DIMENSIONS",
                configured.get("embedding_dimensions", 2048),
            )
        ),
        "url": base_url,
        "api_key": os.getenv("DOUBAO_EMBEDDING_API_KEY", "").strip()
        or os.getenv("ARK_API_KEY", "").strip(),
        "semantic_limit": int(
            os.getenv(
                "DOUBAO_VISION_SEMANTIC_LIMIT",
                configured.get("semantic_limit", 80),
            )
        ),
    }


def _client() -> Any:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _enable_local_packages()
            from qdrant_client import QdrantClient

            config = vision_config()
            Path(config["path"]).mkdir(parents=True, exist_ok=True)
            _CLIENT = QdrantClient(path=config["path"])
    return _CLIENT


def close_runtime() -> None:
    global _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        finally:
            _CLIENT = None


atexit.register(close_runtime)


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"smartlink-kb:doubao-vision:{chunk_id}"))


def _batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _parse_vector(payload: dict[str, Any]) -> list[float]:
    data = payload.get("data") or []
    if not data:
        raise RuntimeError("豆包向量接口返回空数据")
    if isinstance(data, dict):
        vector = data.get("embedding")
    else:
        vector = data[0].get("embedding")
    if isinstance(vector, list) and vector and isinstance(vector[0], list):
        vector = vector[0]
    if not isinstance(vector, list) or not vector:
        raise RuntimeError("豆包向量接口未返回有效 embedding")
    return [float(value) for value in vector]


def embed_text(text: str, *, timeout: int = 60) -> list[float]:
    config = vision_config()
    if not config["api_key"]:
        raise RuntimeError("未配置 DOUBAO_EMBEDDING_API_KEY 或 ARK_API_KEY")
    request_body = {
        "model": config["model"],
        "input": [{"type": "text", "text": text[:12000]}],
    }
    request = Request(
        config["url"],
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            vector = _parse_vector(payload)
            expected = int(config["dimensions"])
            if expected and len(vector) != expected:
                raise RuntimeError(
                    f"豆包向量维度变化：配置为 {expected}，接口返回 {len(vector)}"
                )
            return vector
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(f"豆包向量接口 HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(
                f"豆包向量接口网络错误：{type(exc).__name__}: {exc}"
            ) from exc
    raise RuntimeError("豆包向量接口重试失败")


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "chunk_id": str(row["id"]),
        "document_id": str(row["document_id"]),
        "source_locator": str(row["source_locator"]),
        "vehicle_tags": [item for item in str(row["vehicle_tags"]).split(",") if item],
        "scene": str(row["scene"]),
        "energy_tags": [item for item in str(row["energy_tags"]).split(",") if item],
        "relative_path": str(row["relative_path"]),
        "file_name": str(row["file_name"]),
        "vector_provider": "doubao-embedding-vision",
    }


def _existing_chunk_ids(client: Any, collection: str) -> set[str]:
    existing: set[str] = set()
    if not client.collection_exists(collection):
        return existing
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=256,
            offset=offset,
            with_payload=["chunk_id"],
            with_vectors=False,
        )
        existing.update(
            str((point.payload or {}).get("chunk_id", ""))
            for point in points
            if (point.payload or {}).get("chunk_id")
        )
        if offset is None:
            return existing


def build_index(
    database_path: Path = DATABASE_PATH,
    *,
    batch_size: int = 16,
    workers: int = 4,
    limit: int = 0,
    timeout: int = 60,
) -> dict[str, Any]:
    if not database_path.exists():
        raise FileNotFoundError(f"知识库数据库不存在：{database_path}")
    task_db_path = database_path.parent / "task_index.db"
    if not task_db_path.exists():
        raise FileNotFoundError(f"任务索引数据库不存在：{task_db_path}，请先运行 build_task_index.py")
    config = vision_config()
    _enable_local_packages()
    from qdrant_client import QdrantClient
    from qdrant_client.http import models

    store_path = Path(config["path"])
    store_path.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(store_path))
    collection = config["collection"]
    recreated = False
    if client.collection_exists(collection):
        info = client.get_collection(collection)
        vector_config = info.config.params.vectors
        actual_size = getattr(vector_config, "size", None)
        if actual_size != config["dimensions"]:
            raise RuntimeError(
                f"镜像集合 {collection} 维度为 {actual_size}，预期 {config['dimensions']}；"
                "为避免误删，请手动确认后删除该集合再重建。"
            )
    else:
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(
                size=config["dimensions"], distance=models.Distance.COSINE
            ),
            hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
        )
        recreated = True

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        # 排除故障码和电路图：这两类走精确匹配/视觉向量，不需要文字语义向量
        connection.execute(
            f"ATTACH DATABASE '{task_db_path}' AS task_db"
        )
        rows = connection.execute(
            """
            SELECT c.id, c.document_id, c.content, c.source_locator,
                   c.vehicle_tags, c.scene, c.energy_tags,
                   d.relative_path, d.file_name
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            LEFT JOIN task_db.chunk_categories cat ON cat.chunk_id = c.id
            WHERE d.enabled = 1
              AND (cat.primary_task IS NULL
                   OR cat.primary_task NOT IN ('fault_code', 'drawing'))
            ORDER BY c.id
            """
        ).fetchall()

    existing = _existing_chunk_ids(client, collection)
    rows_to_index = [row for row in rows if str(row["id"]) not in existing]
    if limit > 0:
        rows_to_index = rows_to_index[:limit]

    started = time.perf_counter()
    indexed = 0
    failed = 0
    errors: list[dict[str, str]] = []
    worker_count = max(1, min(int(workers), 16))
    request_timeout = max(10, int(timeout))

    def make_point(row: sqlite3.Row) -> tuple[sqlite3.Row, list[float]]:
        return row, embed_text(str(row["content"]), timeout=request_timeout)

    from qdrant_client.http.models import PointStruct

    for batch in _batches(list(rows_to_index), max(1, int(batch_size))):
        points: list[PointStruct] = []
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {pool.submit(make_point, row): row for row in batch}
            for future in as_completed(future_map):
                row = future_map[future]
                try:
                    _, vector = future.result()
                    points.append(
                        PointStruct(
                            id=point_id(str(row["id"])),
                            vector=vector,
                            payload=_payload(row),
                        )
                    )
                except Exception as exc:
                    failed += 1
                    errors.append(
                        {
                            "chunk_id": str(row["id"]),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        if points:
            client.upsert(collection_name=collection, points=points, wait=True)
            indexed += len(points)
        print(
            f"Doubao 镜像向量化：已写入 {indexed}/{len(rows_to_index)}，失败 {failed}",
            flush=True,
        )

    elapsed = round(time.perf_counter() - started, 2)
    report = {
        "backend": "doubao_embedding_vision",
        "collection": collection,
        "embedding_model": config["model"],
        "dimensions": config["dimensions"],
        "points": len(existing) + indexed,
        "source_rows": len(rows),
        "added": indexed,
        "failed": failed,
        "recreated": recreated,
        "seconds": elapsed,
        "path": str(store_path),
        "errors": errors[:100],
    }
    (BASE_DIR / "output" / "qdrant_doubao_vision_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    client.close()
    return report


def _task_filter_is_ready(config: dict[str, Any]) -> bool:
    marker = BASE_DIR / "output" / "task_filter_ready.json"
    if not marker.exists():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        key = f"{Path(config['path']).resolve()}|{config['collection']}"
        return bool(payload.get("collections", {}).get(key, {}).get("ready"))
    except Exception:
        return False


def semantic_search(
    question: str, limit: int | None = None, task_type: str = ""
) -> list[dict[str, Any]]:
    config = vision_config()
    client = _client()
    collection = config["collection"]
    if not client.collection_exists(collection):
        return []
    vector = embed_text(question)
    query_filter = None
    if task_type and task_type != "general" and _task_filter_is_ready(config):
        _enable_local_packages()
        from qdrant_client.http import models

        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="task_type", match=models.MatchValue(value=task_type)
                )
            ]
        )
    response = client.query_points(
        collection_name=collection,
        query=vector,
        limit=max(1, min(int(limit or config["semantic_limit"]), 500)),
        query_filter=query_filter,
        with_payload=True,
        with_vectors=False,
    )
    results: list[dict[str, Any]] = []
    for point in response.points:
        payload = dict(point.payload or {})
        chunk_id = str(payload.get("chunk_id", ""))
        if chunk_id:
            results.append(
                {
                    "chunk_id": chunk_id,
                    "score": float(point.score),
                    "payload": payload,
                }
            )
    return results


def status() -> dict[str, Any]:
    config = vision_config()
    try:
        client = _client()
        if not client.collection_exists(config["collection"]):
            return {"ready": False, **{k: v for k, v in config.items() if k != "api_key"}, "points": 0}
        info = client.get_collection(config["collection"])
        return {
            "ready": True,
            **{k: v for k, v in config.items() if k != "api_key"},
            "points": int(info.points_count or 0),
        }
    except Exception as exc:
        return {
            "ready": False,
            **{k: v for k, v in config.items() if k != "api_key"},
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="构建独立的豆包多模态镜像向量库")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    report = build_index(
        args.database.resolve(),
        batch_size=args.batch_size,
        workers=args.workers,
        limit=args.limit,
        timeout=args.timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
