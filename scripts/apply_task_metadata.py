from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
TASK_DATABASE = BASE_DIR / "output" / "task_index.db"
READY_FILE = BASE_DIR / "output" / "task_filter_ready.json"
LOCAL_PACKAGES = BASE_DIR / "tools" / "python_packages"


def enable_local_packages() -> None:
    for path in reversed(
        [LOCAL_PACKAGES, LOCAL_PACKAGES / "win32", LOCAL_PACKAGES / "win32" / "lib"]
    ):
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def task_map() -> dict[str, str]:
    connection = sqlite3.connect(TASK_DATABASE)
    try:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT chunk_id, primary_task FROM chunk_categories"
            )
        }
    finally:
        connection.close()


def classify_path(path: str) -> str:
    lower = path.lower()
    rules = (
        ("fault_code", ("故障码", "fault code", "dtc")),
        ("maintenance", ("保养", "维护周期", "润滑")),
        ("warranty", ("保用", "保修", "三包")),
        ("drawing", ("图纸", "电路图", "原理图", "接线图")),
        ("usage", ("用车", "使用说明", "驾驶员手册", "操作手册")),
        ("service_technical", ("服务技术文件", "技术通报", "技术通知")),
        ("claim_case", ("索赔单", "维修案例", "索赔跟踪")),
        ("symptom_diagnosis", ("维修", "修车", "故障诊断", "诊断树")),
        ("vin", ("vin", "底盘号", "整车档案")),
    )
    for task, markers in rules:
        if any(marker in lower for marker in markers):
            return task
    return "general"


def marker_key(path: Path, collection: str) -> str:
    return f"{path.resolve()}|{collection}"


def update_ready_marker(path: Path, collection: str, points: int) -> None:
    payload: dict[str, Any] = {"collections": {}}
    if READY_FILE.exists():
        try:
            payload = json.loads(READY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    payload.setdefault("collections", {})[marker_key(path, collection)] = {
        "ready": True,
        "points": points,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    READY_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def apply(path: Path, collection: str, batch_size: int = 500) -> dict[str, Any]:
    enable_local_packages()
    from qdrant_client import QdrantClient

    mapping = task_map()
    client = QdrantClient(path=str(path.resolve()))
    if not client.collection_exists(collection):
        raise RuntimeError(f"Qdrant collection 不存在：{collection}")
    offset = None
    updated = 0
    counts: defaultdict[str, int] = defaultdict(int)
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=max(64, min(batch_size, 2000)),
            offset=offset,
            with_payload=["chunk_id", "relative_path"],
            with_vectors=False,
        )
        grouped: defaultdict[str, list[Any]] = defaultdict(list)
        for point in points:
            payload = dict(point.payload or {})
            chunk_id = str(payload.get("chunk_id", ""))
            task = mapping.get(chunk_id) or classify_path(
                str(payload.get("relative_path", ""))
            )
            grouped[task].append(point.id)
        for task, ids in grouped.items():
            client.set_payload(
                collection_name=collection,
                payload={"task_type": task},
                points=ids,
                wait=True,
            )
            counts[task] += len(ids)
            updated += len(ids)
        if updated and updated % 5000 < len(points):
            print(f"Qdrant任务标签：{updated} 条", flush=True)
        if offset is None:
            break
    client.close()
    update_ready_marker(path, collection, updated)
    return {
        "path": str(path.resolve()),
        "collection": collection,
        "updated": updated,
        "task_counts": dict(counts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="给现有Qdrant向量补充任务分类标签")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    print(
        json.dumps(
            apply(args.path, args.collection, args.batch_size),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
