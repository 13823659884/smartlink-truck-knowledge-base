from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
LOCAL_PACKAGES = BASE_DIR / "tools" / "python_packages"
_ENGINE: Any = None
_ENGINE_LOCK = threading.Lock()


def _enable_local_packages() -> None:
    value = str(LOCAL_PACKAGES)
    if LOCAL_PACKAGES.exists() and value not in sys.path:
        sys.path.insert(0, value)


def _engine() -> Any:
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _enable_local_packages()
            from rapidocr import RapidOCR

            _ENGINE = RapidOCR()
    return _ENGINE


def pdftoppm_path() -> str:
    detected = shutil.which("pdftoppm")
    if detected:
        return detected
    candidates = [
        Path(r"D:\texlive\texlive\2024\bin\windows\pdftoppm.exe"),
        Path(r"C:\Program Files\poppler\Library\bin\pdftoppm.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("未找到 pdftoppm，无法把扫描 PDF 渲染为图片")


def ocr_status() -> dict[str, object]:
    try:
        _enable_local_packages()
        import rapidocr

        executable = pdftoppm_path()
        return {
            "available": True,
            "engine": "RapidOCR ONNX",
            "version": getattr(rapidocr, "__version__", "unknown"),
            "pdf_renderer": executable,
            "local_only": True,
        }
    except Exception as exc:
        return {
            "available": False,
            "engine": "RapidOCR ONNX",
            "error": f"{type(exc).__name__}: {exc}",
            "local_only": True,
        }


def recognize_image_path(
    path: Path, *, min_confidence: float = 0.45
) -> dict[str, object]:
    started = time.perf_counter()
    result = _engine()(str(path))
    texts = list(getattr(result, "txts", None) or [])
    scores = [float(value) for value in (getattr(result, "scores", None) or [])]
    accepted: list[str] = []
    accepted_scores: list[float] = []
    for index, text in enumerate(texts):
        score = scores[index] if index < len(scores) else 0.0
        cleaned = str(text).strip()
        if cleaned and score >= min_confidence:
            accepted.append(cleaned)
            accepted_scores.append(score)
    return {
        "text": "\n".join(accepted),
        "lines": accepted,
        "line_count": len(accepted),
        "confidence": (
            round(sum(accepted_scores) / len(accepted_scores), 4)
            if accepted_scores
            else 0.0
        ),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "engine": "RapidOCR ONNX",
    }


def recognize_image_bytes(
    data: bytes,
    *,
    suffix: str = ".jpg",
    min_confidence: float = 0.45,
    max_pixels: int = 25_000_000,
) -> dict[str, object]:
    if not data or len(data) > 8 * 1024 * 1024:
        raise ValueError("图片为空或超过 8MB")
    _enable_local_packages()
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if width < 32 or height < 32:
            raise ValueError("图片尺寸过小")
        if width * height > max_pixels:
            raise ValueError("图片像素过大，请压缩后重试")
        detected_format = (image.format or "JPEG").lower()
    safe_suffix = {
        "jpeg": ".jpg",
        "jpg": ".jpg",
        "png": ".png",
        "webp": ".webp",
        "bmp": ".bmp",
    }.get(detected_format, suffix if suffix in {".jpg", ".png", ".webp", ".bmp"} else ".jpg")
    with tempfile.TemporaryDirectory(prefix="kb-image-ocr-") as temp_dir:
        image_path = Path(temp_dir) / f"upload{safe_suffix}"
        image_path.write_bytes(data)
        result = recognize_image_path(
            image_path, min_confidence=min_confidence
        )
    result["width"] = width
    result["height"] = height
    return result


def recognize_pdf(
    path: Path,
    *,
    dpi: int = 160,
    min_confidence: float = 0.45,
    max_pages: int = 200,
) -> dict[str, object]:
    from pypdf import PdfReader

    page_count = len(PdfReader(str(path)).pages)
    if page_count > max_pages:
        raise ValueError(
            f"PDF 共 {page_count} 页，超过单文件 OCR 上限 {max_pages} 页"
        )
    units: list[tuple[str, str]] = []
    confidences: list[float] = []
    elapsed_ms = 0.0
    renderer = pdftoppm_path()
    with tempfile.TemporaryDirectory(prefix="kb-pdf-ocr-") as temp_dir:
        temp_root = Path(temp_dir)
        for page in range(1, page_count + 1):
            output_prefix = temp_root / f"page-{page}"
            completed = subprocess.run(
                [
                    renderer,
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-r",
                    str(dpi),
                    "-png",
                    "-singlefile",
                    str(path),
                    str(output_prefix),
                ],
                capture_output=True,
                timeout=90,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"PDF 第 {page} 页渲染失败：{detail}")
            page_result = recognize_image_path(
                output_prefix.with_suffix(".png"),
                min_confidence=min_confidence,
            )
            text = str(page_result.get("text", "")).strip()
            if text:
                units.append((f"第{page}页（OCR）", text))
                confidences.append(float(page_result.get("confidence", 0.0)))
            elapsed_ms += float(page_result.get("elapsed_ms", 0.0))
    return {
        "units": units,
        "pages": page_count,
        "recognized_pages": len(units),
        "confidence": (
            round(sum(confidences) / len(confidences), 4)
            if confidences
            else 0.0
        ),
        "elapsed_ms": round(elapsed_ms, 2),
        "engine": "RapidOCR ONNX",
    }
