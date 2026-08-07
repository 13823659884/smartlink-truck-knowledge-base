from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
TASK_DATABASE = BASE_DIR / "output" / "task_index.db"


def detect_task_type(question: str) -> str:
    text = question.strip().lower()
    if re.search(r"\bspn\s*[:#-]?\s*\d+|\bfmi\s*[:#-]?\s*\d+|\bp[0-9a-f]{4,7}\b", text):
        return "fault_code"
    if re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", question, re.I) or "vin" in text or "底盘号" in text:
        return "vin"
    if any(word in question for word in ("保养", "更换周期", "维护周期")):
        return "maintenance"
    if any(word in question for word in ("保用", "保修", "索赔条件", "三包")):
        return "warranty"
    if any(word in question for word in ("图纸", "电路图", "原理图", "接线图")):
        return "drawing"
    if any(word in question for word in ("怎么用", "如何使用", "操作方法", "驾驶")):
        return "usage"
    if any(word in question for word in ("不工作", "不灵", "异响", "故障", "异常", "无法", "失效", "怎么办", "怎么修")):
        return "symptom_diagnosis"
    return "general"


def fault_code_parts(question: str) -> tuple[int | None, int | None, str]:
    spn_match = re.search(r"\bspn\s*[:#-]?\s*(\d{2,8})", question, re.I)
    fmi_match = re.search(r"\bfmi\s*[:#-]?\s*(\d{1,2})", question, re.I)
    p_match = re.search(r"\b(p[0-9a-f]{4,7})\b", question, re.I)
    return (
        int(spn_match.group(1)) if spn_match else None,
        int(fmi_match.group(1)) if fmi_match else None,
        p_match.group(1).upper() if p_match else "",
    )


def exact_fault_sources(
    question: str,
    *,
    database_path: Path = TASK_DATABASE,
    limit: int = 24,
) -> list[dict[str, Any]]:
    spn, fmi, p_code = fault_code_parts(question)
    if not database_path.exists() or (spn is None and not p_code):
        return []
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        where: list[str] = []
        values: list[object] = []
        if spn is not None:
            where.append("spn=?")
            values.append(spn)
        if fmi is not None:
            where.append("fmi=?")
            values.append(fmi)
        if p_code:
            where.append("p_code=?")
            values.append(p_code)
        rows = connection.execute(
            f"""
            SELECT * FROM fault_code_entries
            WHERE {' AND '.join(where)}
            ORDER BY fmi, relative_path, source_locator
            LIMIT ?
            """,
            [*values, max(1, min(limit, 100))],
        ).fetchall()
    finally:
        connection.close()

    results: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        identity = (row["spn"], row["fmi"], row["description_zh"])
        if identity in seen:
            continue
        seen.add(identity)
        title = f"SPN {row['spn']} / FMI {row['fmi']}"
        description = str(row["description_zh"] or row["description_en"])
        details = [f"{title}：{description}"]
        if row["description_en"]:
            details.append(f"英文定义：{row['description_en']}")
        if row["controller_code"]:
            details.append(f"控制器故障码：{row['controller_code']}")
        if row["severity"]:
            details.append(f"等级：{row['severity']}")
        results.append(
            {
                "chunk_id": str(row["chunk_id"]),
                "score": 10.0 - len(results) * 0.01,
                "excerpt": "；".join(details),
                "relative_path": str(row["relative_path"]),
                "file_name": Path(str(row["relative_path"])).name,
                "source_locator": str(row["source_locator"]),
                "vehicle_tags": [
                    value for value in str(row["vehicle_tags"]).split(",") if value
                ],
                "scene": "修",
                "energy_tags": [],
                "version": "",
                "effective_date": "",
                "lexical_rank": None,
                "semantic_rank": None,
                "semantic_score": 1.0,
                "task_type": "fault_code",
                "exact_fault_match": True,
                "spn": row["spn"],
                "fmi": row["fmi"],
            }
        )
    return results


def chunk_task_types(
    chunk_ids: list[str], *, database_path: Path = TASK_DATABASE
) -> dict[str, str]:
    if not chunk_ids or not database_path.exists():
        return {}
    connection = sqlite3.connect(database_path)
    try:
        result: dict[str, str] = {}
        for start in range(0, len(chunk_ids), 500):
            batch = chunk_ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT chunk_id, primary_task FROM chunk_categories "
                f"WHERE chunk_id IN ({placeholders})",
                batch,
            ).fetchall()
            result.update({str(row[0]): str(row[1]) for row in rows})
        return result
    finally:
        connection.close()
