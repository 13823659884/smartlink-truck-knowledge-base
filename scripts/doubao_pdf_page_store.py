from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.json"
_CLIENT: Any = None


def _config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def _vision_module() -> Any:
    import sys

    scripts_dir = str(BASE_DIR / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import doubao_image_store

    return doubao_image_store


def page_config() -> dict[str, Any]:
    _load_local_env()
    configured = _config().get("multimodal_retrieval", {})
    return {
        "source_root": (BASE_DIR / str(configured.get("source_root", "../0729中重卡知识库素材"))).resolve(),
        "path": str((BASE_DIR / os.getenv("DOUBAO_PDF_QDRANT_PATH", "output/qdrant_doubao_pdf_pages")).resolve()),
        "collection": os.getenv("DOUBAO_PDF_QDRANT_COLLECTION", "truck_knowledge_pdf_pages_doubao_vision"),
        "dimensions": int(os.getenv("DOUBAO_EMBEDDING_DIMENSIONS", "2048")),
        "render_dpi": int(os.getenv("DOUBAO_PDF_RENDER_DPI", "100")),
        "workers": int(os.getenv("DOUBAO_PDF_WORKERS", "4")),
    }


def _qdrant_client() -> Any:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    import sys

    packages = BASE_DIR / "tools" / "python_packages"
    if str(packages) not in sys.path:
        sys.path.insert(0, str(packages))
    from qdrant_client import QdrantClient

    config = page_config()
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


def _pdftoppm() -> str:
    configured = ""
    report = BASE_DIR / "output" / "build_report.json"
    if report.exists():
        try:
            configured = str(json.loads(report.read_text(encoding="utf-8")).get("ocr", {}).get("pdf_renderer", ""))
        except Exception:
            configured = ""
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("pdftoppm")
    if found:
        return found
    raise FileNotFoundError("未找到 pdftoppm，无法把 PDF 页面渲染为图片")


def _page_key(pdf: Path, root: Path, page: int) -> str:
    relative = pdf.resolve().relative_to(root.resolve()).as_posix()
    stat = pdf.stat()
    raw = f"{relative}:{page}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    return "pdfpage:" + hashlib.sha1(raw).hexdigest()


def _point_id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"smartlink-kb:doubao-pdf-page:{key}"))


def _list_pages(root: Path) -> list[tuple[Path, int, str]]:
    from pypdf import PdfReader

    pages: list[tuple[Path, int, str]] = []
    for pdf in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"):
        try:
            count = len(PdfReader(str(pdf)).pages)
        except Exception as exc:
            print(f"PDF页数读取失败：{pdf}：{type(exc).__name__}: {exc}", flush=True)
            continue
        for page in range(1, count + 1):
            pages.append((pdf, page, _page_key(pdf, root, page)))
    return pages


def _render_page(pdf: Path, page: int, temp_dir: Path, dpi: int) -> Path:
    prefix = temp_dir / f"page_{page:05d}"
    command = [
        _pdftoppm(),
        "-f", str(page),
        "-l", str(page),
        "-png",
        "-singlefile",
        "-r", str(max(72, min(dpi, 160))),
        "-scale-to", "1800",
        str(pdf),
        str(prefix),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    output = prefix.with_suffix(".png")
    if result.returncode != 0 or not output.exists():
        detail = (result.stderr or result.stdout or "渲染失败")[-500:]
        raise RuntimeError(f"PDF第{page}页渲染失败：{detail}")
    return output


def _existing_keys(client: Any, collection: str) -> set[str]:
    found: set[str] = set()
    if not client.collection_exists(collection):
        return found
    offset = None
    while True:
        points, offset = client.scroll(collection_name=collection, limit=256, offset=offset, with_payload=["page_key"], with_vectors=False)
        found.update(str((p.payload or {}).get("page_key", "")) for p in points if (p.payload or {}).get("page_key"))
        if offset is None:
            return found


def build_index(*, workers: int = 4, batch_size: int = 4, limit: int = 0) -> dict[str, Any]:
    config = page_config()
    root = config["source_root"]
    if not root.exists():
        raise FileNotFoundError(f"知识库原始资料目录不存在：{root}")
    pages = _list_pages(root)
    if limit > 0:
        pages = pages[:limit]
    _vision = _vision_module()
    client = _qdrant_client()
    from qdrant_client.http import models

    collection = config["collection"]
    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=config["dimensions"], distance=models.Distance.COSINE),
            hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
        )
    existing = _existing_keys(client, collection)
    pending = [item for item in pages if item[2] not in existing]
    started = time.perf_counter()
    added = 0
    failed = 0
    errors: list[dict[str, str]] = []

    def process(item: tuple[Path, int, str]) -> tuple[Path, int, str, list[float]]:
        pdf, page, key = item
        with tempfile.TemporaryDirectory(prefix="doubao-pdf-page-") as temp:
            rendered = _render_page(pdf, page, Path(temp), config["render_dpi"])
            vector = _vision.embed_image(rendered, timeout=120)
        return pdf, page, key, vector

    for start in range(0, len(pending), max(1, int(batch_size))):
        batch = pending[start : start + max(1, int(batch_size))]
        points = []
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as pool:
            future_map = {pool.submit(process, item): item for item in batch}
            for future in as_completed(future_map):
                pdf, page, key = future_map[future]
                try:
                    _, _, _, vector = future.result()
                    relative = pdf.resolve().relative_to(root.resolve()).as_posix()
                    points.append(
                        models.PointStruct(
                            id=_point_id(key),
                            vector=vector,
                            payload={
                                "page_key": key,
                                "relative_path": relative,
                                "file_name": pdf.name,
                                "page": page,
                                "source_locator": f"第{page}页",
                                "source_type": "pdf_page_image",
                                "vector_provider": "doubao-embedding-vision",
                            },
                        )
                    )
                except Exception as exc:
                    failed += 1
                    errors.append({"path": str(pdf), "page": str(page), "error": f"{type(exc).__name__}: {exc}"})
        if points:
            client.upsert(collection_name=collection, points=points, wait=True)
            added += len(points)
        print(f"Doubao PDF页面向量化：已写入 {added}/{len(pending)}，失败 {failed}", flush=True)

    report = {
        "backend": "doubao_embedding_vision_pdf_pages",
        "collection": collection,
        "dimensions": config["dimensions"],
        "source_pdfs": len({str(item[0]) for item in pages}),
        "source_pages": len(pages),
        "existing": len(existing),
        "added": added,
        "failed": failed,
        "seconds": round(time.perf_counter() - started, 2),
        "path": config["path"],
        "errors": errors[:100],
    }
    (BASE_DIR / "output" / "qdrant_doubao_pdf_pages_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    client.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="把全部 PDF 页面渲染并写入豆包图片向量库")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(build_index(workers=args.workers, batch_size=args.batch_size, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
