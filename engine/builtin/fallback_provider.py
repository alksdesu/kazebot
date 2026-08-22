"""Fallback Provider Plugin — auto-switch to backup API on primary failure.

Why: when the primary LLM provider exhausts retries (429/5xx), the task fails
with no recourse. How: hook into after_llm_call, detect non-OK responses for
retryable status codes, and replay the same request against fallback providers
listed in data/config.yaml. Purpose: improve availability without modifying
core llm_call.py or ai_step.py.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from clonoth_runtime import resolve_channel, resolve_env_ref

# [fix 2026-05-28] Import message formatting utilities so fallback calls go
# through the same conversion pipeline as the primary call (llm_call.py L171-172).
# Without this, internal fields like _meta/_ephemeral leak into provider requests.
from engine.inference.llm_call import _build_messages_for_provider
from engine.attachments import prepare_messages_for_llm


def _messages_contain_images(messages: list[dict[str, Any]]) -> bool:
    """Return whether provider-ready messages contain multimodal image blocks."""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(part, dict) and part.get("type") in {"image_url", "input_image", "image"}
            for part in content
        ):
            return True
    return False


def _strip_image_blocks(messages: list[dict]) -> list[dict]:
    """Remove image_url content blocks from all messages.

    Why: fallback providers like DeepSeek don't support vision/multimodal.
    How: for each message with list-type content, filter out image_url blocks.
    If a message becomes empty after stripping, replace with placeholder text.
    """
    result = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue
        filtered = [p for p in content if not (isinstance(p, dict) and p.get("type") == "image_url")]
        if not filtered:
            new_msg = {**msg, "content": "[image content removed]"}
        elif len(filtered) == 1 and isinstance(filtered[0], dict) and filtered[0].get("type") == "text":
            new_msg = {**msg, "content": filtered[0].get("text", "")}
        elif len(filtered) == len(content):
            result.append(msg)
            continue
        else:
            new_msg = {**msg, "content": filtered}
        result.append(new_msg)
    return result

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

PLUGIN_META = {
    "handler_class": "FallbackProviderHandler",
    "hook_points": [
        ("after_llm_call", "handle"),
    ],
    "priority": -20,  # run after error_snapshot (-10)
    "name": "fallback_provider",
    "description": "Primary API failure auto-fallback to backup providers",
}


def _resolve_env_value(value: str | None) -> str:
    """Expand ${VAR} / $ENV{VAR} references against process environment.

    Why: fallback_provider reads data/config.yaml directly via yaml.safe_load and
    never goes through ConfigStore, so unexpanded placeholders in fallback blocks
    would reach the API verbatim. Purpose: fallback secrets can live in .env.
    """
    return resolve_env_ref((value or "").strip())


def _load_config(workspace_root: str | Path) -> dict[str, Any]:
    """Read full data/config.yaml."""
    cfg_path = Path(workspace_root) / "data" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("fallback_provider: failed to load config: %s", exc)
        return {}


def _resolve_fallback_entry(fb_cfg: dict[str, Any], full_cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve a fallback entry by merging with its provider config block.

    Expected config layout:
        deepseek:
          base_url: https://api.deepseek.com
          api_key: sk-xxx
          model: deepseek-v4-pro
        gemini-中转A:
          type: gemini                  # 渠道名随便起，type 决定线格式
          base_url: https://relay.example/v1
        fallbacks:
          - provider: deepseek          # just reference, auto-inherits from deepseek: block
          - provider: gemini-中转A       # 命名渠道同样能引用
          - provider: openai            # inherits from openai: block
            model: claude-sonnet-4-6    # override model only

    Resolution order for each field (base_url, api_key, model):
      1. Explicit value in fallback entry
      2. Value from the provider config block (e.g. deepseek:)
    """
    channel_name = str(fb_cfg.get("provider") or "openai").strip()
    channel = resolve_channel("", channel_name, config=full_cfg)
    provider_block = full_cfg.get(channel_name)
    if not isinstance(provider_block, dict):
        provider_block = {}

    # [fix 2026-07-09] Resolve ${VAR}/$ENV{VAR} for both the fallback entry and
    # the inherited provider block so secrets can live in .env. Explicit values
    # on the fallback entry still take precedence over the provider block.
    supports_vision_raw = fb_cfg.get("supports_vision")
    if supports_vision_raw is None:
        supports_vision_raw = provider_block.get("supports_vision", False)
    if isinstance(supports_vision_raw, str):
        supports_vision = supports_vision_raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        supports_vision = bool(supports_vision_raw)

    # 备选的 provider 类型往往和主渠道不同，参数键名不通用，所以不继承主渠道的
    # options，只从同名 provider 块继承，再让条目自己覆盖。
    entry_options = fb_cfg.get("options")
    options = dict(channel.options)
    if isinstance(entry_options, dict):
        options.update(entry_options)

    return {
        # provider 是渠道名，只用来说人话；wire 才是 registry 认的那个 key。
        "provider": channel_name,
        "wire": channel.wire,
        "base_url": _resolve_env_value(fb_cfg.get("base_url")) or channel.base_url,
        "api_key": _resolve_env_value(fb_cfg.get("api_key")) or channel.api_key,
        "model": _resolve_env_value(fb_cfg.get("model")) or channel.model,
        "supports_vision": supports_vision,
        "options": options,
    }


def _select_fallbacks_for_node(full_cfg: dict[str, Any], node_id: str) -> list[Any]:
    """Return the fallback chain that applies to *node_id*.

    [2026-07-16] Why: system nodes (system.compactor / system.turn_summarizer /
    read_image 等) may want a cheaper/different backup chain than the main
    conversation. How: look up config.yaml's optional ``node_fallbacks`` mapping
    (keyed by node id) first; if the node has an explicit entry use it, otherwise
    fall back to the top-level ``fallbacks`` list. Purpose: keep existing configs
    working (no node_fallbacks -> global fallbacks) while allowing per-node chains.

    config.yaml layout::

        fallbacks:              # 全局默认备选链（主对话等）
          - provider: openai

        node_fallbacks:         # 可选：按节点 id 的专属备选链
          system.compactor:
            - provider: openai
              model: deepseek-v4-pro
          system.turn_summarizer:
            - provider: openai
              model: deepseek-v4-pro
    """
    node_map = full_cfg.get("node_fallbacks")
    if node_id and isinstance(node_map, dict):
        entry = node_map.get(node_id)
        # An explicit empty list disables fallback for this node. This is
        # particularly important for vision nodes: silently falling through to
        # the global text-only chain would remove the image and invite guesses.
        if isinstance(entry, list):
            return entry
    return full_cfg.get("fallbacks", []) or []


def _is_retryable(status_code: int | None, error: str | None = None) -> bool:
    """Check if the error is worth retrying on a different provider.

    Covers both HTTP-level errors (429/5xx) and upstream provider errors
    that arrive as ok=False with HTTP 200 (e.g. content_filter, safety
    blocks, rate limits, empty responses via SSE error objects).
    """
    # Any response marked ok=False that reaches us is worth retrying,
    # unless it's a definitive client error (4xx other than 429).
    if status_code is None:
        return True  # unknown / connection-level error
    if status_code in _RETRYABLE_STATUS_CODES:
        return True
    # HTTP 200 but ok=False → upstream signaled error through body/SSE
    # (content_filter, safety block, quota via SSE error object, etc.)
    if status_code == 200 and error:
        return True
    # 4xx (except 429 and certain content moderation 403s) are client errors — don't retry
    # 403 can be either a true auth rejection or a content moderation refusal (safeguards).
    # Content moderation refusals are worth retrying on a different provider (e.g., Claude
    # cyber safeguard → fallback to DeepSeek); auth rejections are not.
    _CONTENT_MODERATION_KEYWORDS = {
        'safeguard', 'content_filter', 'refused', 'safety', 'cyber',
        'not allowed', 'community guidelines', 'moderation',
    }
    if status_code == 403 and error:
        err_lower = error.lower()
        if any(kw in err_lower for kw in _CONTENT_MODERATION_KEYWORDS):
            return True  # content moderation → retryable on different provider
        return False  # auth/permissions rejection → don't retry
    if 400 <= status_code < 500:
        return False
    return True  # anything else (1xx, 3xx, unknown) → try fallback


def _create_fallback_provider(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 600.0,
    provider_options: dict[str, Any] | None = None,
) -> Any | None:
    """Instantiate a provider by type string using ProviderRegistry.

    Supports any provider registered in providers/__init__.py — no hardcoding.
    """
    try:
        import inspect

        import httpx

        from providers import registry
        provider_cls = registry.get(provider_type)
        if provider_cls is None:
            logger.warning("fallback_provider: unknown provider type '%s' (available: %s)",
                           provider_type, registry.list())
            return None

        # [fix 2026-08-04] Why: 不同 provider 的 __init__ 签名并不一致——
        # 例如 OpenAIProvider 是 keyword-only，要求外部传入 httpx.AsyncClient
        # 的 `http`，且不接受 `timeout`；而 DeepSeekProvider 接受可选 `http`
        # 和 `timeout`。旧代码固定传 `timeout` 会让所有 openai 类型的 fallback
        # 因 "unexpected keyword argument 'timeout'" 创建失败，导致 fallback
        # 形同虚设（主渠道 900s 超时后无任何备选生效）。
        # How: 用 inspect 读取目标构造函数接受的参数名，只投递它真正支持的
        # kwargs；当 provider 需要 `http` 但不接受 `timeout` 时，为它构造一个
        # 带超时的 httpx.AsyncClient。Purpose: 让 fallback 对任意 provider 通用
        # 生效，且超时语义不丢失。
        try:
            sig_params = inspect.signature(provider_cls.__init__).parameters
            accepted = set(sig_params.keys())
            has_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values()
            )
        except (TypeError, ValueError):
            accepted = set()
            has_var_kw = True  # 无法内省时不做过滤，保持旧行为

        def _wanted(name: str) -> bool:
            return has_var_kw or name in accepted

        kwargs: dict[str, Any] = {"model": model, "api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url

        if _wanted("timeout") and timeout:
            kwargs["timeout"] = timeout

        if _wanted("provider_options") and provider_options:
            kwargs["provider_options"] = provider_options

        # provider 需要外部注入 http 客户端（如 OpenAIProvider）时，构造一个
        # 带超时的 AsyncClient。它的生命周期随 provider 释放（此处不显式关闭，
        # 交由进程退出/GC 回收，fallback provider 为一次性使用）。
        owned_http = None
        if _wanted("http") and "http" not in kwargs:
            owned_http = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout or 600.0, connect=10.0)
            )
            kwargs["http"] = owned_http

        provider = provider_cls(**kwargs)
        # 这条链路每失败一次就建一个连接池，不还回去就一直攒着。挂在实例上让
        # 调用方能在用完后关掉；共享进来的 client 不带这个标记，也就不会被误关。
        if owned_http is not None:
            provider._fallback_owned_http = owned_http
        return provider
    except Exception as exc:
        logger.warning("fallback_provider: failed to create %s provider: %s", provider_type, exc)
        return None


async def _release_fallback_provider(provider: Any | None) -> None:
    """归还 _create_fallback_provider 为这次调用单独建的连接池。"""
    owned = getattr(provider, "_fallback_owned_http", None)
    if owned is None:
        return
    try:
        await owned.aclose()
    except Exception as exc:
        logger.debug("fallback_provider: closing borrowed http client failed: %s", exc)


class FallbackProviderHandler:
    """After-LLM-call hook: replay failed requests on fallback providers."""

    name = "fallback_provider"
    priority = -20

    async def handle(self, ctx: Any) -> Any | None:
        resp = ctx.response
        if resp is None or getattr(resp, "ok", True):
            return None  # success or no response, don't intervene

        status_code = getattr(resp, "status_code", None)
        original_error = getattr(resp, "error", None) or ""
        if not _is_retryable(status_code, original_error):
            logger.debug(
                "fallback_provider: skipping non-retryable error (status=%s)",
                status_code,
            )
            return None

        # Get workspace root from rctx
        rctx = getattr(ctx, "rctx", None)
        workspace_root = getattr(rctx, "workspace_root", None) if rctx else None
        if not workspace_root:
            return None

        full_cfg = _load_config(workspace_root)
        # [2026-07-16] Why: 系统节点（压缩/轮摘要/读图等）希望能配置与
        # 对话主链不同的备选渠道/模型（例如主链用大模型，压缩用便宜模型）。
        # How: 先按当前节点 id 在 config.yaml 的 node_fallbacks 映射里查专属备选链，
        # 命中则用它；未命中回退到全局 fallbacks。Purpose: 不改变旧配置行为
        # 的前提下，让系统节点能单独指定备选渠道。
        _node = getattr(ctx, "node", None)
        _node_id = str(getattr(_node, "id", "") or "").strip()
        fallbacks_raw = _select_fallbacks_for_node(full_cfg, _node_id)
        if not isinstance(fallbacks_raw, list) or not fallbacks_raw:
            return None

        original_error = getattr(resp, "error", "unknown")
        messages = ctx.messages
        tools = ctx.tools
        original_provider = ctx.provider
        has_image_input = _messages_contain_images(messages)
        original_model = getattr(original_provider, "model", "unknown")

        # [fix 2026-05-28] Retrieve formatter from loop_state so we can run the
        # same message format conversion that llm_call.py does before provider.chat().
        # Why: without this, messages with _meta/_ephemeral fields are sent raw to
        # the fallback provider, causing "missing field type" errors on DS etc.
        _ls = ctx.extra.get("loop_state")
        _formatter = getattr(_ls, 'formatter', None) if _ls else None

        logger.warning(
            "fallback_provider: primary failed (status=%s error=%s model=%s), "
            "trying %d fallback(s)",
            status_code, original_error, original_model, len(fallbacks_raw),
        )

        # [fix 2026-05-28] Collect per-fallback error details so that when all
        # fallbacks fail, the user sees what was attempted and why each failed,
        # instead of only seeing the original provider's error message.
        fallback_errors: list[str] = []

        # Try each fallback in chain order
        for i, fb_raw in enumerate(fallbacks_raw):
            if not isinstance(fb_raw, dict):
                continue
            fb_cfg = _resolve_fallback_entry(fb_raw, full_cfg)
            fb_channel = fb_cfg["provider"]
            fb_provider_type = fb_cfg["wire"]
            fb_base_url = fb_cfg["base_url"]
            fb_api_key = fb_cfg["api_key"]
            fb_model = fb_cfg["model"] or original_model
            fb_supports_vision = bool(fb_cfg.get("supports_vision"))

            if has_image_input and not fb_supports_vision:
                logger.warning(
                    "fallback_provider: skipping fallback[%d] (%s/%s) — "
                    "request contains images but fallback is not marked supports_vision=true",
                    i, fb_channel, fb_model,
                )
                fallback_errors.append(
                    f"[Fallback {fb_channel}/{fb_model} skipped: vision unsupported]"
                )
                continue

            if not fb_base_url or not fb_api_key:
                logger.warning(
                    "fallback_provider: skipping fallback[%d] (%s) — no base_url/api_key after resolve",
                    i, fb_channel,
                )
                continue

            fb_provider = None
            try:
                fb_provider = _create_fallback_provider(
                    provider_type=fb_provider_type,
                    base_url=fb_base_url,
                    api_key=fb_api_key,
                    model=fb_model,
                    timeout=getattr(original_provider, "timeout", 600.0),
                    provider_options=fb_cfg.get("options"),
                )
                if fb_provider is None:
                    logger.warning(
                        "fallback_provider: skipping fallback[%d] (%s) — unsupported wire format '%s'",
                        i, fb_channel, fb_provider_type,
                    )
                    continue

                logger.info(
                    "fallback_provider: trying fallback[%d] base_url=%s model=%s",
                    i, fb_base_url[:40], fb_model,
                )

                # [fix 2026-05-28] Format messages for this specific fallback
                # provider before calling chat(). Mirrors llm_call.py L171-172:
                #   1. _build_messages_for_provider: L2 formatter / bypass based
                #      on provider type (e.g. Responses/Gemini skip L2)
                #   2. prepare_messages_for_llm: resolve file:// image refs → base64
                # The third arg MUST be fb_provider (not original_provider) because
                # different providers need different format conversion paths.
                _fb_formatted = _build_messages_for_provider(
                    messages, _formatter, fb_provider,
                )
                _fb_messages = prepare_messages_for_llm(
                    _fb_formatted, workspace_root,
                ) if workspace_root else _fb_formatted

                # Only text-only fallbacks may strip image blocks. Multimodal
                # requests are filtered above unless the entry explicitly opts in
                # with supports_vision=true, in which case the original image
                # blocks must reach the fallback provider unchanged.
                if not fb_supports_vision:
                    _fb_messages = _strip_image_blocks(_fb_messages)

                t0 = time.monotonic()
                new_resp = await fb_provider.chat(
                    messages=_fb_messages,
                    tools=tools,
                )
                elapsed = round((time.monotonic() - t0) * 1000, 1)

                if new_resp.ok:
                    logger.warning(
                        "fallback_provider: fallback[%d] succeeded in %.0fms "
                        "(base_url=%s model=%s)",
                        i, elapsed, fb_base_url[:40], fb_model,
                    )
                    # Overwrite ctx.response so ai_step uses the new response
                    ctx.response = new_resp

                    # Emit signal if bus is available
                    try:
                        from engine.signals import get_bus, Signal
                        bus = get_bus()
                        bus.emit(Signal(
                            name="llm.fallback",
                            payload={
                                "original_error": original_error,
                                "original_status": status_code,
                                "fallback_index": i,
                                "fallback_url": fb_base_url[:60],
                                "fallback_model": fb_model,
                                "success": True,
                                "elapsed_ms": elapsed,
                            },
                        ))
                    except Exception:
                        pass  # signal emission is best-effort

                    return None  # ai_step continues with updated ctx.response
                else:
                    _fb_err = getattr(new_resp, 'error', 'unknown')
                    _fb_st = getattr(new_resp, 'status_code', '?')
                    logger.warning(
                        "fallback_provider: fallback[%d] also failed "
                        "(status=%s error=%s)",
                        i, _fb_st, _fb_err,
                    )
                    # [fix 2026-05-28] Record this fallback's failure for later
                    # aggregation into the user-facing error message.
                    fallback_errors.append(
                        f"[Fallback {fb_channel} failed: {str(_fb_err)[:150]}]"
                    )

            except Exception as exc:
                logger.error(
                    "fallback_provider: fallback[%d] exception: %s",
                    i, exc, exc_info=True,
                )
                # [fix 2026-05-28] Record exception-type failures too.
                fallback_errors.append(
                    f"[Fallback {fb_cfg.get('provider', '?')} exception: {str(exc)[:150]}]"
                )
            finally:
                await _release_fallback_provider(fb_provider)

        # [fix 2026-05-28] Why: when all fallbacks fail, the user only sees the
        # original provider's error and has no idea fallbacks were even attempted.
        # How: append each fallback's failure reason to ctx.response.error.
        # Purpose: give the user full visibility into what was tried.
        if fallback_errors and hasattr(ctx.response, "error"):
            fb_summary = " | ".join(fallback_errors)
            ctx.response.error = f"{original_error} | {fb_summary}"

        # All fallbacks failed, emit signal and let original error flow
        logger.error(
            "fallback_provider: all %d fallback(s) failed, "
            "original error stands (status=%s)",
            len(fallbacks_raw), status_code,
        )
        try:
            from engine.signals import get_bus, Signal
            bus = get_bus()
            bus.emit(Signal(
                name="llm.fallback",
                payload={
                    "original_error": original_error,
                    "original_status": status_code,
                    "fallback_count": len(fallbacks_raw),
                    "success": False,
                },
            ))
        except Exception:
            pass

        return None
