from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
TASK_DATABASE = BASE_DIR / "output" / "task_index.db"

TASK_LABELS = {
    "vin": "VIN查询",
    "fault_code": "故障码查询",
    "symptom_diagnosis": "故障诊断",
    "usage": "用车与操作",
    "maintenance": "保养知识",
    "warranty": "保用保修",
    "service_technical": "服务咨询",
    "drawing": "图纸电路",
    "general": "通用知识",
}

TASK_SCENES = {
    "fault_code": "修",
    "symptom_diagnosis": "修",
    "maintenance": "养",
    "warranty": "保",
    "usage": "用",
    "vin": "",
    "service_technical": "",
    "drawing": "修",
    "general": "",
}

CATEGORY_ALIASES = {
    "vin": "vin",
    "vin查询": "vin",
    "故障码": "fault_code",
    "故障码查询": "fault_code",
    "故障诊断": "symptom_diagnosis",
    "症状诊断": "symptom_diagnosis",
    "故障现象": "symptom_diagnosis",
    "用车": "usage",
    "用车知识": "usage",
    "用车与操作": "usage",
    "操作": "usage",
    "保养": "maintenance",
    "保养知识": "maintenance",
    "保用": "warranty",
    "保修": "warranty",
    "保用保修": "warranty",
    "服务咨询": "service_technical",
    "服务技术": "service_technical",
    "图纸": "drawing",
    "图纸电路": "drawing",
    "通用": "general",
    "通用知识": "general",
}


def normalize_task_type(value: str) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "")
    if normalized in TASK_LABELS:
        return normalized
    exact = CATEGORY_ALIASES.get(normalized, "")
    if exact:
        return exact
    source_category_rules = (
        (("故障码", "spn", "fmi"), "fault_code"),
        (("保用", "保修", "索赔"), "warranty"),
        (("保养", "维护"), "maintenance"),
        (("解放行", "app", "使用常识", "操作"), "usage"),
        (("通讯录", "服务站", "预约进站", "调件", "救援", "收费"), "service_technical"),
        (("图纸", "电路图", "原理图"), "drawing"),
        (("vin", "底盘号"), "vin"),
        (("故障", "报修", "维修咨询"), "symptom_diagnosis"),
    )
    for markers, task_type in source_category_rules:
        if any(marker in normalized for marker in markers):
            return task_type
    return ""


def classify_batch_question(question: str, explicit: str = "") -> dict[str, Any]:
    """Classify an imported question locally without consuming model tokens."""
    explicit_task = normalize_task_type(explicit)
    if explicit_task:
        return {
            "task_type": explicit_task,
            "task_label": TASK_LABELS[explicit_task],
            "scene": TASK_SCENES[explicit_task],
            "confidence": 1.0,
            "reason": "导入文件指定分类",
            "automatic": False,
        }

    raw = str(question or "").strip()
    text = raw.lower()
    scores: dict[str, int] = {key: 0 for key in TASK_LABELS}
    reasons: dict[str, list[str]] = {key: [] for key in TASK_LABELS}

    def add(task: str, weight: int, reason: str) -> None:
        scores[task] += weight
        if reason not in reasons[task]:
            reasons[task].append(reason)

    if re.search(r"\bspn\s*[:#-]?\s*\d+|\bfmi\s*[:#-]?\s*\d+|\bp[0-9a-f]{4,7}\b", text):
        add("fault_code", 30, "识别到标准故障码")
    if re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", raw, re.I):
        add("vin", 30, "识别到17位VIN")
    if any(word in text for word in ("vin", "底盘号", "车辆档案", "静态信息")):
        add("vin", 16, "包含VIN或车辆档案关键词")

    marker_rules = {
        "warranty": (
            ("保用", "保修", "质保", "三包", "索赔条件", "在保", "保多久", "保用期", "保修期"),
            18,
            "询问保用、保修或索赔规则",
        ),
        "maintenance": (
            ("保养", "更换周期", "维护周期", "多久换", "润滑周期", "首次保养", "定期维护"),
            17,
            "询问保养或更换周期",
        ),
        "drawing": (
            ("图纸", "电路图", "原理图", "接线图", "线束图", "管路图"),
            17,
            "询问图纸或电路资料",
        ),
        "service_technical": (
            ("服务站", "维修站", "联系电话", "电话号码", "电话", "客服电话", "就近", "地址", "收费", "救援", "预约进站", "调件", "补贴"),
            15,
            "询问服务网点或服务事项",
        ),
        "usage": (
            ("怎么用", "如何使用", "如何操作", "操作方法", "说明书", "驾驶", "设置", "app", "解放行", "功能", "怎么查询", "如何查询", "怎么看", "后台", "耗电度数", "充电", "钥匙"),
            14,
            "询问车辆或应用操作方法",
        ),
        "symptom_diagnosis": (
            ("不工作", "不灵", "异响", "故障", "异常", "无法", "失效", "不制冷", "水温高", "没电", "费电", "损坏", "漏", "时走时不走", "吃胎", "报码", "报修"),
            13,
            "描述车辆故障现象",
        ),
    }
    for task, (markers, weight, reason) in marker_rules.items():
        matches = sum(1 for marker in markers if marker in text)
        if matches:
            add(task, weight + min(matches - 1, 3) * 3, reason)

    # Intent expressions help questions without explicit domain nouns.
    if any(marker in text for marker in ("原因", "怎么修", "怎么排查", "维修方法", "处理方法")):
        add("symptom_diagnosis", 8, "询问原因或维修排查")
    if any(marker in text for marker in ("周期", "期限", "几个月", "多少公里")):
        if scores["warranty"]:
            add("warranty", 6, "包含期限表达")
        elif scores["maintenance"]:
            add("maintenance", 6, "包含周期表达")
    if any(marker in text for marker in ("车辆配置", "电池品牌", "配件价格", "配件型号")):
        add("service_technical", 13, "询问车辆配置、品牌或配件信息")

    best_task, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score <= 0:
        best_task, best_score = "general", 4
        reasons[best_task].append("未命中专用规则，按通用知识处理")
    ranked = sorted(scores.values(), reverse=True)
    margin = best_score - (ranked[1] if len(ranked) > 1 else 0)
    confidence = min(0.99, 0.58 + best_score / 70 + max(0, margin) / 100)
    return {
        "task_type": best_task,
        "task_label": TASK_LABELS[best_task],
        "scene": TASK_SCENES[best_task],
        "confidence": round(confidence, 3),
        "reason": "；".join(reasons[best_task]) or "本地规则分类",
        "automatic": True,
    }


def rewrite_engineer_question(question: str) -> dict[str, str]:
    """Turn a customer-service record into a concise engineer-style question.

    This is deliberately local and deterministic so batch normalization does not
    consume model tokens or invent a vehicle fault before retrieval.
    """
    original = re.sub(r"\s+", " ", str(question or "")).strip()
    text = original
    # Remove call-log metadata, phone numbers, and trailing ticket IDs.
    text = re.sub(r"20\d{2}年?\s*\d{1,2}月?\s*\d{1,2}日?[^：:，,]{0,20}[：:]", "", text)
    text = re.sub(r"(?:1\d{10}|0\d{2,3}[- ]?\d{7,8})", "", text)
    text = re.sub(r"(?:[，,。；; ]*)(?:编号|工单|单号|记录)?\s*\d{3,8}\s*$", "", text)
    text = re.sub(r"^(?:用户|客户|车主|司机)?(?:来电|来访|反馈|反映|报修|咨询|询问)\s*[：:，,]?\s*", "", text)
    text = re.sub(r"^(?:请问|想问|想咨询|咨询一下|问一下)\s*", "", text)
    text = re.sub(r"(?:，|,)?\s*(?:请问|想问|想咨询|咨询|询问)(?:一下)?\s*", "，", text)
    text = re.sub(r"[。；;]+$", "", text).strip(" ，,：:")
    if not text:
        text = original

    # Preserve the concrete symptom while expressing the requested engineering action.
    text = re.sub(r"^(?:动力电池|蓄电池)故障[，,]?\s*", "电池系统故障，", text)
    text = text.replace("时走时不走", "车辆间歇性无法行驶")
    text = text.replace("能走，有时候不能走", "车辆间歇性无法行驶")
    text = text.replace("不上高压", "无法上高压")
    text = text.replace("充不上电", "车辆无法充电")
    text = text.replace("挂不上档", "车辆无法挂挡")
    text = text.replace("电耗高", "能耗异常偏高")
    text = text.replace("动力不足", "车辆动力不足")
    text = text.replace("能量回收不好用", "能量回收功能异常")
    text = text.replace("空调不好用", "空调系统工作异常")
    text = text.replace("绝缘故障", "高压系统绝缘故障")
    text = text.replace("电机高温", "驱动电机温度过高")
    text = text.replace("电池高温", "动力电池温度过高")
    text = text.replace("DCDC不工作", "DCDC不工作")
    text = text.replace("转向油泵不工作", "转向油泵不工作")
    text = text.replace("打气泵不工作", "打气泵不工作")
    text = text.replace("PTC不工作", "PTC不工作")
    text = text.replace("取力器不工作", "取力器不工作")

    if any(mark in text for mark in ("保用", "保修", "质保", "索赔", "三包")):
        suffix = "如何判定适用条件并办理？"
    elif any(mark in text for mark in ("服务站", "维修站", "电话", "收费", "预约", "进站")):
        suffix = "应如何处理或安排进站？"
    elif any(mark in text for mark in ("怎么处理", "如何处理", "怎么排查", "如何排查", "怎么办")):
        suffix = ""
    else:
        suffix = "如何排查和处理？"
    rewritten = text.rstrip("？?。；;，,")
    if suffix and not rewritten.endswith(("？", "?")):
        rewritten += suffix
    elif not rewritten.endswith(("？", "?")):
        rewritten += "？"
    return {"original_question": original, "engineer_question": rewritten}


def detect_task_type(question: str) -> str:
    return str(classify_batch_question(question)["task_type"])


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
