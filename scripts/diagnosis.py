from __future__ import annotations

import re
from typing import Any


FAULT_CODE_PATTERN = re.compile(
    r"(?i)(?:\bP\d{4,6}\b|\bU\d{4,6}\b|\bSPN\s*\d+(?:\s*FMI\s*\d+)?\b|"
    r"\b[A-Z]{2,6}[-_ ]?\d{2,6}\b)"
)
HIGH_RISK_TERMS = {
    "制动": "制动系统",
    "刹车": "制动系统",
    "高压": "高压系统",
    "绝缘": "高压系统",
    "转向": "转向系统",
    "起火": "消防风险",
    "冒烟": "消防风险",
}
CONTEXTUAL_REPLIES = {
    "是",
    "对",
    "有",
    "亮了",
    "点亮了",
    "正常",
    "能",
    "可以",
    "否",
    "不是",
    "没有",
    "没亮",
    "未点亮",
    "不正常",
    "不能",
    "不可以",
    "不确定",
    "不知道",
    "偶发",
    "一直",
}


def is_contextual_reply(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？,.!?]", "", text.strip())
    if not normalized or len(normalized) > 24:
        return False
    return normalized in CONTEXTUAL_REPLIES or any(
        normalized.startswith(prefix)
        for prefix in ("是的", "有的", "没有", "亮了", "没亮", "大概", "好像")
    )


def resolve_diagnostic_reply(
    question: str,
    *,
    pending_question: str = "",
    diagnostic_topic: str = "",
    explicit_prompt: str = "",
) -> dict[str, object]:
    prompt = explicit_prompt.strip() or pending_question.strip()
    applied = bool(prompt and is_contextual_reply(question))
    if not applied:
        return {
            "applied": False,
            "effective_question": question,
            "answered_prompt": "",
            "topic": question,
        }
    topic = diagnostic_topic.strip() or prompt
    return {
        "applied": True,
        "effective_question": (
            f"原始故障问题：{topic}\n"
            f"上一轮诊断追问：{prompt}\n"
            f"用户对该追问的回答：{question}\n"
            "请保持同一故障诊断上下文，结合上述回答继续分析，不要把短回答当成新问题。"
        ),
        "answered_prompt": prompt,
        "topic": topic,
    }


def select_pending_question(
    diagnosis: dict[str, object],
    related_questions: list[str] | None = None,
    answered_prompt: str = "",
) -> str:
    candidates = [
        str(item).strip()
        for item in diagnosis.get("next_questions", [])
        if str(item).strip()
    ]
    candidates.extend(
        str(item).strip()
        for item in (related_questions or [])
        if str(item).strip()
    )
    candidates = [item for item in dict.fromkeys(candidates) if item != answered_prompt]
    yes_no_markers = ("是否", "有没有", "能否", "亮", "正常", "一致")
    for candidate in candidates:
        if any(marker in candidate for marker in yes_no_markers):
            return candidate
    return candidates[0] if candidates else ""


def extract_fault_codes(text: str) -> list[str]:
    result: list[str] = []
    for match in FAULT_CODE_PATTERN.finditer(text.upper()):
        value = re.sub(r"\s+", " ", match.group(0).strip())
        if value not in result:
            result.append(value)
    return result[:8]


def diagnosis_summary(
    question: str,
    *,
    sources: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    vehicle_series: str = "",
    answered_prompt: str = "",
    user_reply: str = "",
) -> dict[str, object]:
    codes = extract_fault_codes(question)
    risks = [name for term, name in HIGH_RISK_TERMS.items() if term in question]
    risks = list(dict.fromkeys(risks))
    symptom_markers = ["故障", "报警", "不走", "无法", "异常", "报码", "亮", "失效"]
    diagnostic = bool(
        codes or risks or any(marker in question for marker in symptom_markers)
    )
    top_score = max(
        (float(item.get("score", 0.0)) for item in sources), default=0.0
    )
    if len(sources) >= 3 and top_score >= 0.7:
        evidence_status = "充分"
    elif sources:
        evidence_status = "部分匹配"
    else:
        evidence_status = "证据不足"

    next_questions: list[str] = []
    missing: list[str] = []
    if not vehicle_series:
        missing.append("具体车型")
        next_questions.append("请补充具体车型、年款或车辆配置。")
    if diagnostic and not codes:
        missing.append("故障码")
        next_questions.append("仪表或诊断仪显示的完整故障码是什么？")
    if "高压" in question or "绝缘" in question:
        next_questions.extend(
            [
                "车辆低压系统是否正常，能否读取诊断仪？",
                "故障发生在上电、充电还是行驶过程中？",
            ]
        )
    elif "制动" in question or "刹车" in question:
        next_questions.append("制动警示灯是否点亮，左右轮现象是否一致？")
    elif diagnostic:
        next_questions.append("故障是持续出现还是偶发，出现前做过哪些维修？")

    checklist = [
        "确认车型、能源类型和资料适用版本",
        "记录完整故障码、故障现象及发生工况",
        "按引用资料页码核对检查条件和标准值",
    ]
    if codes:
        checklist.insert(1, f"核对故障码：{'、'.join(codes)}")
    if risks:
        checklist.append("涉及安全关键系统，停止带故障运行并由合格人员处理")

    next_questions = [
        item for item in dict.fromkeys(next_questions) if item != answered_prompt
    ][:4]
    return {
        "enabled": diagnostic,
        "title": "引导式故障诊断" if diagnostic else "资料核对建议",
        "fault_codes": codes,
        "safety_level": "高" if risks else "一般",
        "risk_systems": risks,
        "evidence_status": evidence_status,
        "evidence_count": len(sources),
        "graph_relation_count": len(triples),
        "missing_information": missing,
        "checklist": checklist,
        "next_questions": next_questions,
        "answered_prompt": answered_prompt,
        "user_reply": user_reply if answered_prompt else "",
    }


def structure_fault_code(
    query: str,
    triples: list[dict[str, Any]],
    method: str,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    groups: dict[str, list[str]] = {
        "fault_codes": [],
        "possible_causes": [],
        "diagnostic_steps": [],
        "evidence": [],
        "related_systems": [],
    }
    for item in triples:
        predicate = str(item.get("predicate", ""))
        subject = str(item.get("subject", ""))
        obj = str(item.get("object", ""))
        if predicate == "HAS_FAULT_CODE":
            groups["related_systems"].append(subject)
            groups["fault_codes"].append(obj)
        elif predicate == "HAS_POSSIBLE_CAUSE":
            groups["possible_causes"].append(obj)
        elif predicate == "DIAGNOSED_BY":
            groups["diagnostic_steps"].append(obj)
        elif predicate == "SUPPORTED_BY":
            groups["evidence"].append(obj)
    for key, values in groups.items():
        groups[key] = list(dict.fromkeys(value for value in values if value))[:20]
    source_matches: list[dict[str, object]] = []
    query_upper = query.upper().replace(" ", "")
    for source in sources or []:
        excerpt = str(source.get("excerpt", ""))
        matching_lines = [
            line.strip()
            for line in excerpt.splitlines()
            if query_upper in line.upper().replace(" ", "")
        ]
        if matching_lines:
            source_matches.append(
                {
                    "file_name": source.get("file_name", ""),
                    "source_locator": source.get("source_locator", ""),
                    "relative_path": source.get("relative_path", ""),
                    "matching_text": matching_lines[0][:800],
                    "excerpt": excerpt[:1600],
                }
            )
    return {
        "query": query,
        "detected_codes": extract_fault_codes(query),
        "method": method,
        "match_count": max(len(triples), len(source_matches)),
        **groups,
        "triples": triples[:50],
        "source_matches": source_matches[:10],
    }
