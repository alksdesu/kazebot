"""通用生图节点渠道可用性注入插件。

目的：draw.image_gen 节点授权了 gpt_image_2 与 gemini_image 两个生图工具，但
用户不一定同时配置了这两个渠道。若把两个工具的提示词都发给模型，未配置的渠道会
被误调用，导致白白多一轮工具失败。

做法：注册 before_prompt_build 钩子，只对 draw.image_gen 生效。运行时检测每个渠道
是否具备可用的 api_key / base_url（解析顺序与 tools/gpt_image_2.py、tools/
gemini_image.py 内部一致：config.yaml system_models.<slot> > CLONOTH_IMAGE_<X>_*
环境变量 > 主渠道回退），把节点 prompt 里的占位符 <<IMAGE_TOOLS_AVAILABILITY>>
替换成“哪个工具可用 / 哪个未配置不可用”的说明。

原因：与内置 knowledge_inject / draw_character_inject 使用同一套 HookContext 协议，
保持零侵入；工具授权仍是静态的，这里只影响模型看到的提示词，避免误调用。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TARGET_NODE_ID = "draw.image_gen"
_PLACEHOLDER = "<<IMAGE_TOOLS_AVAILABILITY>>"


def _load_dotenv(workspace_root: Path) -> dict[str, str]:
    env_path = workspace_root / ".env"
    kv: dict[str, str] = {}
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip().strip("'\"")
        except Exception as exc:  # noqa: BLE001
            logger.warning("image_gen_inject: 读取 .env 失败: %s", exc)
    return kv


def _load_system_models(workspace_root: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    cfg_path = workspace_root / "data" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_gen_inject: 读取 config.yaml 失败: %s", exc)
        return {}
    sm = data.get("system_models") if isinstance(data, dict) else None
    return sm if isinstance(sm, dict) else {}


class _Resolver:
    """按与生图工具一致的优先级解析单个渠道的 api_key / base_url。"""

    def __init__(self, workspace_root: Path) -> None:
        self._dotenv = _load_dotenv(workspace_root)
        self._system_models = _load_system_models(workspace_root)

    def _env(self, key: str) -> str:
        return (os.environ.get(key, "") or self._dotenv.get(key, "")).strip()

    def _resolve_ref(self, v: str) -> str:
        s = (v or "").strip()
        if s.startswith("${") and s.endswith("}") and len(s) > 3:
            return self._env(s[2:-1].strip())
        if s.startswith("$ENV{") and s.endswith("}") and len(s) > 6:
            return self._env(s[5:-1].strip())
        return s

    def _slot(self, slot: str) -> dict[str, Any]:
        blk = self._system_models.get(slot)
        return blk if isinstance(blk, dict) else {}

    def _pick(self, slot: str, cfg_field: str, env_suffix: str, *fallbacks: str) -> str:
        v = self._resolve_ref(str(self._slot(slot).get(cfg_field) or ""))
        if v:
            return v
        v = self._env(f"CLONOTH_IMAGE_{env_suffix}")
        if v:
            return v
        for fb in fallbacks:
            if fb and fb.strip():
                return fb.strip()
        return ""

    def gpt_available(self) -> bool:
        # [2026-07-19] 只认显式配置的 model（config.system_models.image_gpt.model
        # 或 env CLONOTH_IMAGE_GPT_MODEL 非空），不再因回退主渠道而判为可用。
        return self._model_configured("image_gpt", "CLONOTH_IMAGE_GPT_MODEL")

    def gemini_available(self) -> bool:
        # [2026-07-19] 同上，只认显式配置的 gemini model。
        return self._model_configured("image_gemini", "CLONOTH_IMAGE_GEMINI_MODEL")

    def _model_configured(self, slot: str, env_model_key: str) -> bool:
        cfg_model = self._resolve_ref(str(self._slot(slot).get("model") or ""))
        if cfg_model.strip():
            return True
        return bool(self._env(env_model_key))


def _build_availability_text(gpt_ok: bool, gemini_ok: bool) -> str:
    lines = ["【当前可用的生图渠道】"]
    if gpt_ok:
        lines.append("- gpt_image_2：【可用】写实/照片/图片编辑/精确分辨率生图。")
    else:
        lines.append("- gpt_image_2：【未配置·不可用】禁止调用，也不要向用户提及。")
    if gemini_ok:
        lines.append("- gemini_image：【可用】二次元/插画/风格化/按宽高比生图。")
    else:
        lines.append("- gemini_image：【未配置·不可用】禁止调用，也不要向用户提及。")

    if not gpt_ok and not gemini_ok:
        lines.append(
            "\n当前**没有任何生图渠道被配置**。不要调用任何生图工具，直接用 finish "
            "如实告诉用户：生图功能尚未配置，需要在 data/config.yaml 的 "
            "system_models.image_gpt / system_models.image_gemini 或对应环境变量中"
            "配置 model/base_url/api_key。"
        )
    return "\n".join(lines)


class ImageGenAvailabilityInjector:
    """在 draw.image_gen 节点注入生图渠道可用性说明。"""

    name = "image_gen_inject"
    priority = 45  # 与 draw_character_inject(40) 相近，独立注入互不影响

    async def handle(self, ctx: Any) -> Any | None:
        node = getattr(ctx, "node", None)
        rctx = getattr(ctx, "rctx", None)
        if node is None or rctx is None:
            return None

        node_id = str(getattr(node, "id", "") or "")
        if node_id != _TARGET_NODE_ID:
            return None

        workspace_root = getattr(rctx, "workspace_root", None)
        if workspace_root is None:
            return None
        workspace_root = Path(workspace_root)

        resolver = _Resolver(workspace_root)
        gpt_ok = resolver.gpt_available()
        gemini_ok = resolver.gemini_available()
        availability = _build_availability_text(gpt_ok, gemini_ok)

        # 把占位符替换成可用性说明。占位符出现在最初的 system prompt 骨架里，
        # 遍历 messages 找到含占位符的文本内容并就地替换。
        replaced = False
        messages = ctx.messages
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str) and _PLACEHOLDER in content:
                msg["content"] = content.replace(_PLACEHOLDER, availability)
                replaced = True
            elif isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                        and _PLACEHOLDER in part["text"]
                    ):
                        part["text"] = part["text"].replace(_PLACEHOLDER, availability)
                        replaced = True

        if not replaced:
            # 占位符不存在（prompt 被改过等）：不强行注入，避免污染。
            return None

        logger.info(
            "image_gen_inject: 注入生图渠道可用性 gpt=%s gemini=%s node=%s",
            gpt_ok, gemini_ok, node_id,
        )
        return _hook_result(modified=True)


def _hook_result(*, modified: bool = False):
    """构造一个 HookResult 兼容对象（避免强依赖 hooks 包内部结构）。"""
    try:
        from engine.hooks.types import HookResult  # type: ignore

        return HookResult(modified=modified)
    except Exception:  # noqa: BLE001
        class _R:  # 最小 duck-typed 兜底
            def __init__(self, modified: bool) -> None:
                self.block = False
                self.skip_step = False
                self.action = None
                self.reason = ""
                self.error_message = ""
                self.modified = modified

        return _R(modified)


PLUGIN_META = {
    "name": "image-gen-inject",
    "version": "1.0.0",
    "description": "为 draw.image_gen 节点按实际配置注入可用生图渠道说明，未配置渠道不发提示词避免误调用。",
    "author": "Clonoth",
    "handler_class": "ImageGenAvailabilityInjector",
    "hook_points": [
        ("before_prompt_build", "handle"),
    ],
    "priority": 45,
    "hooks": ["before_prompt_build"],
}
