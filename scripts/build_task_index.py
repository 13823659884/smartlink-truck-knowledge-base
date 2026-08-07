from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


BASE_DIR = Path(__file__).resolve().parents[1]
SOURCE_DATABASE = BASE_DIR / "output" / "knowledge_base.db"
TASK_DATABASE = BASE_DIR / "output" / "task_index.db"


TASK_LABELS = {
    "vin": "VIN查询",
    "fault_code": "故障码查询",
    "symptom_diagnosis": "症状诊断",
    "usage": "用车知识",
    "maintenance": "保养知识",
    "warranty": "保用知识",
    "service_technical": "服务技术文件",
    "drawing": "图纸电路",
    "claim_case": "维修索赔案例",
    "general": "通用资料",
}


def normalize_field(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip(" |")


def classify(path: str, content: str) -> tuple[str, list[str], float, list[str]]:
    text = f"{path}\n{content[:3000]}".lower()
    scores: Counter[str] = Counter()
    reasons: dict[str, list[str]] = {key: [] for key in TASK_LABELS}

    def add(task: str, weight: int, reason: str) -> None:
        scores[task] += weight
        if reason not in reasons[task]:
            reasons[task].append(reason)

    path_rules = {
        "fault_code": (("故障码", "fault code", "dtc"), 12),
        "vin": (("vin", "底盘号", "整车档案"), 10),
        "maintenance": (("保养", "维护周期", "润滑"), 9),
        "warranty": (("保用", "保修", "索赔政策", "三包"), 9),
        "usage": (("用车", "使用说明", "驾驶员手册", "操作手册"), 8),
        "service_technical": (("服务技术文件", "技术通报", "技术通知"), 8),
        "drawing": (("图纸", "电路图", "原理图", "接线图"), 10),
        "claim_case": (("索赔单", "维修案例", "索赔跟踪"), 11),
        "symptom_diagnosis": (("维修", "修车", "故障诊断", "诊断树"), 5),
    }
    lower_path = path.lower()
    for task, (markers, weight) in path_rules.items():
        for marker in markers:
            if marker in lower_path:
                add(task, weight, f"路径:{marker}")

    if re.search(r"(?<!\d)\d{2,8}\s*\*\s*\d{1,2}(?!\d)", content):
        add("fault_code", 16, "结构化SPN/FMI")
    if re.search(r"\bspn\s*[:#-]?\s*\d+|\bfmi\s*[:#-]?\s*\d+|\bp[0-9a-f]{4,7}\b", text):
        add("fault_code", 10, "故障码正文")
    if re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", content, re.I):
        add("vin", 7, "VIN格式")
    if any(marker in content for marker in ("用户反映", "用户报修", "经检查", "故障排除")):
        add("claim_case", 6, "维修案例三段式")
        add("symptom_diagnosis", 3, "症状与处置")
    if any(marker in content for marker in ("检查步骤", "诊断步骤", "可能原因", "故障原因")):
        add("symptom_diagnosis", 7, "诊断步骤")
    if any(marker in content for marker in ("保养周期", "更换周期", "首次保养", "定期保养")):
        add("maintenance", 8, "保养周期")
    if any(marker in content for marker in ("保用期限", "保修期限", "索赔条件", "不予保用")):
        add("warranty", 8, "保用规则")

    if not scores:
        return "general", [], 0.5, ["未命中专用规则"]
    ranked = scores.most_common()
    primary, best = ranked[0]
    secondary = [task for task, score in ranked[1:] if score >= max(5, best * 0.45)][:3]
    confidence = min(0.99, 0.55 + best / 40.0)
    return primary, secondary, round(confidence, 3), reasons[primary]


def _chinese_description(fields: list[str], pair_at: int, marker_at: int) -> str:
    candidates: list[tuple[int, str]] = []
    start = max(0, pair_at - 5)
    end = min(len(fields), marker_at)
    for index in range(start, end):
        value = normalize_field(fields[index])
        if not re.search(r"[\u3400-\u9fff]", value) or len(value) < 5:
            continue
        if value in {"发动机", "数据链", "警告", "故障"}:
            continue
        position_bonus = 3 if index > pair_at + 1 else 1
        keyword_bonus = 5 if any(
            marker in value
            for marker in ("电压", "电路", "传感器", "温度", "压力", "数据", "速率", "异常", "短路")
        ) else 0
        candidates.append((position_bonus + keyword_bonus + min(len(value), 120), value))
    return max(candidates, default=(0, ""))[1]


def _english_description(fields: list[str], pair_at: int, marker_at: int) -> str:
    candidates: list[tuple[int, str]] = []
    for index in range(max(0, pair_at - 5), min(len(fields), marker_at)):
        value = normalize_field(fields[index])
        if len(value) < 8 or re.search(r"[\u3400-\u9fff]", value):
            continue
        if not re.search(r"[A-Za-z]", value) or value in {"OBD", "Solid", "None", "Engine"}:
            continue
        keyword_bonus = 5 if any(
            marker in value.lower()
            for marker in ("circuit", "temperature", "pressure", "voltage", "data", "sensor", "rate")
        ) else 0
        candidates.append((keyword_bonus + min(len(value), 120), value))
    return max(candidates, default=(0, ""))[1]


def extract_spn_entries(content: str) -> Iterable[dict[str, object]]:
    fields = [normalize_field(item) for item in content.split("|")]
    for marker_at, marker in enumerate(fields):
        # Extracted spreadsheet rows are sometimes joined without a pipe, e.g.
        # ``1172*3 692`` where 692 is the first cell of the next row.
        match = re.match(r"^(\d{2,8})\s*\*\s*(\d{1,2})(?:\s|$)", marker)
        if not match:
            continue
        spn, fmi = int(match.group(1)), int(match.group(2))
        pair_at = -1
        for index in range(marker_at - 1, max(-1, marker_at - 18), -1):
            if index + 1 >= marker_at:
                continue
            if fields[index] == str(spn) and fields[index + 1] == str(fmi):
                pair_at = index
                break
        if pair_at < 0:
            continue
        description_zh = _chinese_description(fields, pair_at, marker_at)
        description_en = _english_description(fields, pair_at, marker_at)
        if not description_zh and not description_en:
            continue
        controller_code = ""
        for value in reversed(fields[max(0, pair_at - 7):pair_at]):
            if re.fullmatch(r"\d{1,7}", value) and int(value) not in {spn, fmi}:
                controller_code = value
                break
        severity = next(
            (
                value
                for value in fields[max(0, pair_at - 4):marker_at]
                if value.startswith("Class ") or "Warning" in value
            ),
            "",
        )
        yield {
            "spn": spn,
            "fmi": fmi,
            "controller_code": controller_code,
            "description_zh": description_zh,
            "description_en": description_en,
            "severity": severity,
        }


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS document_categories (
          document_id TEXT PRIMARY KEY,
          primary_task TEXT NOT NULL,
          secondary_tasks_json TEXT NOT NULL,
          confidence REAL NOT NULL,
          relative_path TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_document_categories_task
          ON document_categories(primary_task);
        CREATE TABLE IF NOT EXISTS chunk_categories (
          chunk_id TEXT PRIMARY KEY,
          document_id TEXT NOT NULL,
          primary_task TEXT NOT NULL,
          secondary_tasks_json TEXT NOT NULL,
          confidence REAL NOT NULL,
          reasons_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_categories_task
          ON chunk_categories(primary_task);
        CREATE TABLE IF NOT EXISTS fault_code_entries (
          id TEXT PRIMARY KEY,
          spn INTEGER,
          fmi INTEGER,
          p_code TEXT NOT NULL DEFAULT '',
          controller_code TEXT NOT NULL DEFAULT '',
          description_zh TEXT NOT NULL DEFAULT '',
          description_en TEXT NOT NULL DEFAULT '',
          severity TEXT NOT NULL DEFAULT '',
          chunk_id TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          source_locator TEXT NOT NULL,
          vehicle_tags TEXT NOT NULL DEFAULT '',
          raw_excerpt TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fault_code_spn_fmi
          ON fault_code_entries(spn, fmi);
        CREATE INDEX IF NOT EXISTS idx_fault_code_p
          ON fault_code_entries(p_code);
        CREATE VIRTUAL TABLE IF NOT EXISTS fault_code_fts USING fts5(
          entry_id UNINDEXED,
          description_zh,
          description_en,
          raw_excerpt,
          tokenize='unicode61'
        );
        """
    )


def build(source_database: Path, task_database: Path) -> dict[str, object]:
    if not source_database.exists():
        raise FileNotFoundError(f"知识库不存在：{source_database}")
    task_database.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_database)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(task_database)
    try:
        create_schema(target)
        target.execute("DELETE FROM document_categories")
        target.execute("DELETE FROM chunk_categories")
        target.execute("DELETE FROM fault_code_entries")
        target.execute("DELETE FROM fault_code_fts")
        rows = source.execute(
            """
            SELECT c.id AS chunk_id, c.document_id, c.content, c.source_locator,
                   c.vehicle_tags, d.relative_path
            FROM chunks c JOIN documents d ON d.id=c.document_id
            WHERE d.enabled=1
            ORDER BY d.relative_path, c.ordinal
            """
        )
        task_counts: Counter[str] = Counter()
        document_votes: dict[str, Counter[str]] = {}
        document_paths: dict[str, str] = {}
        fault_count = 0
        processed = 0
        for row in rows:
            primary, secondary, confidence, reasons = classify(
                str(row["relative_path"]), str(row["content"])
            )
            task_counts[primary] += 1
            document_votes.setdefault(str(row["document_id"]), Counter())[primary] += 1
            document_paths[str(row["document_id"])] = str(row["relative_path"])
            target.execute(
                """
                INSERT INTO chunk_categories
                (chunk_id, document_id, primary_task, secondary_tasks_json,
                 confidence, reasons_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["chunk_id"], row["document_id"], primary,
                    json.dumps(secondary, ensure_ascii=False), confidence,
                    json.dumps(reasons, ensure_ascii=False),
                ),
            )
            if primary == "fault_code" or "fault_code" in secondary:
                for entry in extract_spn_entries(str(row["content"])):
                    identity = "|".join(
                        [
                            str(entry["spn"]), str(entry["fmi"]),
                            str(entry["description_zh"]), str(row["relative_path"]),
                            str(row["source_locator"]),
                        ]
                    )
                    entry_id = hashlib.sha1(identity.encode("utf-8")).hexdigest()
                    excerpt = str(row["content"])[:2200]
                    target.execute(
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
                            row["chunk_id"], row["relative_path"],
                            row["source_locator"], row["vehicle_tags"], excerpt,
                        ),
                    )
                    if target.execute("SELECT changes()").fetchone()[0]:
                        target.execute(
                            "INSERT INTO fault_code_fts VALUES (?, ?, ?, ?)",
                            (entry_id, entry["description_zh"], entry["description_en"], excerpt),
                        )
                        fault_count += 1
            processed += 1
            if processed % 5000 == 0:
                target.commit()
                print(f"任务分类：{processed} 条切片", flush=True)

        for document_id, votes in document_votes.items():
            primary, count = votes.most_common(1)[0]
            secondary = [task for task, _ in votes.most_common()[1:4]]
            confidence = count / max(1, sum(votes.values()))
            target.execute(
                "INSERT INTO document_categories VALUES (?, ?, ?, ?, ?)",
                (
                    document_id, primary, json.dumps(secondary, ensure_ascii=False),
                    round(confidence, 3), document_paths[document_id],
                ),
            )
        built_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "built_at": built_at,
            "source_database": str(source_database),
            "chunks": processed,
            "fault_code_entries": fault_count,
            "task_counts": dict(task_counts),
            "task_labels": TASK_LABELS,
        }
        for key, value in metadata.items():
            target.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        target.commit()
        return metadata
    finally:
        source.close()
        target.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="建立任务分类与故障码精确索引")
    parser.add_argument("--source", type=Path, default=SOURCE_DATABASE)
    parser.add_argument("--output", type=Path, default=TASK_DATABASE)
    args = parser.parse_args()
    report = build(args.source.resolve(), args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
