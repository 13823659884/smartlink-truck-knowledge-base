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
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parents[1]
LOCAL_PACKAGES = BASE_DIR / "tools" / "python_packages"
CONFIG_PATH = BASE_DIR / "config.json"
DATABASE_PATH = BASE_DIR / "output" / "knowledge_base.db"

_MODEL: Any = None
_CLIENT: Any = None
_RUNTIME_LOCK = threading.Lock()
_DLL_HANDLES: list[Any] = []


def _enable_local_packages() -> None:
    paths = [
        LOCAL_PACKAGES,
        LOCAL_PACKAGES / "win32",
        LOCAL_PACKAGES / "win32" / "lib",
    ]
    for path in reversed(paths):
        value = str(path)
        if path.exists() and value not in sys.path:
            sys.path.insert(0, value)
    dll_path = LOCAL_PACKAGES / "pywin32_system32"
    if dll_path.exists() and hasattr(os, "add_dll_directory"):
        if not _DLL_HANDLES:
            _DLL_HANDLES.append(os.add_dll_directory(str(dll_path)))


def _config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def qdrant_config() -> dict[str, Any]:
    retrieval = _config().get("retrieval", {})
    return {
        "path": str(
            (BASE_DIR / retrieval.get("qdrant_path", "output/qdrant")).resolve()
        ),
        "collection": str(
            retrieval.get("qdrant_collection", "truck_knowledge_chunks")
        ),
        "model": str(
            retrieval.get("embedding_model", "BAAI/bge-small-zh-v1.5")
        ),
        "dimensions": int(retrieval.get("embedding_dimensions", 512)),
        "semantic_limit": int(retrieval.get("semantic_limit", 80)),
        "cache_dir": str((BASE_DIR / "output" / "models").resolve()),
    }


def _model() -> Any:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _RUNTIME_LOCK:
        if _MODEL is None:
            _enable_local_packages()
            from fastembed import TextEmbedding

            config = qdrant_config()
            _MODEL = TextEmbedding(
                model_name=config["model"], cache_dir=config["cache_dir"]
            )
    return _MODEL


def _client() -> Any:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _RUNTIME_LOCK:
        if _CLIENT is None:
            _enable_local_packages()
            from qdrant_client import QdrantClient

            config = qdrant_config()
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
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"smartlink-kb:{chunk_id}"))


def _batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def build_qdrant_index(
    database_path: Path = DATABASE_PATH, *, batch_size: int = 96
) -> dict[str, Any]:
    if not database_path.exists():
        raise FileNotFoundError(f"知识库数据库不存在：{database_path}")

    _enable_local_packages()
    from qdrant_client import QdrantClient
    from qdrant_client.http import models

    config = qdrant_config()
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
            client.delete_collection(collection)
            recreated = True
    if not client.collection_exists(collection):
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
        rows = connection.execute(
            """
            SELECT c.id, c.document_id, c.content, c.source_locator,
                   c.vehicle_tags, c.scene, c.energy_tags,
                   d.relative_path, d.file_name
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.enabled = 1
            ORDER BY c.id
            """
        ).fetchall()

    existing_chunk_ids: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=256,
            offset=offset,
            with_payload=["chunk_id"],
            with_vectors=False,
        )
        existing_chunk_ids.update(
            str((point.payload or {}).get("chunk_id", ""))
            for point in points
            if (point.payload or {}).get("chunk_id")
        )
        if offset is None:
            break

    current_chunk_ids = {str(row["id"]) for row in rows}
    stale_chunk_ids = existing_chunk_ids - current_chunk_ids
    if stale_chunk_ids:
        client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(
                points=[point_id(item) for item in sorted(stale_chunk_ids)]
            ),
            wait=True,
        )
    rows_to_index = [
        row for row in rows if str(row["id"]) not in existing_chunk_ids
    ]

    started = time.perf_counter()
    embedding_model = _model() if rows_to_index else None
    indexed = 0
    for batch in _batches(list(rows_to_index), max(8, int(batch_size))):
        texts = [str(row["content"]) for row in batch]
        vectors = list(
            embedding_model.passage_embed(
                texts, batch_size=min(32, len(batch))
            )
        )
        points = []
        for row, vector in zip(batch, vectors):
            chunk_id = str(row["id"])
            points.append(
                models.PointStruct(
                    id=point_id(chunk_id),
                    vector=vector.tolist(),
                    payload={
                        "chunk_id": chunk_id,
                        "document_id": str(row["document_id"]),
                        "source_locator": str(row["source_locator"]),
                        "vehicle_tags": [
                            item
                            for item in str(row["vehicle_tags"]).split(",")
                            if item
                        ],
                        "scene": str(row["scene"]),
                        "energy_tags": [
                            item
                            for item in str(row["energy_tags"]).split(",")
                            if item
                        ],
                        "relative_path": str(row["relative_path"]),
                        "file_name": str(row["file_name"]),
                    },
                )
            )
        client.upsert(collection_name=collection, points=points, wait=True)
        indexed += len(points)
        print(
            f"Qdrant 新增向量：{indexed}/{len(rows_to_index)}",
            flush=True,
        )

    elapsed = round(time.perf_counter() - started, 2)
    report = {
        "backend": "qdrant_local",
        "collection": collection,
        "embedding_model": config["model"],
        "dimensions": config["dimensions"],
        "points": len(rows),
        "added": indexed,
        "deleted": len(stale_chunk_ids),
        "recreated": recreated,
        "seconds": elapsed,
        "path": str(store_path),
    }
    (BASE_DIR / "output" / "qdrant_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    client.close()
    return report


def semantic_search(question: str, limit: int | None = None) -> list[dict[str, Any]]:
    config = qdrant_config()
    client = _client()
    collection = config["collection"]
    if not client.collection_exists(collection):
        return []
    vector = next(_model().query_embed(question)).tolist()
    response = client.query_points(
        collection_name=collection,
        query=vector,
        limit=max(1, min(int(limit or config["semantic_limit"]), 500)),
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
    config = qdrant_config()
    try:
        client = _client()
        if not client.collection_exists(config["collection"]):
            return {"ready": False, **config, "points": 0}
        info = client.get_collection(config["collection"])
        return {
            "ready": True,
            **config,
            "points": int(info.points_count or 0),
        }
    except Exception as exc:
        return {"ready": False, **config, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="构建本地 Qdrant 中文语义索引")
    parser.add_argument("--batch-size", type=int, default=96)
    arguments = parser.parse_args()
    report = build_qdrant_index(batch_size=arguments.batch_size)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
