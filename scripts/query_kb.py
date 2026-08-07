from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from build_kb import normalize_text, normalize_vector, search_tokens, vector_counts
from qdrant_store import semantic_search
from task_router import chunk_task_types, detect_task_type, exact_fault_sources


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = BASE_DIR / "output" / "knowledge_base.db"
SEMANTIC_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kb-semantic")
_CHUNK_CACHE: dict[str, dict[str, Any]] = {}
_CHUNK_CACHE_LOCK = threading.Lock()


def warm_chunk_cache(database_path: Path = DEFAULT_DATABASE) -> int:
    """Keep Qdrant result metadata in memory instead of re-reading SQLite."""
    if _CHUNK_CACHE:
        return len(_CHUNK_CACHE)
    with _CHUNK_CACHE_LOCK:
        if _CHUNK_CACHE:
            return len(_CHUNK_CACHE)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT c.id, c.content, c.source_locator, c.vehicle_tags,
                       c.scene, c.energy_tags, d.relative_path, d.file_name,
                       d.effective_date, d.version
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.enabled = 1
                """
            ).fetchall()
            _CHUNK_CACHE.update({str(row["id"]): dict(row) for row in rows})
        finally:
            connection.close()
    return len(_CHUNK_CACHE)


def _timed_semantic_search(
    question: str, limit: int, task_type: str = ""
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    rows = semantic_search(question, limit=limit, task_type=task_type)
    return rows, round((time.perf_counter() - started) * 1000, 2)

RELATION_NAMES = {
    "HAS_FAULT_CODE": "包含故障码",
    "HAS_SYMPTOM": "表现为",
    "HAS_POSSIBLE_CAUSE": "可能原因",
    "DIAGNOSED_BY": "诊断方法",
    "DISPLAYED_AS": "仪表显示",
    "INVOLVES_COMPONENT": "涉及部件",
    "CONFIRMED_ROOT_CAUSE": "确认根因",
    "RESOLVED_BY": "维修措施",
    "APPLIES_TO": "适用于",
    "APPLIES_TO_SCHEME": "适用车型方案",
    "REPRESENTS_DRAWING": "对应图纸",
    "HAS_ENERGY_TYPE": "能源类型",
    "SUPPORTED_BY": "证据来源",
    "SUPPORTED_BY_SUPPLIER": "供应商支持",
    "SUPPLIED_BY": "供应商",
    "BELONGS_TO_CATEGORY": "属于分类",
    "ALIAS_OF": "别名指向",
}

QUERY_SYNONYMS = {
    "刹车片": ["制动片", "摩擦片", "制动蹄", "制动蹄片", "制动衬片"],
    "制动片": ["刹车片", "摩擦片", "制动蹄", "制动衬片"],
    "摩擦片": ["刹车片", "制动片", "制动蹄片", "制动衬片"],
    "刹车": ["制动", "制动系统"],
    "制动": ["刹车", "制动系统"],
    "刹车盘": ["制动盘"],
    "制动盘": ["刹车盘"],
    "刹车油": ["制动液"],
    "制动液": ["刹车油"],
    "方向机": ["转向机", "转向器"],
    "转向机": ["方向机", "转向器"],
    "电瓶": ["蓄电池", "低压蓄电池"],
    "蓄电池": ["电瓶", "低压蓄电池"],
    "不上高压": ["无法上高压", "高压不上电", "高压无法建立"],
    "无法上高压": ["不上高压", "高压不上电", "高压无法建立"],
    "报码": ["故障码", "故障代码", "DTC"],
    "故障码": ["报码", "故障代码", "DTC"],
}


def expand_query(question: str) -> tuple[str, list[str]]:
    expanded: list[str] = []
    for phrase, synonyms in QUERY_SYNONYMS.items():
        if phrase in question:
            for synonym in synonyms:
                if synonym not in question and synonym not in expanded:
                    expanded.append(synonym)
    if not expanded:
        return question, []
    return f"{question} {' '.join(expanded)}", expanded


def diversify_sources(
    candidates: list[dict[str, Any]], top_k: int
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    per_document: dict[str, int] = {}
    for limit in (2, 4, 999):
        for candidate in candidates:
            chunk_id = str(candidate["chunk_id"])
            if chunk_id in selected_ids:
                continue
            path = str(candidate["relative_path"])
            if per_document.get(path, 0) >= limit:
                continue
            selected.append(candidate)
            selected_ids.add(chunk_id)
            per_document[path] = per_document.get(path, 0) + 1
            if len(selected) >= top_k:
                return selected
    return selected


def dot_product(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(index, 0.0) for index, value in left.items())


def fts_expression(question: str) -> str:
    tokens = search_tokens(question)
    preferred = [
        token
        for token in tokens
        if re.search(r"[a-z0-9]", token, re.I) or len(token) >= 2
    ][:24]
    escaped = [token.replace('"', '""') for token in preferred]
    if not escaped:
        return 'content:"__no_match__"'
    return "search_terms:(" + " OR ".join(f'"{token}"' for token in escaped) + ")"


def graph_keywords(question: str) -> list[str]:
    keywords: list[str] = []
    for token in re.findall(
        r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*|[\u3400-\u9fff]{2,8}",
        question,
    ):
        if token not in keywords:
            keywords.append(token)
    for token in search_tokens(question):
        if len(token) >= 2 and token not in keywords:
            keywords.append(token)
        if len(keywords) >= 10:
            break
    return keywords[:10]


def graph_fts_expression(question: str) -> str:
    keywords = graph_keywords(question)
    tokens = search_tokens(" ".join(keywords))
    preferred = [
        token
        for token in tokens
        if re.search(r"[a-z0-9]", token, re.I) or len(token) >= 2
    ][:40]
    escaped = [token.replace('"', '""') for token in preferred]
    if not escaped:
        return ""
    return "search_terms:(" + " OR ".join(f'"{token}"' for token in escaped) + ")"


def fault_code_terms(question: str) -> list[str]:
    """Extract P/SPN/FMI codes, including compact forms such as ``SPN647``."""
    values: list[str] = []
    text = question.lower()
    for match in re.finditer(r"\bp\d{4,7}\b", text):
        values.append(match.group(0))
    for match in re.finditer(r"\bspn\s*[:#-]?\s*(\d{3,8})", text):
        values.extend(["spn" + match.group(1), match.group(1)])
    for match in re.finditer(r"\bfmi\s*[:#-]?\s*(\d{1,3})", text):
        values.extend(["fmi" + match.group(1), match.group(1)])
    # Keep standalone 3+ digit values for formats like “SPN 647”.
    if "spn" in text or "fmi" in text:
        values.extend(re.findall(r"\b\d{3,8}\b", text))
    values = [value for index, value in enumerate(values) if value and value not in values[:index]]
    return values


def focused_fault_excerpt(content: str, code_terms: list[str], limit: int = 1400) -> str:
    """Put the matching code and its nearby diagnosis text at the front."""
    if not code_terms:
        return content
    lowered = content.lower()
    compact = re.sub(r"[\s:#-]+", "", lowered)
    positions: list[int] = []
    for term in code_terms:
        if term in {"spn", "fmi"}:
            continue
        position = fault_term_position(content, term)
        if position >= 0:
            positions.append(position)
    if not positions:
        return content
    start = max(0, min(positions) - 220)
    end = min(len(content), start + limit)
    prefix = "…" if start else ""
    suffix = "…" if end < len(content) else ""
    return prefix + content[start:end].strip() + suffix


def fault_term_position(content: str, term: str) -> int:
    lowered = content.lower()
    if term.startswith(("spn", "fmi")):
        compact = re.sub(r"[\s:#-]+", "", lowered)
        position = compact.find(term)
        if position >= 0:
            return position
        term = re.sub(r"^(?:spn|fmi)", "", term)
    if term.isdigit():
        match = re.search(rf"(?<![\d.]){re.escape(term)}(?![\d.])", lowered)
        return match.start() if match else -1
    match = re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered)
    return match.start() if match else -1


def fetch_triples(
    connection: sqlite3.Connection, question: str, limit: int = 30
) -> tuple[list[dict[str, Any]], str]:
    keywords = graph_keywords(question)
    if not keywords:
        return [], "none"
    method = "triples_fts5"
    try:
        expression = graph_fts_expression(question)
        if not expression:
            return [], "none"
        rows = connection.execute(
            """
            SELECT t.subject, t.subject_label, t.predicate, t.object,
                   t.object_label, t.source_path, t.source_locator,
                   t.confidence, t.review_status
            FROM triples_fts
            JOIN triples t ON t.id = triples_fts.triple_id
            WHERE triples_fts MATCH ?
            ORDER BY bm25(triples_fts)
            LIMIT 600
            """,
            (expression,),
        ).fetchall()
    except sqlite3.OperationalError:
        method = "legacy_like"
        conditions: list[str] = []
        parameters: list[str] = []
        for keyword in keywords:
            pattern = f"%{keyword}%"
            conditions.append(
                "(subject LIKE ? OR object LIKE ? OR predicate LIKE ?)"
            )
            parameters.extend([pattern, pattern, pattern])
        rows = connection.execute(
            f"""
            SELECT subject, subject_label, predicate, object, object_label,
                   source_path, source_locator, confidence, review_status
            FROM triples
            WHERE {" OR ".join(conditions)}
            LIMIT 600
            """,
            parameters,
        ).fetchall()
    candidates = [
        {
            "subject": row["subject"],
            "subject_label": row["subject_label"],
            "predicate": row["predicate"],
            "predicate_name": RELATION_NAMES.get(row["predicate"], row["predicate"]),
            "object": row["object"],
            "object_label": row["object_label"],
            "source_path": row["source_path"],
            "source_locator": row["source_locator"],
            "confidence": row["confidence"],
            "review_status": row["review_status"],
        }
        for row in rows
    ]
    relation_priority = {
        "CONFIRMED_ROOT_CAUSE": 5,
        "RESOLVED_BY": 5,
        "DIAGNOSED_BY": 4,
        "HAS_POSSIBLE_CAUSE": 4,
        "HAS_FAULT_CODE": 4,
        "HAS_SYMPTOM": 3,
        "REPRESENTS_DRAWING": 4,
        "APPLIES_TO": 3,
        "SUPPORTED_BY": 2,
    }

    def score(item: dict[str, Any]) -> float:
        subject = item["subject"].lower()
        obj = item["object"].lower()
        combined = f"{subject} {obj} {item['predicate'].lower()}"
        value = float(relation_priority.get(item["predicate"], 0))
        for keyword in keywords:
            normalized = keyword.lower()
            weight = 5.0 if re.search(r"[a-z0-9]", normalized) else 2.5
            if subject == normalized or obj == normalized:
                value += weight * 3
            elif normalized in combined:
                value += weight
        if item["subject_label"] == "Document":
            value -= 2
        if item["predicate"] == "BELONGS_TO_CATEGORY":
            value -= 2
        return value

    candidates.sort(key=score, reverse=True)
    return candidates[:limit], method


def build_answer(
    question: str,
    sources: list[dict[str, Any]],
    triples: list[dict[str, Any]],
) -> str:
    if not sources and not triples:
        return (
            "专有知识库中暂未检索到足够证据。请补充车系、故障码、"
            "故障现象或零部件名称后重试。"
        )
    parts = ["根据当前启用的专有知识库，检索到以下相关信息："]
    for index, source in enumerate(sources[:3], start=1):
        excerpt = normalize_text(source["excerpt"])
        if len(excerpt) > 260:
            excerpt = excerpt[:260].rstrip() + "…"
        parts.append(
            f"{index}. {excerpt}（来源：{source['relative_path']}，"
            f"{source['source_locator']}）"
        )
    if triples:
        parts.append("关联三元组：")
        for triple in triples[:6]:
            parts.append(
                f"- {triple['subject']} —{triple['predicate_name']}→ "
                f"{triple['object']}"
            )
    parts.append("回答用于辅助诊修，涉及维修操作时请以有效版本原文和安全规范为准。")
    return "\n".join(parts)


def search_knowledge_base(
    database_path: Path,
    question: str,
    vehicle_series: str = "",
    scene: str = "",
    energy_type: str = "",
    top_k: int = 8,
    context: str = "",
    candidate_limit: int = 240,
    semantic_limit: int = 80,
    rrf_k: int = 60,
) -> dict[str, Any]:
    retrieval_started = time.perf_counter()
    question = normalize_text(question)
    if not question:
        raise ValueError("question 不能为空")
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        task_type = detect_task_type(question)
        exact_fault_matches = exact_fault_sources(question)
        if exact_fault_matches:
            total_ms = round((time.perf_counter() - retrieval_started) * 1000, 2)
            return {
                "question": question,
                "filters": {
                    "vehicle_series": vehicle_series,
                    "scene": scene,
                    "energy_type": energy_type,
                    "task_type": task_type,
                },
                "answer": build_answer(question, exact_fault_matches, []),
                "sources": exact_fault_matches[: max(1, top_k)],
                "triples": [],
                "retrieval": {
                    "candidate_count": len(exact_fault_matches),
                    "task_type": task_type,
                    "exact_fault_match_count": len(exact_fault_matches),
                    "lexical_candidate_count": 0,
                    "semantic_candidate_count": 0,
                    "semantic_error": "",
                    "source_count": min(len(exact_fault_matches), max(1, top_k)),
                    "triple_count": 0,
                    "expanded_terms": [],
                    "graph_method": "skipped_for_exact_fault_code",
                    "candidate_limit": 0,
                    "timing_ms": {
                        "candidate_fetch": total_ms,
                        "semantic": 0.0,
                        "rerank": 0.0,
                        "graph": 0.0,
                        "total": total_ms,
                    },
                    "method": "任务路由 + 故障码结构化精确索引（跳过全库向量检索）",
                },
            }
        dimensions = json.loads(
            connection.execute(
                "SELECT value_json FROM vector_meta WHERE key = 'dimensions'"
            ).fetchone()[0]
        )
        idf = json.loads(
            connection.execute(
                "SELECT value_json FROM vector_meta WHERE key = 'idf'"
            ).fetchone()[0]
        )
        expanded_question, expanded_terms = expand_query(question)
        query_text = (
            f"{context}\n{expanded_question}" if context else expanded_question
        )
        code_terms = fault_code_terms(query_text)
        query_vector = normalize_vector(
            vector_counts(query_text, int(dimensions)), idf
        )

        where = ["d.enabled = 1"]
        parameters: list[Any] = [fts_expression(query_text)]
        # Scene is a ranking preference rather than a hard filter. Vehicle
        # diagnostics often need maintenance and operation manual evidence too.
        if vehicle_series:
            where.append(
                "(c.vehicle_tags = '' OR instr(',' || c.vehicle_tags || ',', ?) > 0)"
            )
            parameters.append(f",{vehicle_series},")
        if energy_type:
            where.append(
                "(c.energy_tags = '' OR instr(',' || c.energy_tags || ',', ?) > 0)"
            )
            parameters.append(f",{energy_type},")

        # Qdrant already supplies the semantic ranking. Loading each legacy
        # SQLite JSON vector here transfers several megabytes per question and
        # is unused whenever Qdrant succeeds.
        uses_qdrant_semantic = int(semantic_limit) > 0
        vector_column = "NULL AS vector_json" if uses_qdrant_semantic else "v.vector_json"
        vector_join = "" if uses_qdrant_semantic else "JOIN chunk_vectors v ON v.chunk_id = c.id"
        semantic_future = (
            SEMANTIC_EXECUTOR.submit(
                _timed_semantic_search,
                expanded_question,
                max(10, int(semantic_limit)),
                task_type,
            )
            if int(semantic_limit) > 0
            else None
        )
        candidate_fetch_started = time.perf_counter()
        rows = connection.execute(
            f"""
            SELECT c.id, c.content, c.source_locator, c.vehicle_tags, c.scene,
                   c.energy_tags, d.relative_path, d.file_name, d.effective_date,
                   d.version, {vector_column}, bm25(chunks_fts) AS lexical_rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.chunk_id
            JOIN documents d ON d.id = c.document_id
            {vector_join}
            WHERE chunks_fts MATCH ? AND {" AND ".join(where)}
            ORDER BY lexical_rank
            LIMIT ?
            """,
            [*parameters, max(20, min(int(candidate_limit), 2000))],
        ).fetchall()
        # Numeric SPN/FMI values are not always indexed by FTS5. Add direct
        # substring matches to the candidate pool, then let exact-code boosts
        # place them ahead of generic OCR/semantic matches.
        if code_terms:
            like_terms = [term for term in code_terms if term not in {"spn", "fmi"}]
            if like_terms:
                exact_where = " OR ".join(
                    (
                        "replace(replace(replace(lower(c.content),' ',''),':',''),'-','') LIKE ?"
                        if term.startswith(("spn", "fmi"))
                        else "lower(c.content) LIKE ?"
                    )
                    for term in like_terms
                )
                exact_rows = connection.execute(
                    f"""
                    SELECT c.id, c.content, c.source_locator, c.vehicle_tags, c.scene,
                           c.energy_tags, d.relative_path, d.file_name, d.effective_date,
                           d.version, {vector_column}, 0.0 AS lexical_rank
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    {vector_join}
                    WHERE d.enabled = 1 AND ({exact_where})
                    LIMIT 2000
                    """,
                    [f"%{term}%" for term in like_terms],
                ).fetchall()
                seen_ids = {str(row["id"]) for row in rows}
                rows = list(rows) + [row for row in exact_rows if str(row["id"]) not in seen_ids]
        candidate_fetch_ms = round(
            (time.perf_counter() - candidate_fetch_started) * 1000, 2
        )

        semantic_error = ""
        try:
            semantic_rows, semantic_ms = (
                semantic_future.result() if semantic_future else ([], 0.0)
            )
        except Exception as exc:
            semantic_rows = []
            semantic_ms = 0.0
            semantic_error = f"{type(exc).__name__}: {exc}"

        rerank_started = time.perf_counter()
        row_by_id = {str(row["id"]): dict(row) for row in rows}
        semantic_ids = [
            str(item["chunk_id"])
            for item in semantic_rows
            if str(item.get("chunk_id", ""))
        ]
        missing_ids = [item for item in semantic_ids if item not in row_by_id]
        cached_rows = _CHUNK_CACHE
        for chunk_id in missing_ids:
            cached_row = cached_rows.get(chunk_id)
            if cached_row:
                row_by_id[chunk_id] = {
                    **cached_row,
                    "vector_json": None,
                    "lexical_rank": 0.0,
                }
        missing_ids = [item for item in missing_ids if item not in row_by_id]
        for id_batch_start in range(0, len(missing_ids), 400):
            id_batch = missing_ids[id_batch_start : id_batch_start + 400]
            if not id_batch:
                continue
            placeholders = ",".join("?" for _ in id_batch)
            semantic_chunk_rows = connection.execute(
                f"""
                SELECT c.id, c.content, c.source_locator, c.vehicle_tags,
                       c.scene, c.energy_tags, d.relative_path, d.file_name,
                       d.effective_date, d.version, NULL AS vector_json,
                       0.0 AS lexical_rank
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.enabled = 1 AND c.id IN ({placeholders})
                """,
                id_batch,
            ).fetchall()
            row_by_id.update(
                {str(row["id"]): dict(row) for row in semantic_chunk_rows}
            )

        # Preserve the original SQLite-vector fallback if Qdrant is unavailable.
        if uses_qdrant_semantic and not semantic_rows and row_by_id:
            lexical_ids = list(row_by_id)
            for id_batch_start in range(0, len(lexical_ids), 400):
                id_batch = lexical_ids[id_batch_start : id_batch_start + 400]
                placeholders = ",".join("?" for _ in id_batch)
                vector_rows = connection.execute(
                    f"SELECT chunk_id, vector_json FROM chunk_vectors "
                    f"WHERE chunk_id IN ({placeholders})",
                    id_batch,
                ).fetchall()
                for vector_row in vector_rows:
                    chunk_id = str(vector_row["chunk_id"])
                    if chunk_id in row_by_id:
                        row_by_id[chunk_id]["vector_json"] = vector_row["vector_json"]

        candidates: list[dict[str, Any]] = []
        lexical_ranks = {
            str(row["id"]): index for index, row in enumerate(rows, start=1)
        }
        semantic_ranks = {
            str(item["chunk_id"]): index
            for index, item in enumerate(semantic_rows, start=1)
        }
        semantic_scores = {
            str(item["chunk_id"]): float(item.get("score", 0.0))
            for item in semantic_rows
        }
        candidate_ids = list(
            dict.fromkeys([*lexical_ranks.keys(), *semantic_ranks.keys()])
        )
        category_by_chunk = chunk_task_types(candidate_ids)
        total = max(1, len(rows))
        for chunk_id in candidate_ids:
            row = row_by_id.get(chunk_id)
            if row is None:
                continue
            if semantic_rows:
                rrf_score = 0.0
                if chunk_id in lexical_ranks:
                    rrf_score += 1.0 / (max(1, int(rrf_k)) + lexical_ranks[chunk_id])
                if chunk_id in semantic_ranks:
                    rrf_score += 1.0 / (max(1, int(rrf_k)) + semantic_ranks[chunk_id])
                maximum_rrf = 2.0 / (max(1, int(rrf_k)) + 1)
                retrieval_score = 0.82 * (rrf_score / maximum_rrf)
                retrieval_score += 0.18 * max(
                    0.0, semantic_scores.get(chunk_id, 0.0)
                )
            else:
                index = lexical_ranks.get(chunk_id, total)
                vector = json.loads(row["vector_json"])
                vector_score = dot_product(query_vector, vector)
                lexical_score = 1.0 - (index - 1) / total
                retrieval_score = 0.58 * lexical_score + 0.42 * vector_score
            metadata_boost = 0.0
            candidate_task = category_by_chunk.get(chunk_id, "")
            if candidate_task == task_type:
                metadata_boost += 0.34
            elif task_type != "general" and candidate_task:
                metadata_boost -= 0.12
            if task_type == "fault_code" and candidate_task == "claim_case":
                metadata_boost -= 0.65
            if vehicle_series and vehicle_series in row["vehicle_tags"].split(","):
                metadata_boost += 0.08
            if scene and scene == row["scene"]:
                metadata_boost += 0.05
            content_text = row["content"]
            specific_matches = sum(
                1
                for term in expanded_terms
                if len(term) >= 3 and term in content_text
            )
            metadata_boost += min(0.36, specific_matches * 0.12)
            if code_terms:
                content_lower = content_text.lower()
                code_hits = [
                    term
                    for term in code_terms
                    if term not in {"spn", "fmi"}
                    and fault_term_position(content_text, term) >= 0
                ]
                if code_hits:
                    # A direct P-code/SPN number hit must outrank generic OCR
                    # fragments returned by the semantic collection.
                    metadata_boost += min(3.2, 2.0 + 0.45 * len(code_hits))
                    structured_hits = sum(
                        1
                        for term in code_hits
                        if term.isdigit()
                        and re.search(rf"(?<!\d){re.escape(term)}\s*\*\s*\d+", content_lower)
                    )
                    metadata_boost += min(1.8, structured_hits * 1.8)
                if "spn" in code_terms and "fmi" in code_terms:
                    if "spn" in content_lower and "fmi" in content_lower:
                        metadata_boost += 0.8
            intent_paths = {
                "故障码": "故障码",
                "图纸": "图纸",
                "电路图": "图纸",
                "保养": "保养",
                "保用": "保用",
                "使用": "用车",
            }
            for intent, path_marker in intent_paths.items():
                if intent in question and path_marker in row["relative_path"]:
                    metadata_boost += 0.22
            score = retrieval_score + metadata_boost
            candidates.append(
                {
                    "chunk_id": chunk_id,
                    "score": round(score, 6),
                    "excerpt": focused_fault_excerpt(row["content"], code_terms),
                    "relative_path": row["relative_path"],
                    "file_name": row["file_name"],
                    "source_locator": row["source_locator"],
                    "vehicle_tags": [
                        value for value in row["vehicle_tags"].split(",") if value
                    ],
                    "scene": row["scene"],
                    "energy_tags": [
                        value for value in row["energy_tags"].split(",") if value
                    ],
                    "version": row["version"],
                    "effective_date": row["effective_date"],
                    "lexical_rank": lexical_ranks.get(chunk_id),
                    "semantic_rank": semantic_ranks.get(chunk_id),
                    "semantic_score": round(
                        semantic_scores.get(chunk_id, 0.0), 6
                    ),
                    "task_type": candidate_task,
                }
            )
        candidates.sort(key=lambda item: item["score"], reverse=True)
        if exact_fault_matches:
            exact_ids = {str(item["chunk_id"]) for item in exact_fault_matches}
            remaining = [
                item for item in candidates if str(item["chunk_id"]) not in exact_ids
            ]
            sources = [
                *exact_fault_matches[: max(1, top_k)],
                *diversify_sources(remaining, max(0, top_k - len(exact_fault_matches))),
            ][: max(1, top_k)]
        else:
            sources = diversify_sources(candidates, max(1, top_k))
        rerank_ms = round((time.perf_counter() - rerank_started) * 1000, 2)
        graph_started = time.perf_counter()
        triples, graph_method = fetch_triples(connection, expanded_question)
        graph_ms = round((time.perf_counter() - graph_started) * 1000, 2)
        total_ms = round((time.perf_counter() - retrieval_started) * 1000, 2)
        return {
            "question": question,
            "filters": {
                "vehicle_series": vehicle_series,
                "scene": scene,
                "energy_type": energy_type,
                "task_type": task_type,
            },
            "answer": build_answer(question, sources, triples),
            "sources": sources,
            "triples": triples,
            "retrieval": {
                "candidate_count": len(candidates),
                "task_type": task_type,
                "exact_fault_match_count": len(exact_fault_matches),
                "lexical_candidate_count": len(rows),
                "semantic_candidate_count": len(semantic_rows),
                "semantic_error": semantic_error,
                "source_count": len(sources),
                "triple_count": len(triples),
                "expanded_terms": expanded_terms,
                "graph_method": graph_method,
                "candidate_limit": max(20, min(int(candidate_limit), 2000)),
                "timing_ms": {
                    "candidate_fetch": candidate_fetch_ms,
                    "semantic": semantic_ms,
                    "rerank": rerank_ms,
                    "graph": graph_ms,
                    "total": total_ms,
                },
                "method": (
                    "同义词扩展 + SQLite FTS5关键词召回 + Qdrant中文语义向量召回 "
                    "+ RRF融合 + 场景软优先 + 来源多样化 + 图谱FTS邻接扩展"
                    if semantic_rows
                    else "同义词扩展 + FTS5字符召回 + 哈希TF-IDF向量重排 + 场景软优先 + 来源多样化 + 图谱FTS邻接扩展"
                ),
            },
        }
    finally:
        connection.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--question", required=True)
    parser.add_argument("--vehicle-series", default="")
    parser.add_argument("--scene", default="")
    parser.add_argument("--energy-type", default="")
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()
    result = search_knowledge_base(
        args.database.resolve(),
        args.question,
        vehicle_series=args.vehicle_series,
        scene=args.scene,
        energy_type=args.energy_type,
        top_k=args.top_k,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
