from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
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


def _load_local_env() -> None:
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


def image_config() -> dict[str, Any]:
    _load_local_env()
    configured = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(
        "multimodal_retrieval", {}
    )
    return {
        "source_root": (BASE_DIR / str(configured.get("source_root", "../0729中重卡知识库素材"))).resolve(),
        "path": str(
            (
                BASE_DIR
                / os.getenv(
                    "DOUBAO_IMAGE_QDRANT_PATH",
                    configured.get("image_qdrant_path", "output/qdrant_doubao_images"),
                )
            ).resolve()
        ),
        "collection": str(
            os.getenv(
                "DOUBAO_IMAGE_QDRANT_COLLECTION",
                configured.get("image_qdrant_collection", "truck_knowledge_images_doubao_vision"),
            )
        ),
        "model": os.getenv("DOUBAO_EMBEDDING_MODEL", "doubao-embedding-vision"),
        "dimensions": int(os.getenv("DOUBAO_EMBEDDING_DIMENSIONS", "2048")),
        "url": os.getenv(
            "DOUBAO_EMBEDDING_URL",
            "https://ark.cn-beijing.volces.com/api/plan/v3/embeddings/multimodal",
        ).rstrip("/"),
        "api_key": os.getenv("DOUBAO_EMBEDDING_API_KEY", "").strip()
        or os.getenv("ARK_API_KEY", "").strip(),
        "semantic_limit": int(os.getenv("DOUBAO_IMAGE_SEMANTIC_LIMIT", "12")),
    }


def _client() -> Any:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _enable_local_packages()
            from qdrant_client import QdrantClient

            config = image_config()
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


def _batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def image_key(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    stat = path.stat()
    digest = hashlib.sha1(f"{relative}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
    return f"image:{digest}"


def point_id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"smartlink-kb:doubao-image:{key}"))


def _image_data_url(path: Path) -> str:
    _enable_local_packages()
    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            max_side = max(image.size)
            if max_side > 1800:
                scale = 1800 / max_side
                image = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                )
            from io import BytesIO

            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=86, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        suffix = path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        return f"data:{mime};base64,{data}"


def _parse_vector(payload: dict[str, Any]) -> list[float]:
    data = payload.get("data") or []
    vector = data.get("embedding") if isinstance(data, dict) else data[0].get("embedding")
    if isinstance(vector, list) and vector and isinstance(vector[0], list):
        vector = vector[0]
    if not isinstance(vector, list) or not vector:
        raise RuntimeError("豆包图片向量接口未返回有效 embedding")
    return [float(value) for value in vector]


def embed_image(path: Path, *, timeout: int = 90) -> list[float]:
    config = image_config()
    if not config["api_key"]:
        raise RuntimeError("未配置 DOUBAO_EMBEDDING_API_KEY 或 ARK_API_KEY")
    body = {
        "model": config["model"],
        "input": [{"type": "image_url", "image_url": {"url": _image_data_url(path)}}],
    }
    request = Request(
        config["url"],
        data=json.dumps(body).encode("utf-8"),
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
                vector = _parse_vector(json.loads(response.read().decode("utf-8")))
            if len(vector) != int(config["dimensions"]):
                raise RuntimeError(f"图片向量维度异常：{len(vector)}")
            return vector
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(f"豆包图片向量接口 HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(f"豆包图片向量网络错误：{exc}") from exc
    raise RuntimeError("豆包图片向量接口重试失败")


def _existing_keys(client: Any, collection: str) -> set[str]:
    found: set[str] = set()
    if not client.collection_exists(collection):
        return found
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=256,
            offset=offset,
            with_payload=["image_key"],
            with_vectors=False,
        )
        found.update(
            str((point.payload or {}).get("image_key", ""))
            for point in points
            if (point.payload or {}).get("image_key")
        )
        if offset is None:
            return found


def build_index(*, workers: int = 4, batch_size: int = 8, timeout: int = 90) -> dict[str, Any]:
    config = image_config()
    root = config["source_root"]
    if not root.exists():
        raise FileNotFoundError(f"知识库原始资料目录不存在：{root}")
    image_paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    )
    _enable_local_packages()
    from qdrant_client import QdrantClient
    from qdrant_client.http import models

    Path(config["path"]).mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=config["path"])
    collection = config["collection"]
    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=config["dimensions"], distance=models.Distance.COSINE),
            hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
        )
    existing = _existing_keys(client, collection)
    pending = [(path, image_key(path, root)) for path in image_paths]
    pending = [(path, key) for path, key in pending if key not in existing]
    started = time.perf_counter()
    added = 0
    failed = 0
    errors: list[dict[str, str]] = []

    def make_point(item: tuple[Path, str]) -> tuple[Path, str, list[float]]:
        path, key = item
        return path, key, embed_image(path, timeout=timeout)

    for batch in _batches(pending, max(1, int(batch_size))):
        points = []
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 12))) as pool:
            futures = {pool.submit(make_point, item): item for item in batch}
            for future in as_completed(futures):
                path, key = futures[future]
                try:
                    _, _, vector = future.result()
                    relative = path.resolve().relative_to(root.resolve()).as_posix()
                    points.append(
                        models.PointStruct(
                            id=point_id(key),
                            vector=vector,
                            payload={
                                "image_key": key,
                                "relative_path": relative,
                                "file_name": path.name,
                                "source_type": "image_file",
                                "vector_provider": "doubao-embedding-vision",
                            },
                        )
                    )
                except Exception as exc:
                    failed += 1
                    errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
        if points:
            client.upsert(collection_name=collection, points=points, wait=True)
            added += len(points)
        print(f"Doubao 图片向量化：已写入 {added}/{len(pending)}，失败 {failed}", flush=True)

    report = {
        "backend": "doubao_embedding_vision_image",
        "collection": collection,
        "embedding_model": config["model"],
        "dimensions": config["dimensions"],
        "source_images": len(image_paths),
        "existing": len(existing),
        "added": added,
        "failed": failed,
        "seconds": round(time.perf_counter() - started, 2),
        "path": config["path"],
        "errors": errors[:100],
    }
    (BASE_DIR / "output" / "qdrant_doubao_images_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    client.close()
    return report


def semantic_search(question: str, limit: int | None = None) -> list[dict[str, Any]]:
    config = image_config()
    client = _client()
    if not client.collection_exists(config["collection"]):
        return []
    # Importing the text helper keeps the query vector in the same 2048-D space.
    from doubao_vision_store import embed_text

    response = client.query_points(
        collection_name=config["collection"],
        query=embed_text(question),
        limit=max(1, min(int(limit or config["semantic_limit"]), 100)),
        with_payload=True,
        with_vectors=False,
    )
    return [
        {
            "image_key": str((point.payload or {}).get("image_key", "")),
            "score": float(point.score),
            "payload": dict(point.payload or {}),
        }
        for point in response.points
        if (point.payload or {}).get("image_key")
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="构建独立的豆包图片向量库")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    print(json.dumps(build_index(workers=args.workers, batch_size=args.batch_size, timeout=args.timeout), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
