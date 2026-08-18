from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parents[1]
_ARK_QUOTA_ERRORS = {"fast": "", "deep": ""}


def provider_details(model: str) -> tuple[str, str]:
    normalized = model.lower()
    if normalized.startswith("kimi"):
        return "kimi", "Kimi（火山方舟）"
    if normalized.startswith("deepseek"):
        return "deepseek", "DeepSeek（火山方舟）"
    if normalized.startswith("glm"):
        return "glm", "GLM（火山方舟）"
    return "doubao", "豆包方舟"


def load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def answer_mode_config(mode: str) -> dict[str, Any]:
    """Return isolated credentials and endpoint settings for one answer mode."""
    load_local_env()
    normalized = "fast" if mode == "fast" else "deep"
    deep_uses_fast = normalized == "deep" and os.getenv(
        "ARK_DEEP_USE_FAST", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    prefix = "ARK_FAST" if normalized == "fast" or deep_uses_fast else "ARK_DEEP"
    default_model = (
        "doubao-seed-2-0-lite-260215"
        if normalized == "fast" or deep_uses_fast
        else "doubao-seed-2-1-pro-260628"
    )
    model = os.getenv(f"{prefix}_MODEL", default_model).strip()
    api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
    base_url = os.getenv(
        f"{prefix}_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    ).rstrip("/")
    return {
        "mode": normalized,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "shared_with_fast": deep_uses_fast,
    }


def agent_status() -> dict[str, Any]:
    fast = answer_mode_config("fast")
    deep = answer_mode_config("deep")
    provider, provider_name = provider_details(deep["model"])
    return {
        "provider": provider,
        "provider_name": provider_name,
        "configured": bool(fast["api_key"] or deep["api_key"]),
        "all_modes_configured": bool(fast["api_key"] and deep["api_key"]),
        "model": deep["model"],
        "modes": {
            "fast": {
                "model": fast["model"],
                "label": "快速问答",
                "configured": bool(fast["api_key"]),
                "base_url": fast["base_url"],
            },
            "deep": {
                "model": deep["model"],
                "label": "深度诊断",
                "configured": bool(deep["api_key"]),
                "base_url": deep["base_url"],
            },
        },
        "base_url": deep["base_url"],
    }


def _evidence_text(
    sources: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    *,
    source_limit: int = 8,
    excerpt_limit: int = 900,
    triple_limit: int = 16,
) -> str:
    sections: list[str] = []
    for index, source in enumerate(sources[:source_limit], start=1):
        sections.append(
            f"[资料{index}] 文件：{source.get('relative_path', '')}\n"
            f"位置：{source.get('source_locator', '')}\n"
            f"内容：{str(source.get('excerpt', ''))[:excerpt_limit]}"
        )
    if triples:
        graph_lines = [
            f"- {item.get('subject', '')} --"
            f"{item.get('predicate_name', item.get('predicate', ''))}--> "
            f"{item.get('object', '')}"
            for item in triples[:triple_limit]
        ]
        sections.append("[知识图谱关系]\n" + "\n".join(graph_lines))
    return "\n\n".join(sections)


def _response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts: list[str] = []
    for output in payload.get("output", []):
        if output.get("type") != "message":
            continue
        for content in output.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _parse_json_content(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _clean_list_item(value: str) -> str:
    return re.sub(
        r"^\s*(?:[-*•]+|\d+[.、)）])\s*", "", value.strip()
    ).strip()


def _parse_sectioned_content(content: str) -> dict[str, Any]:
    text = content.strip()
    labels = ["回答", "处理步骤", "相关问题", "安全提示"]
    sections: dict[str, str] = {}
    for index, label in enumerate(labels):
        following = "|".join(re.escape(item) for item in labels[index + 1 :])
        end_pattern = rf"(?=【(?:{following})】|$)" if following else r"$"
        match = re.search(
            rf"【{re.escape(label)}】\s*(.*?){end_pattern}",
            text,
            flags=re.S,
        )
        sections[label] = match.group(1).strip() if match else ""
    if not sections["回答"]:
        sections["回答"] = text

    def lines(label: str, limit: int) -> list[str]:
        values = []
        for line in sections[label].splitlines():
            cleaned = _clean_list_item(line)
            if cleaned and cleaned not in values:
                values.append(cleaned)
            if len(values) >= limit:
                break
        return values

    return {
        "answer": sections["回答"],
        "solution_steps": lines("处理步骤", 10),
        "related_questions": lines("相关问题", 6),
        "safety_notice": sections["安全提示"],
    }


def parse_agent_content(content: str) -> dict[str, Any]:
    try:
        return _parse_json_content(content)
    except (json.JSONDecodeError, ValueError):
        return _parse_sectioned_content(content)


def open_json_with_retry(
    request: Request, timeout: int, attempts: int = 2
) -> dict[str, Any]:
    last_error: Exception | None = None
    retry_count = max(1, int(attempts))
    for attempt in range(retry_count):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < retry_count:
                time.sleep(min(1.0 * (2**attempt), 5.0))
                continue
            raise
    raise RuntimeError(f"火山方舟网络调用失败：{last_error}")


def ark_http_error_message(exc: HTTPError) -> tuple[str, str]:
    """Return a user-facing Ark error and its machine-readable code."""
    detail = exc.read().decode("utf-8", errors="replace")[:2000]
    code = ""
    message = ""
    try:
        payload = json.loads(detail)
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            code = str(error.get("code", ""))
            message = str(error.get("message", ""))
    except json.JSONDecodeError:
        pass
    if code == "AccountQuotaExceeded":
        reset_match = re.search(r"reset at ([^.]+)", message, flags=re.I)
        reset_at = reset_match.group(1).strip() if reset_match else "套餐周期结束时"
        return (
            f"火山方舟月度套餐额度已用完，将于 {reset_at} 重置；"
            "请升级套餐、购买额外额度或等待重置",
            code,
        )
    if code == "AccountOverdueError" or "AccountOverdueError" in detail:
        return "火山方舟账户欠费或余额异常，请在控制台处理后重试", code
    if exc.code == 401:
        return "火山方舟 API Key 无效或已失效", code
    if exc.code == 429:
        return "火山方舟请求频率或Token速率已达到上限，请稍后重试", code
    return f"火山方舟 HTTP {exc.code}: {message or detail[:500]}", code


def _focused_answer_policy(answer_target: str, mode: str) -> tuple[str, str, int]:
    target = str(answer_target or "").strip()
    labels = {
        "overview": "含义、适用范围和关键判断标准",
        "cause": "可能原因及判断依据",
        "solution": "排查步骤和对应处理方案",
        "safety": "适用条件、风险和安全注意事项",
    }
    if target not in labels:
        return "", "", 0
    if target == "solution":
        output_format = (
            "严格按以下纯文本格式输出，不要输出JSON或Markdown代码块：\n"
            "【回答】\n一句话说明处理原则\n"
            "【处理步骤】\n1. 检查与判断\n2. 对应处理\n"
            "【安全提示】\n必要的安全注意事项。\n\n"
        )
    elif target == "safety":
        output_format = (
            "严格按以下纯文本格式输出，不要输出JSON或Markdown代码块：\n"
            "【回答】\n适用条件和风险结论\n"
            "【安全提示】\n禁止事项和安全注意事项。\n\n"
        )
    else:
        output_format = (
            "严格按以下纯文本格式输出，不要输出JSON或Markdown代码块：\n"
            "【回答】\n针对所选重点的答案\n"
            "【安全提示】\n仅在确有安全风险时输出。\n\n"
        )
    length = "150至350" if mode == "fast" else "300至600"
    guidance = (
        f"用户已确认本轮只需要“{labels[target]}”。不要重复输出完整固定模板，"
        f"不要生成相关问题，不要扩展到其他回答方向；正文以{length}个汉字为宜。"
        + (
            "处理步骤应按顺序说明检查对象、判断结果和对应动作。\n"
            if target == "solution"
            else "结论按优先级简洁排列，并说明资料依据。\n"
        )
    )
    return output_format, guidance, (900 if mode == "fast" else 1400)


def generate_grounded_answer(
    *,
    question: str,
    sources: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    history: str = "",
    vehicle_series: str = "",
    scene: str = "",
    mode: str = "deep",
    knowledge_only: bool = True,
    answer_target: str = "",
    timeout: int = 90,
) -> dict[str, Any]:
    mode = "fast" if mode == "fast" else "deep"
    mode_config = answer_mode_config(mode)
    api_key = mode_config["api_key"]
    model = mode_config["model"]
    provider, _ = provider_details(model)
    base_url = mode_config["base_url"]
    if not api_key:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "error": f"{mode.upper()} 模型 API Key 未配置",
        }

    source_limit = 5 if mode == "fast" else 12
    excerpt_limit = 600 if mode == "fast" else 1000
    history_limit = 1600 if mode == "fast" else 3600
    evidence = (
        _evidence_text(
            sources,
            triples,
            source_limit=source_limit,
            excerpt_limit=excerpt_limit,
            triple_limit=(10 if mode == "fast" else 30),
        )
        if knowledge_only
        else ""
    )
    role_prompt = (
        "你是中重卡用、养、修、保知识问答智能体。只能依据下面给出的企业知识库证据回答，"
        "不得编造故障原因、零件参数、维修步骤或适用车型。证据不足时必须明确说明，并提出需要"
        "补充的车型、故障码或故障现象。涉及制动、高压电、转向等安全关键系统，必须建议由合格"
        "维修人员按有效版维修手册操作。引用时使用[资料1]这样的编号。"
        if knowledge_only
        else (
            "你是一个友好的通用问答助手。当前问题不属于企业车辆专业知识查询，可以依据通用知识"
            "自然回答，不要因为企业知识库没有资料就拒答，也不要虚构企业内部资料。若用户询问你是"
            "什么模型，说明当前接入的模型名称；若问题属于车辆用养修保专业问题，应提醒用户切换到"
            "对应专业查询后再给出基于资料的结论。"
        )
    )
    citation_prompt = (
        "企业知识库证据：\n" + (evidence or "未检索到证据")
        if knowledge_only
        else "本轮不使用企业知识库证据。"
    )
    deep_synthesis_prompt = (
        "深度诊断要求：综合不同文档的证据交叉判断，不要只复述排名第一的资料；"
        "按系统或部件归纳可能原因并标明优先级，说明每项原因的检查对象、检查方法、"
        "正常与异常判断标准、异常后的处理以及维修后的复测方法；资料存在版本或车型差异时必须明确指出。\n"
        if mode == "deep" and knowledge_only and answer_target in {"", "full"}
        else ""
    )
    focused_format, focused_guidance, focused_max_tokens = _focused_answer_policy(
        answer_target, mode
    )
    output_format = focused_format or (
        "严格按以下纯文本格式输出，不要输出JSON或Markdown代码块：\n"
        "【回答】\n完整答案\n"
        "【处理步骤】\n1. 步骤一\n2. 步骤二\n"
        "【相关问题】\n1. 相关问题一\n2. 相关问题二\n"
        "【安全提示】\n安全注意事项。\n\n"
    )
    answer_guidance = focused_guidance or (
        (
            "通用快速回答要求：不要寒暄，第一句话直接回答；问题不需要步骤时不要强行添加。"
            "可以使用自然语言和常识，不要输出资料编号。\n"
            if not knowledge_only
            else "快速模式要求：不要寒暄，第一句话直接给结论。回答段必须包含判断依据以及按优先级排列的"
            "3至5项可能原因，并结合资料编号解释原因；处理步骤必须给出4至6步，每一步分别说明"
            "检查对象、检查方法、正常或异常的判断以及异常后的处理。相关问题给出3项。"
            "除非证据不足，完整回答不得只写一小段，正文以500至800个汉字为宜。\n"
        )
        if mode == "fast"
        else (
            "通用深度回答要求：完整解释问题，必要时给出例子或步骤，但保持自然简洁，不要输出资料编号。\n"
            if not knowledge_only
            else "深度模式要求：完整说明结论、可能原因及优先级、逐步检查方法、判断标准、"
            "处理建议和仍需确认的信息；引用证据编号，但避免大段重复证据原文。\n"
        )
    )
    prompt = (
        role_prompt + "\n" + deep_synthesis_prompt
        + output_format
        + f"车型：{vehicle_series or '未指定'}\n"
        f"场景：{scene or '未指定'}\n"
        + answer_guidance
        + f"最近对话：{history[-history_limit:] if history else '无'}\n"
        f"用户问题：{question}\n\n"
        f"企业知识库证据：\n{evidence or '未检索到证据'}"
    )
    request_body = {
        "model": model,
        "input": prompt,
        "thinking": {
            "type": "disabled"
            if mode == "fast" or mode_config["shared_with_fast"]
            else "enabled"
        },
        "max_output_tokens": focused_max_tokens or (
            (1600 if knowledge_only else 700)
            if mode == "fast"
            else (2200 if knowledge_only else 1000)
        ),
    }
    request = Request(
        f"{base_url}/responses",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        payload = open_json_with_retry(request, timeout)
        content = _response_text(payload)
        parsed = parse_agent_content(content)
        return {
            "ok": True,
            "provider": provider,
            "model": payload.get("model", model),
            "mode": mode,
            "answer": str(parsed.get("answer", "")).strip(),
            "related_questions": [
                str(item) for item in parsed.get("related_questions", [])[:6]
            ],
            "solution_steps": [
                str(item) for item in parsed.get("solution_steps", [])[:10]
            ],
            "safety_notice": str(parsed.get("safety_notice", "")).strip(),
            "usage": payload.get("usage", {}),
        }
    except HTTPError as exc:
        friendly_error, _ = ark_http_error_message(exc)
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "error": friendly_error,
        }
    except (URLError, TimeoutError, KeyError, ValueError) as exc:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "error": f"火山方舟调用失败：{type(exc).__name__}: {exc}",
        }


def generate_batch_diagnosis(
    *,
    symptom: str,
    sources: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    vehicle_series: str = "JH6",
    task_type: str = "symptom_diagnosis",
    task_label: str = "故障诊断",
    mode: str = "fast",
    timeout: int = 120,
) -> dict[str, Any]:
    """Return cause-to-repair pairs for spreadsheet batch diagnosis."""
    mode = "deep" if mode == "deep" else "fast"
    global _ARK_QUOTA_ERRORS
    if _ARK_QUOTA_ERRORS[mode]:
        return {
            "ok": False,
            "provider": "ark",
            "model": "",
            "error": _ARK_QUOTA_ERRORS[mode],
        }
    mode_config = answer_mode_config(mode)
    api_key = mode_config["api_key"]
    model = mode_config["model"]
    provider, _ = provider_details(model)
    base_url = mode_config["base_url"]
    if not api_key:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "error": f"{mode.upper()} 模型 API Key 未配置",
        }

    evidence = _evidence_text(
        sources,
        triples,
        source_limit=(6 if mode == "fast" else 14),
        excerpt_limit=(750 if mode == "fast" else 1200),
        triple_limit=(12 if mode == "fast" else 32),
    )
    task_guidance = {
        "fault_code": (
            "先给出故障码定义、涉及控制器/部件和触发条件；每条cause写可能根因，"
            "repair_plan写对应诊断树与清码复测步骤。"
        ),
        "symptom_diagnosis": (
            "输出3至6组按优先级排序的可能原因；每条cause写一种原因，"
            "repair_plan写只针对该原因的检查、判断、维修和复测步骤。"
        ),
        "maintenance": (
            "cause填写保养项目或适用条件，repair_plan填写对应保养周期、材料要求、"
            "操作步骤和完成后的检查；不得把保养问题描述成车辆故障。"
        ),
        "warranty": (
            "cause填写保用结论或适用/不适用条件，repair_plan填写对应的凭证核验、"
            "进站鉴定和索赔办理步骤；没有明确条款时必须说明资料不足。"
        ),
        "usage": (
            "cause填写功能说明、使用前提或当前问题原因，repair_plan填写对应的操作步骤、"
            "注意事项和操作结果确认。"
        ),
        "service_technical": (
            "cause填写服务事项或办理条件，repair_plan填写对应联系、预约、进站或资料准备步骤；"
            "不得虚构电话、地址或收费标准。"
        ),
        "drawing": (
            "cause填写所需图纸/系统或图纸适用条件，repair_plan填写查图、定位回路/部件和"
            "按图检查的步骤。"
        ),
        "vin": (
            "cause填写VIN识别结果或字段含义，repair_plan填写车辆信息核验与后续查询步骤；"
            "不得虚构车辆静态字段。"
        ),
        "general": (
            "根据资料给出问题要点；cause填写结论或关键说明，repair_plan填写对应处理或查询步骤。"
        ),
    }.get(task_type, "每一条结论必须有一条与之对应的处理方案。")
    prompt = f"""你是商用车企业知识库的工程师诊断助手。车辆系列为{vehicle_series or '未指定'}。
系统已将问题识别为“{task_label}”（任务代码：{task_type}）。用户问题：{symptom}。
客服记录已经转换为工程师提问口吻。请使用工程师口吻回答，避免“客户来电、用户咨询、建议联系客户”等客服话术；直接描述系统、部件、检查项目和处理动作。
只能依据下面的企业知识库证据回答，不得编造参数、故障码、保用条款、服务网点或维修结论。资料不足时在summary中明确说明缺少什么信息。

当前类别回答要求：{task_guidance}

通用要求：
1. summary只写最关键的诊断结论，一句话且不超过50个汉字；不要复述来电时间、电话、客服处理过程或问题背景。
2. 每一条cause必须有且只能有一条与之对应的repair_plan，不要把多个原因或多个方案混在同一项。
3. repair_plan应为可执行的有序步骤，尽量包含检查/查询对象、方法、判断依据和后续处理。
4. verification填写结果验证或信息核验方法。
5. evidence填写所依据的资料编号，例如“资料1、资料3”。
6. 安全关键系统需提示由合格维修人员按有效版手册操作。

严格输出JSON，不要输出Markdown代码块或JSON以外文字：
{{
  "summary": "总体判断或资料不足说明",
  "pairs": [
    {{
      "cause": "故障原因",
      "repair_plan": "1. 检查……；2. 判断……；3. 处理……",
      "verification": "验证该原因的方法",
      "evidence": "资料1"
    }}
  ],
  "safety_notice": "安全提示"
}}

企业知识库证据：
{evidence or '未检索到证据'}"""
    request_body = {
        "model": model,
        "input": prompt,
        "thinking": {
            "type": "disabled"
            if mode == "fast" or mode_config["shared_with_fast"]
            else "enabled"
        },
        "max_output_tokens": 1800 if mode == "fast" else 2600,
    }
    request = Request(
        f"{base_url}/responses",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        payload: dict[str, Any] | None = None
        for attempt in range(4):
            try:
                # Batch jobs must survive occasional Ark no-response and
                # connection-reset errors without marking the row as failed.
                payload = open_json_with_retry(request, timeout, attempts=3)
                break
            except HTTPError as exc:
                if exc.code != 429 or attempt >= 3:
                    raise
                friendly_error, error_code = ark_http_error_message(exc)
                if error_code == "AccountQuotaExceeded":
                    _ARK_QUOTA_ERRORS[mode] = friendly_error
                    return {
                        "ok": False,
                        "provider": provider,
                        "model": model,
                        "error": friendly_error,
                    }
                time.sleep((2, 5, 10)[attempt])
        if payload is None:
            raise RuntimeError("批量诊断请求未完成")
        parsed = _parse_json_content(_response_text(payload))
        pairs: list[dict[str, str]] = []
        for item in parsed.get("pairs", [])[:8]:
            if not isinstance(item, dict):
                continue
            cause = str(item.get("cause", "")).strip()
            repair_plan = str(item.get("repair_plan", "")).strip()
            if not cause or not repair_plan:
                continue
            pairs.append(
                {
                    "cause": cause,
                    "repair_plan": repair_plan,
                    "verification": str(item.get("verification", "")).strip(),
                    "evidence": str(item.get("evidence", "")).strip(),
                }
            )
        if not pairs:
            raise ValueError("模型未返回有效的原因—维修方案对应项")
        return {
            "ok": True,
            "provider": provider,
            "model": payload.get("model", model),
            "mode": mode,
            "summary": str(parsed.get("summary", "")).strip(),
            "pairs": pairs,
            "safety_notice": str(parsed.get("safety_notice", "")).strip(),
            "usage": payload.get("usage", {}),
        }
    except HTTPError as exc:
        error, error_code = ark_http_error_message(exc)
        if error_code == "AccountQuotaExceeded":
            _ARK_QUOTA_ERRORS[mode] = error
        return {"ok": False, "provider": provider, "model": model, "error": error}
    except (URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "error": f"批量诊断调用失败：{type(exc).__name__}: {exc}",
        }


def _stream_request_events(request: Request, timeout: int) -> Iterator[dict[str, Any]]:
    with urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield payload


def generate_grounded_answer_stream(
    *,
    question: str,
    sources: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    history: str = "",
    vehicle_series: str = "",
    scene: str = "",
    mode: str = "fast",
    knowledge_only: bool = True,
    answer_target: str = "",
    timeout: int = 90,
) -> Iterator[dict[str, Any]]:
    mode = "fast" if mode == "fast" else "deep"
    mode_config = answer_mode_config(mode)
    api_key = mode_config["api_key"]
    model = mode_config["model"]
    provider, _ = provider_details(model)
    base_url = mode_config["base_url"]
    if not api_key:
        yield {
            "type": "done",
            "ok": False,
            "provider": provider,
            "model": model,
            "mode": mode,
            "error": f"{mode.upper()} 模型 API Key 未配置",
        }
        return

    source_limit = 5 if mode == "fast" else 12
    excerpt_limit = 600 if mode == "fast" else 1000
    history_limit = 1600 if mode == "fast" else 3600
    evidence = (
        _evidence_text(
            sources,
            triples,
            source_limit=source_limit,
            excerpt_limit=excerpt_limit,
            triple_limit=(10 if mode == "fast" else 30),
        )
        if knowledge_only
        else ""
    )
    role_prompt = (
        "你是中重卡用、养、修、保知识问答智能体。只能依据下面给出的企业知识库证据回答，"
        "不得编造故障原因、零件参数、维修步骤或适用车型。证据不足时必须明确说明。"
        "涉及制动、高压电、转向等安全关键系统，必须建议由合格维修人员按有效版维修手册操作。"
        "引用时使用[资料1]这样的编号。"
        if knowledge_only
        else (
            "你是一个友好的通用问答助手。当前问题不属于企业车辆专业知识查询，可以依据通用知识"
            "自然回答，不要因为企业知识库没有资料就拒答，也不要虚构企业内部资料。若用户询问你是"
            "什么模型，说明当前接入的模型名称；若问题属于车辆用养修保专业问题，应提醒用户切换到"
            "对应专业查询后再给出基于资料的结论。"
        )
    )
    citation_prompt = (
        "企业知识库证据：\n" + (evidence or "未检索到证据")
        if knowledge_only
        else "本轮不使用企业知识库证据。"
    )
    deep_synthesis_prompt = (
        "深度诊断要求：综合不同文档的证据交叉判断，不要只复述排名第一的资料；"
        "按系统或部件归纳可能原因并标明优先级，说明每项原因的检查对象、检查方法、"
        "正常与异常判断标准、异常后的处理以及维修后的复测方法；资料存在版本或车型差异时必须明确指出。\n"
        if mode == "deep" and knowledge_only and answer_target in {"", "full"}
        else ""
    )
    focused_format, focused_guidance, focused_max_tokens = _focused_answer_policy(
        answer_target, mode
    )
    output_format = focused_format or (
        "严格按以下纯文本格式输出：\n"
        "【回答】\n完整答案\n"
        "【处理步骤】\n1. 步骤一\n2. 步骤二\n"
        "【相关问题】\n1. 相关问题一\n2. 相关问题二\n"
        "【安全提示】\n安全注意事项。\n"
    )
    answer_guidance = focused_guidance or (
        (
            "通用快速回答要求：不要寒暄，第一句话直接回答；问题不需要步骤时不要强行添加。"
            "可以使用自然语言和常识，不要输出资料编号。\n"
            if not knowledge_only
            else "快速模式要求：不要寒暄，第一句话直接给结论。回答段必须包含判断依据以及按优先级排列的"
            "3至5项可能原因，并结合资料编号解释原因；处理步骤必须给出4至6步，每一步分别说明"
            "检查对象、检查方法、正常或异常的判断以及异常后的处理。相关问题给出3项。"
            "除非证据不足，完整回答不得只写一小段，正文以500至800个汉字为宜。\n"
        )
        if mode == "fast"
        else (
            "通用深度回答要求：完整解释问题，必要时给出例子或步骤，但保持自然简洁，不要输出资料编号。\n"
            if not knowledge_only
            else "深度模式要求：完整说明结论、可能原因及优先级、逐步检查方法、判断标准、"
            "处理建议和仍需确认的信息；引用证据编号，但避免大段重复证据原文。\n"
        )
    )
    prompt = (
        role_prompt + "\n" + deep_synthesis_prompt
        + output_format
        + answer_guidance
        + f"车型：{vehicle_series or '未指定'}\n"
        + f"场景：{scene or '未指定'}\n"
        + f"最近对话：{history[-history_limit:] if history else '无'}\n"
        + f"用户问题：{question}\n\n"
        + citation_prompt
    )
    body = {
        "model": model,
        "input": prompt,
        "thinking": {
            "type": "disabled"
            if mode == "fast" or mode_config["shared_with_fast"]
            else "enabled"
        },
        "max_output_tokens": focused_max_tokens or (
            (1600 if knowledge_only else 700)
            if mode == "fast"
            else (2200 if knowledge_only else 1000)
        ),
        "stream": True,
    }
    request = Request(
        f"{base_url}/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    content_parts: list[str] = []
    completed_response: dict[str, Any] = {}
    try:
        for event in _stream_request_events(request, timeout):
            event_type = str(event.get("type", ""))
            if event_type.endswith("output_text.delta"):
                delta = str(event.get("delta", ""))
                if delta:
                    content_parts.append(delta)
                    yield {"type": "delta", "text": delta}
            elif event_type in {"response.completed", "response.done"}:
                completed_response = dict(event.get("response") or event)
            elif event_type in {"error", "response.failed"}:
                detail = event.get("error") or event
                raise RuntimeError(str(detail))
        content = _response_text(completed_response) or "".join(content_parts)
        parsed = parse_agent_content(content)
        yield {
            "type": "done",
            "ok": True,
            "provider": provider,
            "model": completed_response.get("model", model),
            "mode": mode,
            "answer": str(parsed.get("answer", "")).strip(),
            "related_questions": [
                str(item) for item in parsed.get("related_questions", [])[:6]
            ],
            "solution_steps": [
                str(item) for item in parsed.get("solution_steps", [])[:10]
            ],
            "safety_notice": str(parsed.get("safety_notice", "")).strip(),
            "usage": completed_response.get("usage", {}),
        }
    except HTTPError as exc:
        friendly_error, _ = ark_http_error_message(exc)
        yield {
            "type": "done",
            "ok": False,
            "provider": provider,
            "model": model,
            "mode": mode,
            "error": friendly_error,
        }
    except (URLError, TimeoutError, RuntimeError, ValueError) as exc:
        yield {
            "type": "done",
            "ok": False,
            "provider": provider,
            "model": model,
            "mode": mode,
            "error": f"火山方舟流式调用失败：{type(exc).__name__}: {exc}",
        }
