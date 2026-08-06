from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parents[1]


def load_local_env() -> None:
    """Load local secrets without overriding process environment variables."""
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
        if key:
            os.environ.setdefault(key, value)


load_local_env()


def agent_status() -> dict[str, Any]:
    load_local_env()
    return {
        "provider": "deepseek",
        "configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    }


def _evidence_text(
    sources: list[dict[str, Any]], triples: list[dict[str, Any]]
) -> str:
    sections: list[str] = []
    for index, source in enumerate(sources[:8], start=1):
        excerpt = str(source.get("excerpt", ""))[:1200]
        sections.append(
            f"[资料{index}] 文件：{source.get('relative_path', '')}\n"
            f"位置：{source.get('source_locator', '')}\n内容：{excerpt}"
        )
    if triples:
        graph_lines = []
        for item in triples[:20]:
            graph_lines.append(
                f"- {item.get('subject', '')} --{item.get('predicate_name', item.get('predicate', ''))}--> "
                f"{item.get('object', '')}"
            )
        sections.append("[知识图谱关系]\n" + "\n".join(graph_lines))
    return "\n\n".join(sections)


def generate_grounded_answer(
    *,
    question: str,
    sources: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    history: str = "",
    vehicle_series: str = "",
    scene: str = "",
    timeout: int = 60,
) -> dict[str, Any]:
    load_local_env()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if not api_key:
        return {
            "ok": False,
            "provider": "deepseek",
            "model": model,
            "error": "DEEPSEEK_API_KEY 未配置",
        }

    evidence = _evidence_text(sources, triples)
    system_prompt = (
        "你是中重卡用、养、修、保知识问答智能体。只能依据用户提供的企业知识库证据回答，"
        "不得编造故障原因、零件参数、维修步骤或适用车型。证据不足时明确说明，并提出需要补充的"
        "车型、故障码或故障现象。涉及制动、高压电、转向等安全关键系统时，必须建议由合格维修"
        "人员按有效版维修手册操作。答案引用资料时使用[资料1]这样的编号。"
        "请只输出合法JSON对象，字段必须为answer、related_questions、solution_steps、safety_notice。"
        "related_questions和solution_steps必须是字符串数组，其余字段为字符串。"
    )
    user_prompt = (
        f"车型：{vehicle_series or '未指定'}\n"
        f"场景：{scene or '未指定'}\n"
        f"最近对话：{history[-4000:] if history else '无'}\n"
        f"用户问题：{question}\n\n"
        f"企业知识库证据：\n{evidence or '未检索到证据'}"
    )
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 1800,
    }
    request = Request(
        f"{base_url}/chat/completions",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return {
            "ok": True,
            "provider": "deepseek",
            "model": payload.get("model", model),
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
        return {
            "ok": False,
            "provider": "deepseek",
            "model": model,
            "error": f"DeepSeek HTTP {exc.code}: {detail}",
        }
    except (URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "provider": "deepseek",
            "model": model,
            "error": f"DeepSeek 调用失败：{type(exc).__name__}: {exc}",
        }
