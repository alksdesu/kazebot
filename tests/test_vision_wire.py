"""看图请求的线格式。

同一张图发给四家，端点、鉴权头、图片装在哪一层、正文从哪取，四套都不一样。
这里盯的是「发出去之前就已经是对面认识的形状」——发错了只会拿回一个不解释原因的 4xx。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

import _vision_wire as vw  # noqa: E402

_PNG = "data:image/png;base64,QUJD"


def _build(wire: str, *, base_url: str = "https://relay.example/v1", model: str = "m"):
    return vw.build_request(
        wire=wire, base_url=vw.normalize_base_url(wire, base_url), api_key="sk-x",
        model=model, image_urls=[_PNG], system="你是标签生成器", prompt="给标签",
        max_tokens=300, temperature=0.2,
    )


# ── 映射表不能和 registry 分家 ──

def test_the_wire_table_matches_the_provider_registry() -> None:
    # 工具子进程导不了 providers（那边依赖 httpx），只能自己存一份。这条是那份的锁。
    from providers import registry

    assert vw.PROVIDER_WIRES == registry.wire_formats()


def test_the_openai_compatible_set_is_exactly_the_openai_wire_family() -> None:
    # 同一件事的第二份表示：谁收 /chat/completions。加一家新 provider 时两处都得动。
    from _channel import OPENAI_COMPATIBLE

    assert set(OPENAI_COMPATIBLE) == {
        name for name, wire in vw.PROVIDER_WIRES.items() if wire == vw.WIRE_OPENAI
    }


def test_unknown_providers_fall_back_to_openai() -> None:
    assert vw.wire_for("some-relay-brand") == vw.WIRE_OPENAI
    assert vw.wire_for("") == vw.WIRE_OPENAI


def test_deepseek_speaks_openai() -> None:
    assert vw.wire_for("deepseek") == vw.WIRE_OPENAI


@pytest.mark.parametrize(("wire", "family"), [
    ("openai", "openai"),
    ("openai-responses", "openai"),
    ("anthropic", "anthropic"),
    ("gemini", "gemini"),
])
def test_every_wire_maps_to_a_known_image_family(wire: str, family: str) -> None:
    from _image import ACCEPTED_IMAGE_MIMES

    assert vw.family_for_wire(wire) == family
    assert family in ACCEPTED_IMAGE_MIMES


# ── 地址归一化 ──

def test_openai_keeps_the_version_segment() -> None:
    assert vw.normalize_base_url("openai", "https://relay.example/v1") == "https://relay.example/v1"


def test_openai_gains_a_version_segment_when_missing() -> None:
    # 只填域名的配置一直靠这一步补全，改掉会让现有部署 404。
    assert vw.normalize_base_url("openai", "https://relay.example") == "https://relay.example/v1"


@pytest.mark.parametrize("given", [
    "https://api.anthropic.com",
    "https://api.anthropic.com/v1",
    "https://api.anthropic.com/",
])
def test_anthropic_strips_the_version_segment(given: str) -> None:
    # 端点自己会拼 /v1/messages，地址上再带一个就成了 /v1/v1/messages。
    assert vw.normalize_base_url("anthropic", given) == "https://api.anthropic.com"


@pytest.mark.parametrize("given", [
    "https://generativelanguage.googleapis.com",
    "https://generativelanguage.googleapis.com/v1beta",
])
def test_gemini_strips_the_version_segment(given: str) -> None:
    assert vw.normalize_base_url("gemini", given) == "https://generativelanguage.googleapis.com"


def test_a_bare_host_gets_a_scheme() -> None:
    assert vw.normalize_base_url("gemini", "relay.example").startswith("https://")


def test_an_empty_address_stays_empty() -> None:
    assert vw.normalize_base_url("openai", "  ") == ""


# ── 端点 ──

def test_openai_endpoint() -> None:
    assert _build("openai").url == "https://relay.example/v1/chat/completions"


def test_responses_endpoint() -> None:
    assert _build("openai-responses").url == "https://relay.example/v1/responses"


def test_anthropic_endpoint() -> None:
    assert _build("anthropic", base_url="https://api.anthropic.com").url == (
        "https://api.anthropic.com/v1/messages"
    )


def test_gemini_endpoint_carries_the_model() -> None:
    url = _build("gemini", base_url="https://generativelanguage.googleapis.com", model="flash").url
    assert url == (
        "https://generativelanguage.googleapis.com/v1beta/models/flash:generateContent"
    )


# ── 鉴权头 ──

def test_official_gemini_uses_its_own_key_header() -> None:
    headers = vw.headers("gemini", "https://generativelanguage.googleapis.com", "sk-x")
    assert headers["x-goog-api-key"] == "sk-x"
    assert "Authorization" not in headers


def test_a_gemini_relay_uses_bearer() -> None:
    headers = vw.headers("gemini", "https://relay.example", "sk-x")
    assert headers["Authorization"] == "Bearer sk-x"
    assert "x-goog-api-key" not in headers


def test_official_anthropic_uses_x_api_key_and_a_version() -> None:
    headers = vw.headers("anthropic", "https://api.anthropic.com", "sk-x")
    assert headers["x-api-key"] == "sk-x"
    assert headers["anthropic-version"]
    assert "Authorization" not in headers


def test_an_anthropic_relay_uses_bearer_but_keeps_the_version() -> None:
    headers = vw.headers("anthropic", "https://relay.example", "sk-x")
    assert headers["Authorization"] == "Bearer sk-x"
    assert headers["anthropic-version"]


def test_openai_uses_bearer() -> None:
    assert vw.headers("openai", "https://relay.example/v1", "sk-x")["Authorization"] == "Bearer sk-x"


@pytest.mark.parametrize("wire", ["openai", "openai-responses", "anthropic", "gemini"])
def test_the_key_never_lands_in_the_url(wire: str) -> None:
    # ?key= 会被代理日志和浏览器历史原样记下来，一律走请求头。
    assert "sk-x" not in _build(wire).url


# ── 请求体 ──

def test_openai_body_carries_the_image_as_a_content_part() -> None:
    body = _build("openai").body
    assert body["model"] == "m"
    assert body["messages"][0]["role"] == "system"
    parts = body["messages"][1]["content"]
    assert parts[0]["image_url"]["url"] == _PNG
    assert parts[-1]["text"] == "给标签"


def test_responses_body_uses_input_not_messages() -> None:
    body = _build("openai-responses").body
    assert "messages" not in body
    assert body["max_output_tokens"] == 300
    content = body["input"][-1]["content"]
    assert content[0] == {"type": "input_image", "image_url": _PNG}
    assert content[-1] == {"type": "input_text", "text": "给标签"}


def test_anthropic_body_splits_the_data_url_into_a_source_block() -> None:
    body = _build("anthropic").body
    assert body["system"] == "你是标签生成器"
    # 这家 max_tokens 是必填。
    assert body["max_tokens"] == 300
    image = body["messages"][0]["content"][0]
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}


def test_gemini_body_uses_inline_data_and_a_system_instruction() -> None:
    body = _build("gemini").body
    assert body["systemInstruction"]["parts"][0]["text"] == "你是标签生成器"
    parts = body["contents"][0]["parts"]
    assert parts[0]["inlineData"] == {"mimeType": "image/png", "data": "QUJD"}
    assert parts[-1]["text"] == "给标签"
    assert body["generationConfig"]["maxOutputTokens"] == 300


@pytest.mark.parametrize("wire", ["openai", "openai-responses", "anthropic", "gemini"])
def test_an_empty_system_prompt_is_omitted_entirely(wire: str) -> None:
    # 空的 system 块在 Anthropic 和 Gemini 那边都是 400。
    body = vw.build_request(
        wire=wire, base_url="https://relay.example/v1", api_key="k", model="m",
        image_urls=[_PNG], system="", prompt="给标签", max_tokens=64, temperature=0.0,
    ).body
    assert "system" not in body
    assert "systemInstruction" not in body
    for entry in body.get("messages", []) + body.get("input", []) + body.get("contents", []):
        assert entry.get("role") != "system"


def _body_with_a_bare_url(wire: str) -> str:
    return str(vw.build_request(
        wire=wire, base_url="https://relay.example/v1", api_key="k", model="m",
        image_urls=["https://example.com/a.png"], system="", prompt="给标签",
        max_tokens=64, temperature=0.0,
    ).body)


@pytest.mark.parametrize("wire", ["anthropic", "gemini"])
def test_these_two_drop_a_bare_url_instead_of_sending_it_broken(wire: str) -> None:
    # 这两家只收 base64，把裸地址塞进 source/inlineData 只会换一个 400。
    assert "example.com" not in _body_with_a_bare_url(wire)


@pytest.mark.parametrize("wire", ["openai", "openai-responses"])
def test_openai_family_passes_a_bare_url_through(wire: str) -> None:
    assert "example.com" in _body_with_a_bare_url(wire)


# ── 取正文 ──

def test_openai_reply() -> None:
    payload = {"choices": [{"message": {"content": " ok "}}]}
    assert vw.parse_text("openai", payload) == "ok"


def test_openai_reply_with_a_block_array_content() -> None:
    # 有些中转把 content 也拆成了块数组。
    payload = {"choices": [{"message": {"content": [{"type": "text", "text": "ok"}]}}]}
    assert vw.parse_text("openai", payload) == "ok"


def test_responses_prefers_the_shortcut_field() -> None:
    assert vw.parse_text("openai-responses", {"output_text": "ok"}) == "ok"


def test_responses_falls_back_to_walking_the_output() -> None:
    payload = {"output": [{"content": [{"type": "output_text", "text": "ok"}]}]}
    assert vw.parse_text("openai-responses", payload) == "ok"


def test_anthropic_reply_joins_text_blocks_only() -> None:
    payload = {"content": [
        {"type": "thinking", "thinking": "略"},
        {"type": "text", "text": "o"},
        {"type": "text", "text": "k"},
    ]}
    assert vw.parse_text("anthropic", payload) == "ok"


def test_gemini_reply_joins_parts() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": "o"}, {"text": "k"}]}}]}
    assert vw.parse_text("gemini", payload) == "ok"


@pytest.mark.parametrize("wire", ["openai", "openai-responses", "anthropic", "gemini"])
@pytest.mark.parametrize("payload", [{}, None, "", {"error": {"message": "boom"}}, {"choices": []}])
def test_unusable_replies_yield_an_empty_string(wire: str, payload) -> None:
    assert vw.parse_text(wire, payload) == ""


# ── 错误文案 ──

@pytest.mark.parametrize("payload", [
    {"error": {"message": "配额用光了"}},
    {"error": "配额用光了"},
    {"message": "配额用光了"},
])
def test_error_detail_finds_the_human_sentence(payload) -> None:
    assert vw.error_detail(payload) == "配额用光了"


# ── 思考预算与退让 ──

def test_gemini_turns_thinking_off() -> None:
    # 思考照样算进 maxOutputTokens，几百个思考 token 就能把正文顶掉。
    config = _build("gemini").body["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingBudget": 0}


@pytest.mark.parametrize("wire", ["openai", "openai-responses", "anthropic"])
def test_other_wires_have_no_thinking_knob(wire: str) -> None:
    assert "thinkingConfig" not in str(_build(wire).body)


def test_a_model_that_refuses_to_stop_thinking_gets_room_instead() -> None:
    body = _build("gemini").body
    relaxed = vw.relax_body("gemini", body, "thinkingBudget must be at least 128 for this model")

    assert relaxed is not None
    assert "thinkingConfig" not in relaxed["generationConfig"]
    # 思考关不掉就得给它留位置，否则正文一样会被顶掉。
    assert relaxed["generationConfig"]["maxOutputTokens"] > body["generationConfig"]["maxOutputTokens"]


def test_relaxing_does_not_mutate_the_original_body() -> None:
    body = _build("gemini").body
    vw.relax_body("gemini", body, "thinking budget invalid")

    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_unrelated_errors_are_not_retried() -> None:
    # 退让只针对我们自己加的那个优化，模型名写错重发一次也还是错。
    assert vw.relax_body("gemini", _build("gemini").body, "model not found") is None


@pytest.mark.parametrize("wire", ["openai", "openai-responses", "anthropic"])
def test_only_gemini_has_something_to_relax(wire: str) -> None:
    assert vw.relax_body(wire, _build(wire).body, "thinking budget invalid") is None


def test_relaxing_twice_finds_nothing_left() -> None:
    body = _build("gemini").body
    once = vw.relax_body("gemini", body, "thinking budget invalid")
    assert vw.relax_body("gemini", once, "thinking budget invalid") is None


# ── 截断 ──

@pytest.mark.parametrize(("wire", "payload"), [
    ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS"}]}),
    ("anthropic", {"stop_reason": "max_tokens"}),
    ("openai", {"choices": [{"finish_reason": "length"}]}),
    ("openai-responses", {"incomplete_details": {"reason": "max_output_tokens"}}),
])
def test_truncation_is_recognised(wire: str, payload) -> None:
    assert vw.truncated(wire, payload) is True


@pytest.mark.parametrize(("wire", "payload"), [
    ("gemini", {"candidates": [{"finishReason": "STOP"}]}),
    ("anthropic", {"stop_reason": "end_turn"}),
    ("openai", {"choices": [{"finish_reason": "stop"}]}),
    ("openai-responses", {"status": "completed"}),
    ("openai", {}),
])
def test_a_clean_finish_is_not_truncation(wire: str, payload) -> None:
    assert vw.truncated(wire, payload) is False


def test_error_detail_gives_up_quietly() -> None:
    assert vw.error_detail({"weird": 1}) == ""
    assert vw.error_detail(None) == ""
