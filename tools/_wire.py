"""看图请求的线格式适配。

同一件事——一张图换一段文字——四家的端点、鉴权头、请求体和取文字的路径都不一样。
读图工具和表情包打标共用这一份，格式才不会两边各写一遍然后慢慢分叉。
"""
from __future__ import annotations

from typing import Any, NamedTuple

WIRE_OPENAI = "openai"
WIRE_OPENAI_RESPONSES = "openai-responses"
WIRE_ANTHROPIC = "anthropic"
WIRE_GEMINI = "gemini"

# provider 名 → 线格式。唯一权威是 providers/registry.wire_formats()，但工具是只吃标准库的
# 子进程，导不了那个包（它依赖 httpx），所以在这里存一份，一致性由 test_wire.py 钉住。
PROVIDER_WIRES: dict[str, str] = {
    "openai": WIRE_OPENAI,
    "deepseek": WIRE_OPENAI,
    "openai-responses": WIRE_OPENAI_RESPONSES,
    "anthropic": WIRE_ANTHROPIC,
    "gemini": WIRE_GEMINI,
}

# 线格式 → 图片格式接受集用哪一套。_image.py 的表按这几个名字建。
_WIRE_FAMILIES: dict[str, str] = {
    WIRE_OPENAI: "openai",
    WIRE_OPENAI_RESPONSES: "openai",
    WIRE_ANTHROPIC: "anthropic",
    WIRE_GEMINI: "gemini",
}

_ANTHROPIC_DOMAIN = "api.anthropic.com"
_GEMINI_DOMAIN = "googleapis.com"

# 各家的官方根，和 providers/*.py 的 default_base_url 同值。
_DEFAULT_URLS: dict[str, str] = {
    WIRE_OPENAI: "https://api.openai.com/v1",
    WIRE_OPENAI_RESPONSES: "https://api.openai.com/v1",
    WIRE_ANTHROPIC: "https://api.anthropic.com",
    WIRE_GEMINI: "https://generativelanguage.googleapis.com",
}


def default_base_url(wire: str) -> str:
    return _DEFAULT_URLS.get(str(wire or "").strip().lower(), _DEFAULT_URLS[WIRE_OPENAI])


def wire_for(provider: str) -> str:
    """这家按哪种格式发。认不出按 OpenAI 算——中转站绝大多数是这一套。"""
    return PROVIDER_WIRES.get(str(provider or "").strip().lower(), WIRE_OPENAI)


def family_for_wire(wire: str) -> str:
    return _WIRE_FAMILIES.get(str(wire or "").strip().lower(), "openai")


def normalize_base_url(wire: str, base_url: str) -> str:
    """把地址收拾成这套格式的根。

    OpenAI 系的版本段是根的一部分（`/v1/chat/completions`），另外两家的版本段在路径里由
    端点自己拼，地址上再带一个就成了 `/v1/v1/messages`。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    if wire in (WIRE_ANTHROPIC, WIRE_GEMINI):
        for suffix in ("/v1beta", "/v1"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base
    # OpenAI 系保留旧行为：只填域名的配置一直靠这一步补全，改掉会让现有部署 404。
    return base if "/v1" in base else base + "/v1"


def endpoint(wire: str, base_url: str, model: str) -> str:
    base = str(base_url or "").rstrip("/")
    if wire == WIRE_GEMINI:
        return base + "/v1beta/models/" + str(model or "") + ":generateContent"
    if wire == WIRE_ANTHROPIC:
        return base + "/v1/messages"
    if wire == WIRE_OPENAI_RESPONSES:
        return base + "/responses"
    return base + "/chat/completions"


def headers(wire: str, base_url: str, api_key: str) -> dict[str, str]:
    """鉴权头。两家官方域名收自己的专用头，中转站一律 Bearer。"""
    key = str(api_key or "")
    base = str(base_url or "").lower()
    out = {"Content-Type": "application/json"}
    if wire == WIRE_GEMINI:
        if _GEMINI_DOMAIN in base:
            out["x-goog-api-key"] = key
        else:
            out["Authorization"] = "Bearer " + key
        return out
    if wire == WIRE_ANTHROPIC:
        out["anthropic-version"] = "2023-06-01"
        if _ANTHROPIC_DOMAIN in base:
            out["x-api-key"] = key
        else:
            out["Authorization"] = "Bearer " + key
        return out
    out["Authorization"] = "Bearer " + key
    return out


class VisionRequest(NamedTuple):
    url: str
    headers: dict[str, str]
    body: dict[str, Any]


def split_data_url(url: str) -> tuple[str, str]:
    """data:<mime>;base64,<data> → (mime, data)。不是这个形状返回两个空串。"""
    text = str(url or "")
    if not text.startswith("data:"):
        return ("", "")
    header, sep, payload = text.partition(",")
    if not sep or not payload:
        return ("", "")
    mime = header[len("data:"):].split(";", 1)[0].strip()
    return (mime or "application/octet-stream", payload)


def build_request(
    *,
    wire: str,
    base_url: str,
    api_key: str,
    model: str,
    image_urls: list[str],
    system: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> VisionRequest:
    """图片一律传 data URL，各家格式里怎么装由这里翻译。"""
    body = _body(
        wire, model=model, image_urls=image_urls, system=system,
        prompt=prompt, max_tokens=max_tokens, temperature=temperature,
    )
    return VisionRequest(
        url=endpoint(wire, base_url, model),
        headers=headers(wire, base_url, api_key),
        body=body,
    )


def _body(
    wire: str, *, model: str, image_urls: list[str], system: str,
    prompt: str, max_tokens: int, temperature: float,
) -> dict[str, Any]:
    if wire == WIRE_GEMINI:
        parts: list[dict[str, Any]] = []
        for url in image_urls:
            mime, data = split_data_url(url)
            if data:
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
        parts.append({"text": prompt})
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                # 看图只要一段描述，思考纯属浪费：它照样算进 maxOutputTokens，
                # 几百个思考 token 就能把正文顶掉，回来的是半截 JSON。
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    if wire == WIRE_ANTHROPIC:
        blocks: list[dict[str, Any]] = []
        for url in image_urls:
            mime, data = split_data_url(url)
            if data:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                })
        blocks.append({"type": "text", "text": prompt})
        body = {
            "model": model,
            # 这家 max_tokens 是必填，缺了直接 400。
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": blocks}],
        }
        if system:
            body["system"] = system
        return body

    if wire == WIRE_OPENAI_RESPONSES:
        content: list[dict[str, Any]] = [
            {"type": "input_image", "image_url": url} for url in image_urls
        ]
        content.append({"type": "input_text", "text": prompt})
        entries: list[dict[str, Any]] = []
        if system:
            entries.append({"role": "system", "content": [{"type": "input_text", "text": system}]})
        entries.append({"role": "user", "content": content})
        return {
            "model": model,
            "input": entries,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }

    content = [{"type": "image_url", "image_url": {"url": url}} for url in image_urls]
    content.append({"type": "text", "text": prompt})
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


# 关不掉思考的模型（Gemini 的 pro 系列就是）留出的余量，够思考加一段标签。
_RELAXED_MAX_TOKENS = 2048


def relax_body(wire: str, body: dict[str, Any], detail: str) -> dict[str, Any] | None:
    """按上游的抱怨退让一档，返回可以重发的请求体；没得退返回 None。

    只退让「我们主动加的优化」，不碰调用方给的参数 —— 退让不该改变结果的含义。
    """
    if wire != WIRE_GEMINI:
        return None
    config = body.get("generationConfig")
    if not isinstance(config, dict) or "thinkingConfig" not in config:
        return None
    if not any(word in detail.lower() for word in ("thinking", "thought", "budget")):
        return None
    relaxed = {**body, "generationConfig": {**config}}
    relaxed["generationConfig"].pop("thinkingConfig", None)
    # 思考关不掉就得给它留位置，否则正文一样会被顶掉。
    relaxed["generationConfig"]["maxOutputTokens"] = max(
        int(config.get("maxOutputTokens") or 0), _RELAXED_MAX_TOKENS,
    )
    return relaxed


def truncated(wire: str, payload: Any) -> bool:
    """输出是不是撞上限断掉了。断掉的正文多半是半截 JSON，解析失败得说清原因。"""
    if not isinstance(payload, dict):
        return False
    if wire == WIRE_GEMINI:
        candidates = payload.get("candidates")
        first = candidates[0] if isinstance(candidates, list) and candidates else {}
        return str((first or {}).get("finishReason") or "").upper() == "MAX_TOKENS"
    if wire == WIRE_ANTHROPIC:
        return str(payload.get("stop_reason") or "") == "max_tokens"
    if wire == WIRE_OPENAI_RESPONSES:
        incomplete = payload.get("incomplete_details")
        reason = (incomplete or {}).get("reason") if isinstance(incomplete, dict) else ""
        return str(reason or "") == "max_output_tokens"
    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    return str((first or {}).get("finish_reason") or "") == "length"


def _texts(items: Any, key: str) -> list[str]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            value = item.get(key)
            if isinstance(value, str) and value:
                out.append(value)
    return out


def parse_text(wire: str, payload: Any) -> str:
    """从回复里取出正文。取不到返回空串，由调用方决定怎么报。"""
    if not isinstance(payload, dict):
        return ""
    if wire == WIRE_GEMINI:
        candidates = payload.get("candidates")
        first = candidates[0] if isinstance(candidates, list) and candidates else {}
        parts = ((first or {}).get("content") or {}).get("parts")
        return "".join(_texts(parts, "text")).strip()

    if wire == WIRE_ANTHROPIC:
        blocks = payload.get("content")
        if isinstance(blocks, list):
            return "".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        return ""

    if wire == WIRE_OPENAI_RESPONSES:
        # 这家给了个拼好的快捷字段，但只在纯文本输出时才有，缺了就自己走一遍 output。
        shortcut = payload.get("output_text")
        if isinstance(shortcut, str) and shortcut.strip():
            return shortcut.strip()
        chunks: list[str] = []
        for item in payload.get("output") or []:
            if isinstance(item, dict):
                chunks.extend(_texts(item.get("content"), "text"))
        return "".join(chunks).strip()

    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    content = ((first or {}).get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    # 有些中转把 content 也拆成了块数组。
    return "".join(_texts(content, "text")).strip()


def error_detail(payload: Any) -> str:
    """从错误响应里挖出人话。四家都把它塞在 error 下，字段名不一样。"""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if isinstance(err, str):
        return err.strip()
    if isinstance(err, dict):
        for field in ("message", "detail", "status"):
            value = err.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    message = payload.get("message")
    return message.strip() if isinstance(message, str) else ""
