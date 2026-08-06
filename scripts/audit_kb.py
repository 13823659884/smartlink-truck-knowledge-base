from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from query_kb import search_knowledge_base


BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "output" / "knowledge_base.db"
REPORT_PATH = BASE_DIR / "output" / "audit_report.json"

TESTS = [
    {"question": "BMS故障码怎么排查？", "scene": "修"},
    {"question": "无法上高压可能是什么原因？", "scene": "修"},
    {"question": "SAA38289有哪些图纸？"},
    {
        "question": "JH6保用期限是多少？",
        "vehicle_series": "JH6",
        "scene": "保",
    },
]


def main() -> int:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        status_counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM documents GROUP BY status"
            )
        }
        legacy_documents = [
            dict(row)
            for row in connection.execute(
                """
                SELECT relative_path, status, chunk_count
                FROM documents
                WHERE extension IN ('.doc', '.xls')
                ORDER BY relative_path
                """
            )
        ]
        orphan_chunks = connection.execute(
            """
            SELECT COUNT(*)
            FROM chunks c
            LEFT JOIN documents d ON d.id = c.document_id
            WHERE d.id IS NULL
            """
        ).fetchone()[0]
        missing_triple_entities = connection.execute(
            """
            SELECT COUNT(*)
            FROM triples t
            LEFT JOIN entities s ON s.id = t.subject_id
            LEFT JOIN entities o ON o.id = t.object_id
            WHERE s.id IS NULL OR o.id IS NULL
            """
        ).fetchone()[0]
        legacy_samples = [
            dict(row)
            for row in connection.execute(
                """
                SELECT d.relative_path, c.source_locator,
                       substr(c.content, 1, 500) AS sample
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.extension IN ('.doc', '.xls')
                GROUP BY d.id
                ORDER BY d.relative_path
                """
            )
        ]
    finally:
        connection.close()

    query_results = []
    for test in TESTS:
        result = search_knowledge_base(
            DATABASE_PATH,
            test["question"],
            vehicle_series=test.get("vehicle_series", ""),
            scene=test.get("scene", ""),
            top_k=3,
        )
        query_results.append(
            {
                "question": test["question"],
                "candidate_count": result["retrieval"]["candidate_count"],
                "sources": [
                    {
                        "relative_path": source["relative_path"],
                        "source_locator": source["source_locator"],
                        "score": source["score"],
                    }
                    for source in result["sources"]
                ],
                "triples": [
                    {
                        "subject": triple["subject"],
                        "predicate": triple["predicate"],
                        "object": triple["object"],
                    }
                    for triple in result["triples"][:5]
                ],
            }
        )

    report = {
        "valid": orphan_chunks == 0 and missing_triple_entities == 0,
        "status_counts": status_counts,
        "legacy_documents": legacy_documents,
        "legacy_samples": legacy_samples,
        "orphan_chunks": orphan_chunks,
        "missing_triple_entities": missing_triple_entities,
        "query_tests": query_results,
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(str(REPORT_PATH))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
