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

# Short workshop-style symptom phrases often omit words such as "vehicle" or
# "fault".  Keep them in one shared list so both online routing and batch task
# classification still send these questions through the enterprise KB.
VEHICLE_SYMPTOM_MARKERS = (
    "动力不足", "加速无力", "爬坡无力", "跑不动", "电耗高", "费电", "续航下降",
    "能量回收不好用", "能量回收失效", "空调不好用", "空调不工作", "不制冷",
    "绝缘故障", "绝缘异常", "电机高温", "电池高温", "水温高", "温度高",
    "不上高压", "无法上高压", "挂不上档", "挂不上挡", "无法挂档", "无法挂挡",
    "dcdc不工作", "转向油泵不工作", "打气泵不工作", "ptc不工作", "取力器不工作",
    "充不上电", "无法充电", "充电失败", "充电慢", "充电限流",
    "启动不了", "无法启动", "无法行驶", "风扇不工作", "风扇不转",
    "制动不灵敏", "刹车不灵敏", "制动失效", "刹车失效",
    "转向不灵敏", "转向沉重", "方向盘沉重", "转向助力异常", "方向跑偏",
    "漏油", "漏气", "漏水", "异响", "抖动", "冒黑烟", "冒白烟",
)

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
            VEHICLE_SYMPTOM_MARKERS + ("不工作", "不灵", "故障", "异常", "不能", "无法", "失效", "没电", "损坏", "漏", "时走时不走", "吃胎", "报码", "报修", "充电越来越慢", "电流小", "限流", "转速异常", "转速高", "转速低"),
            13,
            "描述车辆故障现象",
        ),
    }
    for task, (markers, weight, reason) in marker_rules.items():
        matches = sum(1 for marker in markers if marker in text)
        if matches:
            add(task, weight + min(matches - 1, 3) * 3, reason)

    # Intent expressions help questions without explicit domain nouns.
    if any(marker in text for marker in ("原因", "怎么修", "怎么排查", "如何排查", "如何处理", "维修方法", "处理方法")):
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

    # Remove the call timestamp/caller header, contact details, agent number and
    # other metadata before identifying the issue. Vehicle fault codes and VINs
    # are intentionally preserved because they are useful retrieval keys.
    text = re.sub(
        r"^\s*20\d{2}年\s*\d{1,2}月\s*\d{1,2}日\s*\d{1,2}[：:]\d{1,2}[：:]\d{1,2}\s*",
        "",
        text,
    )
    text = re.sub(
        r"^(?:用户|客户|车主|司机|服务站)?(?:来电|来访|反馈|反映|报修)\s*(?:1[3-9]\d{9})?\s*[：:；;，,]?\s*",
        "",
        text,
    )
    text = re.sub(r"1[3-9]\d{9}|0\d{2,3}[- ]?\d{7,8}", "", text)
    text = re.sub(r"[\u4e00-\u9fff]{1,4}(?:先生|女士|师傅)\s*[，,：:]?", "", text)
    text = re.sub(r"(?<![A-Z0-9])[\u4e00-\u9fff][A-Z][A-Z0-9]{5,7}(?![A-Z0-9])", "", text, flags=re.I)
    text = re.sub(
        r"(?:[，,。；; ]*)(?:客服|坐席|员工)?(?:编号|工号|工单|单号|记录)?\s*\d{4}\s*$",
        "",
        text,
    )

    # Everything after these expressions is normally handling history rather
    # than the customer's technical question. Keeping only the prefix avoids
    # sending call duration, callbacks, contact attempts and already-given
    # customer-service answers into retrieval and the answer model.
    handling_markers = (
        "客服告知", "已告知", "告知用户", "告知", "客服提供", "客服短信",
        "客服", "提供电话", "提供联系方式", "提供", "告诉", "建议联系",
        "建议用户", "引导联系", "指导联系", "引导在线",
        "表示帮其联系", "帮其联系", "安排救援", "客服转接", "回拨",
        "通话中", "用户知晓", "客户知晓", "用户表示知道", "用户表示自行",
        "用户说自行", "用户咨询", "用户在", "用户挂", "用户说", "用户已",
        "自己已联系", "无需联系", "无需安排", "无需对接", "自行联系服务站",
        "自行进站", "已经联系服务站", "已联系服务站", "查询中", "具体以",
        "可以联系", "服务站说", "挂机", "挂断",
    )
    marker_positions = [text.find(marker) for marker in handling_markers if text.find(marker) > 0]
    if marker_positions:
        text = text[: min(marker_positions)]

    # Remove remaining dialogue/reporting language without deleting the actual
    # symptom or requested subject.
    text = re.sub(r"^(?:用户|客户|车主|司机)?(?:表示|称|说|反映|反馈|报修)\s*[：:，,]?\s*", "", text)
    text = re.sub(r"^(?:请问|想问|想咨询|咨询一下|问一下|询问)\s*", "", text)
    text = re.sub(r"(?:，|,)?\s*(?:请问|想问|想咨询|咨询|询问)(?:一下)?(?:原因|是否正常|是不是正常)?\s*", "，", text)
    text = re.sub(r"(?:，|,)?\s*(?:用户|客户)(?:表示|称|说).*$", "", text)
    text = re.sub(r"[。；;]+$", "", text).strip(" ，,：:")

    # Long records can still contain several clauses. Keep only clauses that
    # carry a fault, component, code or an actual request, with a short maximum
    # length so the displayed summary remains the core question.
    clauses = [item.strip(" ，,。；;：:") for item in re.split(r"[。；;]", text) if item.strip(" ，,。；;：:")]
    noise_markers = (
        "服务站说", "服务站回复", "客服", "用户无需", "用户已", "用户在",
        "马上到", "报备一下", "有问题再联系", "具体以服务站", "查询不到",
    )
    clauses = [item for item in clauses if not any(marker in item for marker in noise_markers)]
    if clauses:
        text = clauses[0]

    # A technical fault followed by a request for a nearby station is still a
    # fault-diagnosis question. Remove that service tail so retrieval focuses on
    # the failure itself rather than contact-book information.
    technical_markers = (
        "故障", "异常", "不能", "无法", "不工作", "不灵", "异响", "高温",
        "水温", "气压", "转速", "限扭", "动力不足", "费电", "耗电", "充电慢",
        "充不上电", "充电限流", "没有空调", "不制冷", "损坏", "乌龟灯", "吃胎",
    )
    explicit_policy_request = any(
        marker in text
        for marker in ("保用", "保修", "质保", "索赔", "三包", "保养", "更换周期")
    )
    if any(marker in text for marker in technical_markers) and not explicit_policy_request:
        text = re.sub(
            r"[，,]\s*(?:去[^，,。]{0,8})?(?:咨询|询问|找)?(?:就近|附近|[^，,。]{1,10})?(?:服务站|维修站).*$",
            "",
            text,
        )
        parts = [item.strip() for item in re.split(r"[，,]", text) if item.strip()]
        selected_parts: list[str] = []
        for item in parts:
            if any(marker in item for marker in technical_markers):
                selected_parts.append(item)
            elif selected_parts and (
                re.search(r"\d", item)
                or any(marker in item for marker in ("能行驶", "可以行驶", "不能行驶", "成空挡"))
            ):
                selected_parts.append(item)
        if selected_parts:
            text = "，".join(selected_parts)

    # For administrative questions, discard opening call-quality chatter and
    # retain the actual requested item.
    if "补贴" in text and "补贴" not in text[:8]:
        start = text.find("停运补贴")
        text = text[start if start >= 0 else text.find("补贴") :]

    # Warranty records often include the answer immediately after the subject
    # without an “告知” verb. Stop after the requested warranty period.
    warranty_match = re.search(r"(.{1,40}?(?:保用|保修|质保)(?:时间|周期|期限)?)", text)
    if warranty_match:
        text = warranty_match.group(1)
    if len(text) > 60:
        comma_parts = [item.strip() for item in re.split(r"[，,]", text) if item.strip()]
        important_markers = (
            "故障", "异常", "不能", "无法", "不工作", "不灵", "异响", "高温",
            "水温", "气压", "限扭", "动力不足", "费电", "耗电", "充电", "保用",
            "保修", "质保", "保养", "周期", "服务站", "电话", "说明书", "解绑",
            "绑定", "spn", "fmi",
        )
        selected = [
            item for item in comma_parts
            if any(marker in item.lower() for marker in important_markers)
        ][:4]
        text = "，".join(selected or comma_parts[:3])[:60].rstrip("，,")
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
    text = text.replace("充电没有空调吗", "充电过程中空调无法使用")
    if "被别人绑定" in text or "其他人绑定" in text:
        text = "解放行提示车辆被他人绑定"
    text = text.replace("车辆车辆", "车辆")

    text = re.sub(r"^咨询", "", text).strip(" ，,")
    text = re.sub(r"^报(?=(?:动力|电池|发动机|变速箱|车辆|系统).*(?:故障|异常))", "", text)
    text = re.sub(r"，(?:原因|是否正常|是不是正常)$", "", text)
    text = re.sub(r"(?:怎么处理|如何处理|怎么排查|如何排查|怎么办)[？?]?$", "", text)

    if any(mark in text for mark in ("保用", "保修", "质保", "索赔", "三包")):
        suffix = "如何判定适用条件并办理？"
    elif any(mark in text for mark in ("保养", "多久换", "更换周期", "换一次油")):
        suffix = "应按什么周期和要求进行？"
    elif any(mark in text for mark in ("服务站", "维修站", "电话", "收费", "预约", "进站", "补贴", "代理商")):
        suffix = "应如何查询或办理？"
    elif any(mark in text for mark in ("说明书", "解放行", "app", "APP", "绑定", "解绑", "品牌", "型号", "后台")):
        suffix = "应如何操作或处理？"
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
