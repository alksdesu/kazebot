"""Fallback Provider Plugin — auto-switch to backup API on primary failure.

Why: when the primary LLM provider exhausts retries (429/5xx), the task fails
with no recourse. How: hook into after_llm_call, detect non-OK responses for
retryable status codes, and replay the same request against fallback providers
listed in config/models.yaml. Purpose: improve availability without modifying
core llm_call.py or ai_step.py.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

from clonoth_runtime import load_models_config, load_runtime_config, parse_bool, resolve_channel, resolve_env_ref
from plugin_runtime import PluginExecutionError, plugin_scope

from engine.attachments import contains_image_blocks
from engine.inference.llm_call import (
    _node_strips_images,
    cancelled_action,
    provider_messages,
    record_response_usage,
    request_view,
    wait_provider_call,
)
from engine.inference.llm_outcome import error_kind, should_fallback
from engine.model import create_llm_http_client, instantiate_provider, layered_provider_options, llm_request_timeout
from .result import hook_result
from .usage_tracker import accumulate_usage, note_serving_provider

logger = logging.getLogger(__name__)

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

    Why: fallback entries come raw from config/models.yaml (load_models_config) and
    never go through ConfigStore, so unexpanded placeholders in fallback blocks
    would reach the API verbatim. Purpose: fallback secrets can live in .env.
    """
    return resolve_env_ref((value or "").strip())


def _resolve_fallback_entry(
    fb_cfg: dict[str, Any],
    full_cfg: dict[str, Any],
    *,
    runtime_cfg: Mapping[str, Any] | None = None,
    node_options: Mapping[str, Any] | None = None,
    primary_wire: str = "",
) -> dict[str, Any]:
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

    options are layered narrow over wide: runtime providers.<wire>.options, the
    provider config block, the node's provider_options (only when the fallback
    speaks the same wire as the primary request), then the entry itself.
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
    supports_vision = bool(parse_bool(supports_vision_raw, False))

    # 备选的 provider 类型往往和主渠道不同，参数键名不通用，所以不继承主渠道的
    # options，只从同名 provider 块继承，再让条目自己覆盖。
    # 节点的 provider_options 是照主请求的线格式写的，备选也说这个线格式时才带上。
    same_wire = bool(primary_wire) and channel.wire == primary_wire
    options = layered_provider_options(
        runtime_cfg, channel.wire, channel.options, node_options if same_wire else None, fb_cfg.get("options"),
    )

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


def _select_fallbacks_for_node(
    full_cfg: dict[str, Any], node_id: str, aliases: Iterable[str] = (),
) -> list[Any]:
    """Return the fallback chain that applies to *node_id*.

    Chains written under a node's old ids (*aliases*) keep applying after a rename,
    the current id first.

    [2026-07-16] Why: system nodes (system.compactor / system.turn_summarizer /
    read_image 等) may want a cheaper/different backup chain than the main
    conversation. How: look up models.yaml's optional ``node_fallbacks`` mapping
    (keyed by node id) first; if the node has an explicit entry use it, otherwise
    fall back to the top-level ``fallbacks`` list. Purpose: keep existing configs
    working (no node_fallbacks -> global fallbacks) while allowing per-node chains.

    models.yaml layout::

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
        for key in (node_id, *aliases):
            entry = node_map.get(key)
            # An explicit empty list disables fallback for this node. This is
            # particularly important for vision nodes: silently falling through to
            # the global text-only chain would remove the image and invite guesses.
            if isinstance(entry, list):
                return entry
    return full_cfg.get("fallbacks", []) or []


def _live_loop_state(ls: Any) -> bool:
    """hook 拿到的 loop_state 是不是真在跑的那份（能发事件），测试和精简调用方只给一个 formatter。"""
    return callable(getattr(getattr(ls, "rctx", None), "emit_event", None))


async def _account_usage(ls: Any, rctx: Any, resp: Any, messages: list[dict[str, Any]], tools: Any) -> None:
    """备用渠道这次请求的用量照样记账：任务用量统计和压缩阈值都按它校准。"""
    usage = getattr(resp, "usage", None)
    if not isinstance(usage, dict):
        return
    total = getattr(rctx, "total_usage", None)
    if isinstance(total, dict):
        accumulate_usage(total, usage)
    if _live_loop_state(ls):
        await record_response_usage(ls, resp, messages, tools)


def _create_fallback_provider(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str,
    provider_options: dict[str, Any] | None = None,
    http: Any | None = None,
    runtime_cfg: dict[str, Any] | None = None,
) -> Any | None:
    """Instantiate a provider by type string using ProviderRegistry, with the same constructor kwargs as the primary path.

    Supports any provider registered in providers/__init__.py — no hardcoding.
    http is the engine's shared LLM client when the caller has one; otherwise a
    client is built by the same factory and released by _release_fallback_provider.
    """
    try:
        from providers import registry
        provider_cls = registry.get(provider_type)
        if provider_cls is None:
            logger.warning("fallback_provider: unknown provider type '%s' (available: %s)",
                           provider_type, registry.list())
            return None

        owned_http = None
        if http is None:
            owned_http = create_llm_http_client(llm_request_timeout(runtime_cfg))
            http = owned_http
        provider = instantiate_provider(provider_cls, {
            "http": http,
            "api_key": api_key,
            "base_url": base_url or None,
            "model": model,
            "provider_options": dict(provider_options or {}),
        })
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
        if not should_fallback(resp):
            logger.debug(
                "fallback_provider: skipping error not worth a fallback (status=%s kind=%s)",
                status_code, error_kind(resp),
            )
            return None

        # Get workspace root from rctx
        rctx = getattr(ctx, "rctx", None)
        workspace_root = getattr(rctx, "workspace_root", None) if rctx else None
        if not workspace_root:
            return None

        full_cfg = load_models_config(workspace_root)
        # [2026-07-16] Why: 系统节点（压缩/轮摘要/读图等）希望能配置与
        # 对话主链不同的备选渠道/模型（例如主链用大模型，压缩用便宜模型）。
        # How: 先按当前节点 id 在 models.yaml 的 node_fallbacks 映射里查专属备选链，
        # 命中则用它；未命中回退到全局 fallbacks。Purpose: 不改变旧配置行为
        # 的前提下，让系统节点能单独指定备选渠道。
        _node = getattr(ctx, "node", None)
        _node_id = str(getattr(_node, "id", "") or "").strip()
        fallbacks_raw = _select_fallbacks_for_node(full_cfg, _node_id, getattr(_node, "aliases", ()) or ())
        if not isinstance(fallbacks_raw, list) or not fallbacks_raw:
            return None

        original_error = getattr(resp, "error", "unknown")
        _ls = ctx.extra.get("loop_state")
        # 备用渠道收到的得是主渠道那一份：同样的保护预算、读后让位和任务进度摘要
        view = request_view(ctx.messages, _ls)
        tools = ctx.tools
        original_provider = ctx.provider
        node_strips_images = _node_strips_images(_node)
        has_image_input = not node_strips_images and contains_image_blocks(view)
        original_model = getattr(original_provider, "model", "unknown")
        step = int(getattr(ctx, "step", 0) or 0)
        _formatter = getattr(_ls, 'formatter', None) if _ls else None
        # 备用渠道的 provider 和主路径一样构造：同一份运行参数、同一个模型请求客户端
        runtime_cfg = getattr(_ls, "runtime_cfg", None)
        if not isinstance(runtime_cfg, dict):
            runtime_cfg = load_runtime_config(Path(workspace_root))
        shared_http = getattr(rctx, "llm_http", None)
        node_options = getattr(_node, "provider_options", None)
        primary_wire = str(getattr(original_provider, "provider_name", "") or "").strip().lower()

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
            fb_cfg = _resolve_fallback_entry(
                fb_raw, full_cfg, runtime_cfg=runtime_cfg, node_options=node_options, primary_wire=primary_wire,
            )
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
                    provider_options=fb_cfg.get("options"),
                    http=shared_http,
                    runtime_cfg=runtime_cfg,
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

                # 格式转换按备用渠道自己的 provider 走：Responses/Gemini 绕过 L2，其余经 formatter
                _fb_messages = provider_messages(
                    view, formatter=_formatter, provider=fb_provider, workspace_root=workspace_root,
                    strip_images=node_strips_images or not fb_supports_vision,
                )

                t0 = time.monotonic()
                fb_provider.plugin_channel = fb_channel
                with plugin_scope(
                    workspace_root=workspace_root, node_id=_node_id,
                    task_id=getattr(rctx, "task_id", ""), channel=fb_channel, purpose="fallback",
                ):
                    new_resp = await wait_provider_call(
                        _ls, fb_provider.chat(messages=_fb_messages, tools=tools), step,
                    )
                elapsed = round((time.monotonic() - t0) * 1000, 1)
                if new_resp is None:
                    logger.info("fallback_provider: task cancelled while waiting on fallback[%d]", i)
                    return hook_result(action=cancelled_action(_ls))
                await _account_usage(_ls, rctx, new_resp, _fb_messages, tools)

                if new_resp.ok:
                    logger.warning(
                        "fallback_provider: fallback[%d] succeeded in %.0fms "
                        "(base_url=%s model=%s)",
                        i, elapsed, fb_base_url[:40], fb_model,
                    )
                    # Overwrite ctx.response so ai_step uses the new response
                    ctx.response = new_resp
                    note_serving_provider(rctx, fb_provider)

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

            except PluginExecutionError:
                raise
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
