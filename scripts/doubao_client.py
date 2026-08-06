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


def provider_details(model: str) -> tuple[str, str]:
    normalized = model.lower()
    if normalized.startswith("kimi"):
        return "kimi", "Kimi（火山方舟）"
    if normalized.startswith("deepseek"):
        return "deepseek", "DeepSeek（火山方舟）"
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


def agent_status() -> dict[str, Any]:
    load_local_env()
    model = os.getenv("ARK_DEEP_MODEL", os.getenv("ARK_MODEL", "kimi-k3"))
    fast_model = os.getenv(
        "ARK_FAST_MODEL", "doubao-seed-2-0-lite-260215"
    )
    provider, provider_name = provider_details(model)
    return {
        "provider": provider,
        "provider_name": provider_name,
        "configured": bool(os.getenv("ARK_API_KEY", "").strip()),
        "model": model,
        "modes": {
            "fast": {"model": fast_model, "label": "快速问答"},
            "deep": {"model": model, "label": "深度诊断"},
        },
        "base_url": os.getenv(
            "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3"
        ),
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


def open_json_with_retry(request: Request, timeout: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.8)
                continue
            raise
    raise RuntimeError(f"火山方舟网络调用失败：{last_error}")


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
    timeout: int = 90,
) -> dict[str, Any]:
    load_local_env()
    api_key = os.getenv("ARK_API_KEY", "").strip()
    mode = "fast" if mode == "fast" else "deep"
    deep_model = os.getenv("ARK_DEEP_MODEL", os.getenv("ARK_MODEL", "kimi-k3"))
    model = (
        os.getenv("ARK_FAST_MODEL", "doubao-seed-2-0-lite-260215")
        if mode == "fast"
        else deep_model
    ).strip()
    provider, _ = provider_details(model)
    base_url = os.getenv(
        "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3"
    ).rstrip("/")
    if not api_key:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "error": "ARK_API_KEY 未配置",
        }

    source_limit = 5 if mode == "fast" else 10
    excerpt_limit = 600 if mode == "fast" else 1100
    history_limit = 1600 if mode == "fast" else 4200
    evidence = (
        _evidence_text(
            sources,
            triples,
            source_limit=source_limit,
            excerpt_limit=excerpt_limit,
            triple_limit=(10 if mode == "fast" else 20),
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
    prompt = (
        role_prompt + "\n"
        "严格按以下纯文本格式输出，不要输出JSON或Markdown代码块：\n"
        "【回答】\n完整答案\n"
        "【处理步骤】\n1. 步骤一\n2. 步骤二\n"
        "【相关问题】\n1. 相关问题一\n2. 相关问题二\n"
        "【安全提示】\n安全注意事项。\n\n"
        f"车型：{vehicle_series or '未指定'}\n"
        f"场景：{scene or '未指定'}\n"
        + (
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
        + f"最近对话：{history[-history_limit:] if history else '无'}\n"
        f"用户问题：{question}\n\n"
        f"企业知识库证据：\n{evidence or '未检索到证据'}"
    )
    request_body = {
        "model": model,
        "input": prompt,
        "thinking": {"type": "disabled"},
        "max_output_tokens": 1600 if mode == "fast" else 2600,
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
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        friendly_error = f"火山方舟 HTTP {exc.code}: {detail}"
        if "AccountOverdueError" in detail:
            friendly_error = "火山方舟账户欠费或余额异常，请在控制台处理后重试"
        elif exc.code == 401:
            friendly_error = "火山方舟 API Key 无效或已失效"
        elif exc.code == 429:
            friendly_error = "火山方舟请求过于频繁，请稍后重试"
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
    timeout: int = 90,
) -> Iterator[dict[str, Any]]:
    load_local_env()
    api_key = os.getenv("ARK_API_KEY", "").strip()
    mode = "fast" if mode == "fast" else "deep"
    deep_model = os.getenv("ARK_DEEP_MODEL", os.getenv("ARK_MODEL", "kimi-k3"))
    model = (
        os.getenv("ARK_FAST_MODEL", "doubao-seed-2-0-lite-260215")
        if mode == "fast"
        else deep_model
    ).strip()
    provider, _ = provider_details(model)
    base_url = os.getenv(
        "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3"
    ).rstrip("/")
    if not api_key:
        yield {
            "type": "done",
            "ok": False,
            "provider": provider,
            "model": model,
            "mode": mode,
            "error": "ARK_API_KEY 未配置",
        }
        return

    source_limit = 5 if mode == "fast" else 10
    excerpt_limit = 600 if mode == "fast" else 1100
    history_limit = 1600 if mode == "fast" else 4200
    evidence = (
        _evidence_text(
            sources,
            triples,
            source_limit=source_limit,
            excerpt_limit=excerpt_limit,
            triple_limit=(10 if mode == "fast" else 20),
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
    prompt = (
        role_prompt + "\n"
        "严格按以下纯文本格式输出：\n"
        "【回答】\n完整答案\n"
        "【处理步骤】\n1. 步骤一\n2. 步骤二\n"
        "【相关问题】\n1. 相关问题一\n2. 相关问题二\n"
        "【安全提示】\n安全注意事项。\n"
        + (
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
        + f"车型：{vehicle_series or '未指定'}\n"
        + f"场景：{scene or '未指定'}\n"
        + f"最近对话：{history[-history_limit:] if history else '无'}\n"
        + f"用户问题：{question}\n\n"
        + citation_prompt
    )
    body = {
        "model": model,
        "input": prompt,
        "thinking": {"type": "disabled"},
        "max_output_tokens": 1600 if mode == "fast" else 2600,
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
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        yield {
            "type": "done",
            "ok": False,
            "provider": provider,
            "model": model,
            "mode": mode,
            "error": f"火山方舟 HTTP {exc.code}: {detail}",
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
