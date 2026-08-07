from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = BASE_DIR / "output" / "knowledge_base.db"
DEFAULT_CSV = BASE_DIR.parent / "车辆VIN信息.csv"
DOCUMENT_ID = "doc:structured-vin-master"
RELATIVE_PATH = "结构化数据/VIN车辆主数据/车辆VIN信息.csv"
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

REQUIRED_HEADERS = {
    "car_vin",
    "car_type",
    "car_series",
    "engine_type",
    "engine_model",
    "bsx",
    "fuel_type",
    "emission",
    "device_app_version",
    "mcu_version",
    "offline_time",
    "sim_match",
}

EXTRA_COLUMNS = {
    "vehicle_type": "TEXT DEFAULT ''",
    "device_app_version": "TEXT DEFAULT ''",
    "mcu_version": "TEXT DEFAULT ''",
    "sim_match": "TEXT DEFAULT ''",
}


def clean(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return "" if text.upper() == "UNDEFINED" else text


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_chunk_id(group: str, vins: list[str]) -> str:
    identity = f"{group}|{'|'.join(vins)}"
    return "chunk:vin:" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:24]


def local_hash_vector(text: str, dimensions: int, idf: list[float]) -> dict[str, float]:
    """Build the same lightweight sparse compatibility vector as build_kb.py."""
    counts: dict[int, int] = defaultdict(int)
    normalized = re.sub(r"\s+", "", text.lower())
    for width in (2, 3, 4):
        for index in range(max(0, len(normalized) - width + 1)):
            gram = normalized[index : index + width]
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=4).digest()
            counts[int.from_bytes(digest, "big") % dimensions] += 1
    for token in re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", normalized):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        counts[int.from_bytes(digest, "big") % dimensions] += 2
    weighted = {
        index: (1.0 + math.log(count)) * idf[index]
        for index, count in counts.items()
        if count > 0
    }
    norm = math.sqrt(sum(value * value for value in weighted.values())) or 1.0
    return {
        str(index): round(value / norm, 6)
        for index, value in weighted.items()
    }


def load_rows(path: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    by_vin: dict[str, dict[str, str]] = {}
    total = 0
    duplicates = 0
    invalid = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = set(reader.fieldnames or [])
        missing = REQUIRED_HEADERS - headers
        if missing:
            raise ValueError(f"VIN文件缺少字段：{', '.join(sorted(missing))}")
        for source in reader:
            total += 1
            vin = clean(source.get("car_vin")).upper()
            if not VIN_PATTERN.fullmatch(vin):
                invalid += 1
                continue
            record = {
                "vin": vin,
                "vehicle_type": clean(source.get("car_type")),
                "chassis_no": "",
                "emission_type": clean(source.get("emission")),
                "vehicle_series": clean(source.get("car_series")),
                "fuel_type": clean(source.get("fuel_type")),
                "announcement_model": "",
                "factory_model_code": "",
                "rear_axle": "",
                "tire_spec": "",
                "engine_type": clean(source.get("engine_type")),
                "engine_model": clean(source.get("engine_model")),
                "transmission_model": clean(source.get("bsx")),
                "offline_time": clean(source.get("offline_time")),
                "vehicle_note": "",
                "engine_name": "",
                "device_app_version": clean(source.get("device_app_version")),
                "mcu_version": clean(source.get("mcu_version")),
                "sim_match": clean(source.get("sim_match")),
            }
            if vin in by_vin:
                duplicates += 1
            by_vin[vin] = record
    return list(by_vin.values()), {
        "source_rows": total,
        "valid_rows": total - invalid,
        "invalid_rows": invalid,
        "duplicate_rows": duplicates,
        "unique_vins": len(by_vin),
    }


def make_chunks(rows: list[dict[str, str]], batch_size: int) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row["vehicle_series"] or "未标注车系", row["vehicle_type"] or "未标注车型")].append(row)

    chunks: list[dict[str, object]] = []
    ordinal = 0
    for (series, vehicle_type), records in sorted(groups.items()):
        records.sort(key=lambda item: item["vin"])
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            lines = [
                f"VIN车辆主数据；车系：{series}；车辆类型：{vehicle_type}。",
                "以下每行是一辆车的完整静态信息，可按VIN、车系、车型、发动机、变速箱、排放、版本或下线时间查询。",
            ]
            for item in batch:
                fields = [
                    ("VIN", item["vin"]),
                    ("车辆类型", item["vehicle_type"]),
                    ("车系", item["vehicle_series"]),
                    ("发动机类型", item["engine_type"]),
                    ("发动机型号", item["engine_model"]),
                    ("变速箱", item["transmission_model"]),
                    ("燃料种类", item["fuel_type"]),
                    ("排放", item["emission_type"]),
                    ("设备应用版本", item["device_app_version"]),
                    ("MCU版本", item["mcu_version"]),
                    ("下线时间", item["offline_time"]),
                    ("SIM匹配型号", item["sim_match"]),
                ]
                lines.append("；".join(f"{label}：{value}" for label, value in fields if value))
            content = "\n".join(lines)
            vins = [item["vin"] for item in batch]
            chunks.append(
                {
                    "id": stable_chunk_id(f"{series}|{vehicle_type}", vins),
                    "document_id": DOCUMENT_ID,
                    "ordinal": ordinal,
                    "source_locator": f"VIN主数据 第{ordinal + 1}组（{series}/{vehicle_type}）",
                    "content": content,
                    "search_terms": content,
                    "vehicle_tags": series if series != "未标注车系" else "",
                    "scene": "用",
                    "energy_tags": "",
                    "token_count": max(1, math.ceil(len(content) / 2)),
                }
            )
            ordinal += 1
    return chunks


def ensure_vin_columns(connection: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(vin_records)")}
    for name, definition in EXTRA_COLUMNS.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE vin_records ADD COLUMN {name} {definition}")


def import_data(csv_path: Path, database_path: Path, batch_size: int) -> dict[str, object]:
    if not csv_path.exists():
        raise FileNotFoundError(f"VIN文件不存在：{csv_path}")
    if not database_path.exists():
        raise FileNotFoundError(f"知识库不存在：{database_path}")
    rows, quality = load_rows(csv_path)
    chunks = make_chunks(rows, max(1, min(batch_size, 50)))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    modified_at = datetime.fromtimestamp(csv_path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")

    fields = (
        "vin", "vehicle_type", "chassis_no", "emission_type", "vehicle_series",
        "fuel_type", "announcement_model", "factory_model_code", "rear_axle",
        "tire_spec", "engine_type", "engine_model", "transmission_model",
        "offline_time", "vehicle_note", "engine_name", "device_app_version",
        "mcu_version", "sim_match",
    )
    with sqlite3.connect(database_path, timeout=120) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ensure_vin_columns(connection)
        connection.commit()
        existing_vins = connection.execute("SELECT COUNT(*) FROM vin_records").fetchone()[0]
        old_chunk_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM chunks WHERE document_id=?", (DOCUMENT_ID,)
            )
        ]
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            f"""
            INSERT INTO vin_records ({', '.join(fields)}, updated_at)
            VALUES ({', '.join('?' for _ in range(len(fields) + 1))})
            ON CONFLICT(vin) DO UPDATE SET
              {', '.join(f'{field}=excluded.{field}' for field in fields[1:])},
              updated_at=excluded.updated_at
            """,
            [tuple(row[field] for field in fields) + (now,) for row in rows],
        )
        for chunk_id in old_chunk_ids:
            connection.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
            connection.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (chunk_id,))
        connection.execute("DELETE FROM chunks WHERE document_id=?", (DOCUMENT_ID,))
        connection.execute(
            """
            INSERT INTO documents
            (id, relative_path, file_name, extension, sha256, size_bytes,
             modified_at, logical_key, version, effective_date, scene,
             vehicle_tags, energy_tags, status, enabled, chunk_count, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              relative_path=excluded.relative_path, file_name=excluded.file_name,
              extension=excluded.extension, sha256=excluded.sha256,
              size_bytes=excluded.size_bytes, modified_at=excluded.modified_at,
              logical_key=excluded.logical_key, scene=excluded.scene,
              vehicle_tags=excluded.vehicle_tags, status=excluded.status,
              enabled=excluded.enabled, chunk_count=excluded.chunk_count,
              error_message=NULL
            """,
            (
                DOCUMENT_ID, RELATIVE_PATH, csv_path.name, ".csv", sha256_file(csv_path),
                csv_path.stat().st_size, modified_at, "structured-vin-master", "",
                "", "用", "", "", "active", 1, len(chunks), None,
            ),
        )
        connection.executemany(
            """
            INSERT INTO chunks
            (id, document_id, ordinal, source_locator, content, search_terms,
             vehicle_tags, scene, energy_tags, token_count)
            VALUES (:id, :document_id, :ordinal, :source_locator, :content,
                    :search_terms, :vehicle_tags, :scene, :energy_tags, :token_count)
            """,
            chunks,
        )
        connection.executemany(
            "INSERT INTO chunks_fts(chunk_id, search_terms, content) VALUES (?, ?, ?)",
            [(item["id"], item["search_terms"], item["content"]) for item in chunks],
        )
        vector_meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute(
                "SELECT key, value_json FROM vector_meta WHERE key IN ('dimensions', 'idf')"
            )
        }
        dimensions = int(vector_meta.get("dimensions") or 0)
        idf = vector_meta.get("idf") or []
        legacy_vectors = 0
        if dimensions > 0 and isinstance(idf, list) and len(idf) == dimensions:
            connection.executemany(
                "INSERT INTO chunk_vectors(chunk_id, vector_json) VALUES (?, ?)",
                [
                    (
                        item["id"],
                        json.dumps(
                            local_hash_vector(str(item["content"]), dimensions, idf),
                            separators=(",", ":"),
                        ),
                    )
                    for item in chunks
                ],
            )
            legacy_vectors = len(chunks)
        connection.commit()
        total_vins = connection.execute("SELECT COUNT(*) FROM vin_records").fetchone()[0]

    return {
        "csv": str(csv_path.resolve()),
        "database": str(database_path.resolve()),
        **quality,
        "previous_vin_records": int(existing_vins),
        "vin_records": int(total_vins),
        "document_id": DOCUMENT_ID,
        "chunks": len(chunks),
        "local_compatibility_vectors": legacy_vectors,
        "replaced_chunks": len(old_chunk_ids),
        "batch_size": batch_size,
        "updated_at": now,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="增量导入车辆VIN主数据并生成VIN分类检索切片")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()
    report = import_data(args.csv.resolve(), args.database.resolve(), args.batch_size)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
