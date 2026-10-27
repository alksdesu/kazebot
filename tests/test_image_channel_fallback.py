"""生图渠道的回退链，以及提示词按可用渠道的裁剪。

难点是「借主渠道」的边界：地址借错家不会报地址错误，只会得到一个跟配置毫无关系的
404。所以格式对不上时宁可判成没配。models.yaml 读不了又是另一回事：那是判断不了，不是没配。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox._common import safe_subprocess_env  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


channel = _load("_test_image_channel", "tools/_channel.py")
inject = _load("_test_image_inject", "plugins/image_gen_inject.py")


def _workspace(tmp_path: Path, config: str, dotenv: str = "") -> Path:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "models.yaml").write_text(config, encoding="utf-8")
    if dotenv:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    return tmp_path


_GEMINI_MAIN = """
version: 1
provider: gemini
gemini:
  base_url: https://relay.example.com/g/tok
  api_key: main-key
  model: gemini-3.7-flash
"""

_OPENAI_MAIN = """
version: 1
provider: openai
openai:
  base_url: https://gw.example.com/v1
  api_key: main-key
  model: '[v]some-chat-model'
"""

_RESPONSES_MAIN = """
version: 1
provider: openai-responses
openai-responses:
  base_url: https://gw.example.com/v1
  api_key: main-key
  model: gpt-5.5
"""

# 出错的正是写着密钥的那一行：报出来的原因里不能带上它。
_BROKEN_MODELS = """version: 1
provider: openai
openai:
  api_key: [sk-secret-in-a-bad-line
  base_url: https://gw.example.com/v1
"""


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 开发机上真有 OPENAI_API_KEY 的话，回退矩阵会整片测不准。
    for name in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "GEMINI_API_KEY", "GEMINI_BASE_URL",
        "CLONOTH_IMAGE_GPT_API_KEY", "CLONOTH_IMAGE_GPT_BASE_URL",
        "CLONOTH_IMAGE_GEMINI_API_KEY", "CLONOTH_IMAGE_GEMINI_BASE_URL",
        "NOVELAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class TestBorrowingTheMainChannel:
    def test_gemini_borrows_a_gemini_main_channel(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _GEMINI_MAIN)
        resolved = channel.resolve_image_channel("image_gemini", root=root)

        assert resolved.usable
        assert resolved.api_key == "main-key"
        assert resolved.base_url == "https://relay.example.com/g/tok"

    def test_gpt_refuses_a_gemini_main_channel(self, tmp_path: Path) -> None:
        # 拿 Gemini 的原生反代发 /chat/completions 只会 404。
        root = _workspace(tmp_path, _GEMINI_MAIN)
        resolved = channel.resolve_image_channel("image_gpt", root=root)

        assert not resolved.usable
        assert resolved.base_url == ""

    def test_gpt_borrows_an_openai_main_channel(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _OPENAI_MAIN)
        resolved = channel.resolve_image_channel("image_gpt", root=root)

        assert resolved.usable
        assert resolved.base_url == "https://gw.example.com/v1"

    def test_a_deepseek_main_channel_is_not_borrowed(self, tmp_path: Path) -> None:
        # DeepSeek 只有 chat/completions，没有 images 端点，借过去发的每一张都是 404。
        root = _workspace(tmp_path, """
version: 1
provider: deepseek
deepseek:
  base_url: https://ds.example.com/v1
  api_key: ds-key
  model: deepseek-v4
""")
        resolved = channel.resolve_image_channel("image_gpt", root=root)

        assert not resolved.usable
        assert (resolved.api_key, resolved.base_url) == ("", "")

    @pytest.mark.parametrize("config", [
        _RESPONSES_MAIN,
        # 块名随便起，type 写明是哪一家
        """
version: 1
provider: relay
relay:
  type: openai-responses
  base_url: https://gw.example.com/v1
  api_key: main-key
  model: gpt-5.5
""",
    ], ids=["block-name", "declared-type"])
    def test_gpt_borrows_a_responses_main_channel(self, tmp_path: Path, config: str) -> None:
        # 同一个兼容网关的根下 responses、chat/completions 和 images 都挂着，Engine 也按同一族看。
        resolved = channel.resolve_image_channel("image_gpt", root=_workspace(tmp_path, config))

        assert resolved.usable
        assert (resolved.api_key, resolved.base_url) == ("main-key", "https://gw.example.com/v1")
        assert resolved.model == "gpt-image-2"

    def test_gemini_refuses_a_responses_main_channel(self, tmp_path: Path) -> None:
        resolved = channel.resolve_image_channel("image_gemini", root=_workspace(tmp_path, _RESPONSES_MAIN))

        assert resolved.api_key == ""
        assert not resolved.usable

    def test_the_gpt_slot_borrows_exactly_what_the_engine_calls_one_family(self) -> None:
        from engine.runner import _OPENAI_COMPATIBLE_PROVIDERS

        assert channel.OPENAI_FAMILY == _OPENAI_COMPATIBLE_PROVIDERS
        assert set(channel.IMAGE_SLOTS["image_gpt"].providers) == channel.OPENAI_FAMILY

    def test_gemini_refuses_an_openai_main_channel(self, tmp_path: Path) -> None:
        # generateContent 是 Gemini 原生端点，别家的地址接不住。
        root = _workspace(tmp_path, _OPENAI_MAIN)
        resolved = channel.resolve_image_channel("image_gemini", root=root)

        assert resolved.api_key == ""
        assert not resolved.usable

    def test_the_chat_model_is_never_borrowed(self, tmp_path: Path) -> None:
        # 主渠道那个是聊天模型，生图端点不认。
        root = _workspace(tmp_path, _GEMINI_MAIN)
        resolved = channel.resolve_image_channel("image_gemini", root=root)

        assert resolved.model == "gemini-3-pro-image-preview"


class TestPrecedence:
    def test_the_slot_outranks_everything(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _GEMINI_MAIN + """
system_models:
  image_gemini:
    model: own-model
    base_url: https://own.example.com
    api_key: own-key
""")
        resolved = channel.resolve_image_channel("image_gemini", root=root)

        assert (resolved.api_key, resolved.base_url, resolved.model) == (
            "own-key", "https://own.example.com", "own-model")

    def test_env_outranks_the_main_channel(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _GEMINI_MAIN, dotenv="GEMINI_API_KEY=env-key\n")

        assert channel.resolve_image_channel("image_gemini", root=root).api_key == "env-key"

    def test_an_explicit_model_argument_wins(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _GEMINI_MAIN)
        resolved = channel.resolve_image_channel(
            "image_gemini", root=root, model_override="asked-for-this")

        assert resolved.model == "asked-for-this"


class TestUsability:
    def test_gemini_falls_back_to_the_public_endpoint(self, tmp_path: Path) -> None:
        # 只给 key 就该能用：Gemini 有公开域名，地址不是必填项。
        root = _workspace(tmp_path, "version: 1\nprovider: openai\n",
                          dotenv="GEMINI_API_KEY=k\n")
        resolved = channel.resolve_image_channel("image_gemini", root=root)

        assert resolved.base_url == "https://generativelanguage.googleapis.com"
        assert resolved.usable

    def test_gpt_without_an_address_is_not_usable(self, tmp_path: Path) -> None:
        # OpenAI 系没有能假设的默认端点，地址必须配。
        root = _workspace(tmp_path, "version: 1\nprovider: gemini\n",
                          dotenv="OPENAI_API_KEY=k\n")

        assert not channel.resolve_image_channel("image_gpt", root=root).usable

    def test_a_trailing_v1_is_dropped_for_gemini(self, tmp_path: Path) -> None:
        # 后面还要拼 /v1beta/...，留着 /v1 会拼出一个不存在的路径。
        root = _workspace(tmp_path, _GEMINI_MAIN.replace("/g/tok", "/g/tok/v1"))

        assert channel.resolve_image_channel("image_gemini", root=root).base_url.endswith("/g/tok")

    def test_an_unknown_slot_is_never_usable(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _GEMINI_MAIN)

        assert not channel.resolve_image_channel("image_nope", root=root).usable


class TestConditionRendering:
    def test_an_available_tool_keeps_its_block(self) -> None:
        text = "head\n<<IF:gemini_image>>\nbody\n<<ENDIF>>\ntail\n"

        assert inject.render_conditions(text, {"gemini_image": True}) == "head\nbody\ntail\n"

    def test_an_unavailable_tool_loses_the_whole_block(self) -> None:
        text = "head\n<<IF:gpt_image_2>>\nbody\n<<ENDIF>>\ntail\n"

        assert inject.render_conditions(text, {"gpt_image_2": False}) == "head\ntail\n"

    def test_indented_markers_work(self) -> None:
        text = "head\n  <<IF:x>>\n  body\n  <<ENDIF>>\ntail\n"

        assert inject.render_conditions(text, {"x": True}) == "head\n  body\ntail\n"

    def test_blocks_are_independent(self) -> None:
        text = "<<IF:a>>\nA\n<<ENDIF>>\n<<IF:b>>\nB\n<<ENDIF>>\n"

        assert inject.render_conditions(text, {"a": False, "b": True}) == "B\n"

    def test_an_unknown_condition_is_left_alone(self) -> None:
        # 标记写错时多发一段，好过静默吞掉整块说明。
        text = "<<IF:typo>>\nbody\n<<ENDIF>>\n"

        assert inject.render_conditions(text, {"a": True}) == text

    def test_text_without_markers_is_untouched(self) -> None:
        text = "just a prompt\nwith lines\n"

        assert inject.render_conditions(text, {"a": True}) == text


class TestFlags:
    def test_two_live_tools_mean_multi(self) -> None:
        flags = inject.build_flags({"gpt_image_2": True, "gemini_image": True})

        assert flags["MULTI"] and not flags["SINGLE"] and not flags["NONE"]

    def test_one_live_tool_means_single(self) -> None:
        flags = inject.build_flags({"gpt_image_2": False, "gemini_image": True})

        assert flags["SINGLE"] and not flags["MULTI"]

    def test_nothing_configured(self) -> None:
        flags = inject.build_flags({"gpt_image_2": False, "gemini_image": False})

        assert flags["NONE"] and not flags["SINGLE"]


class TestAvailabilityText:
    def test_a_single_channel_is_stated_without_alternatives(self) -> None:
        text = inject.build_availability_text({"gpt_image_2": False, "gemini_image": True})

        assert "gemini_image" in text
        assert "gpt_image_2" not in text

    def test_the_default_is_mentioned_only_when_there_is_a_choice(self) -> None:
        both = {"gpt_image_2": True, "gemini_image": True}

        assert "默认 `gemini_image`" in inject.build_availability_text(both, "gemini_image")
        assert "默认" not in inject.build_availability_text(
            {"gpt_image_2": False, "gemini_image": True}, "gemini_image")

    def test_nothing_configured_tells_the_model_to_stop(self) -> None:
        text = inject.build_availability_text({"gpt_image_2": False, "gemini_image": False})

        assert "finish" in text


def _image_gen_prompt() -> str:
    import yaml

    data = yaml.safe_load((_ROOT / "defaults" / "nodes" / "draw.image_gen.yaml").read_text(encoding="utf-8"))
    return str(data["prompt"])


class TestRealPrompt:
    """拿真实节点提示词跑一遍，标记漏配对的话这里会先炸。"""

    def _prompt(self) -> str:
        return _image_gen_prompt()

    def test_only_gemini_leaves_no_trace_of_gpt(self) -> None:
        available = {"gpt_image_2": False, "gemini_image": True}
        rendered = inject.render_conditions(
            self._prompt().replace(
                "<<IMAGE_TOOLS_AVAILABILITY>>", inject.build_availability_text(available)),
            inject.build_flags(available),
        )

        assert "gpt_image_2" not in rendered
        assert "gemini_image" in rendered

    def test_every_marker_is_consumed(self) -> None:
        for gpt in (True, False):
            for gem in (True, False):
                available = {"gpt_image_2": gpt, "gemini_image": gem}
                rendered = inject.render_conditions(
                    self._prompt(), inject.build_flags(available))

                assert "<<IF:" not in rendered, (gpt, gem)
                assert "<<ENDIF>>" not in rendered, (gpt, gem)


def _drawtools_workspace(tmp_path: Path, config: str, settings: str | None) -> Path:
    """带 tools/ 的完整工作区。按路径加载靠的是模块位置，所以脚本得真的在；运行时配置在实例的 config/drawtools/。"""
    root = _workspace(tmp_path, config)
    (root / "tools" / "drawtools").mkdir(parents=True, exist_ok=True)
    # _channel 按需从自己旁边加载 _wire：只跑这几个用例时没有别处先把它导进来。
    for name in ("_channel.py", "_wire.py"):
        (root / "tools" / name).write_text(
            (_ROOT / "tools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    for name in ("common.py",):
        (root / "tools" / "drawtools" / name).write_text(
            (_ROOT / "tools" / "drawtools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    if settings is not None:
        target = root / "config" / "drawtools" / "settings.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(settings, encoding="utf-8")
    return root


class TestNovelaiAvailability:
    def test_a_key_in_settings_counts(self, tmp_path: Path) -> None:
        from clonoth_runtime import novelai_available

        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, "api:\n  api_key: nai-key\n")

        assert novelai_available(root)

    def test_the_env_variable_counts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # safe_subprocess_env 对 NOVELAI_API_KEY 开了白名单，这条回退是真的能用。
        from clonoth_runtime import novelai_available

        monkeypatch.setenv("NOVELAI_API_KEY", "from-env")
        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, "api:\n  api_key: ''\n")

        assert novelai_available(root)

    def test_nothing_configured(self, tmp_path: Path) -> None:
        from clonoth_runtime import novelai_available

        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, "api:\n  api_key: ''\n")

        assert not novelai_available(root)

    def test_falling_back_to_the_example_is_not_configured(self, tmp_path: Path) -> None:
        # settings.example.yaml 里 api_key 是空串，不能让它把示例当成已配。
        from clonoth_runtime import novelai_available

        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, None)

        assert not novelai_available(root)

    def test_a_workspace_without_drawtools(self, tmp_path: Path) -> None:
        from clonoth_runtime import novelai_available

        assert not novelai_available(_workspace(tmp_path, _GEMINI_MAIN))


class TestDisabledDrawNodes:
    """画不出图的节点不该留在委派目标里 —— 留着就是白烧一轮再失败。"""

    def _disabled(self, root: Path) -> set[str]:
        from engine.builtin.image_gen_gating import disabled_draw_nodes

        return disabled_draw_nodes(root)

    def test_novelai_without_a_key_is_dropped(self, tmp_path: Path) -> None:
        # 主渠道是 gemini，所以 image_gen 活着，novelai 该被摘掉。
        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, "api:\n  api_key: ''\n")

        assert self._disabled(root) == {"draw.novelai_planner"}

    def test_both_configured_drops_nothing(self, tmp_path: Path) -> None:
        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, "api:\n  api_key: nai-key\n")

        assert self._disabled(root) == set()

    def test_neither_configured_drops_both(self, tmp_path: Path) -> None:
        root = _drawtools_workspace(
            tmp_path, "version: 1\nprovider: openai\n", "api:\n  api_key: ''\n")

        assert self._disabled(root) == {"draw.image_gen", "draw.novelai_planner"}

    def test_image_gen_alone_is_dropped_when_only_novelai_works(self, tmp_path: Path) -> None:
        root = _drawtools_workspace(
            tmp_path, "version: 1\nprovider: openai\n", "api:\n  api_key: nai-key\n")

        assert self._disabled(root) == {"draw.image_gen"}

    def test_an_unreadable_models_file_keeps_the_image_tools_and_their_node(self, tmp_path: Path) -> None:
        # 判断不了不等于没配：摘掉的话模型只会说「没配生图」，真正的原因谁也看不到。
        from engine.builtin.image_gen_gating import disabled_image_tools

        root = _drawtools_workspace(tmp_path, _BROKEN_MODELS, "api:\n  api_key: ''\n")

        assert disabled_image_tools(root) == set()
        assert self._disabled(root) == {"draw.novelai_planner"}

    def test_an_unreadable_settings_file_keeps_the_novelai_node(self, tmp_path: Path) -> None:
        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, "api:\n  api_key: [nai-secret-key\n")

        assert self._disabled(root) == set()

    def test_a_settings_file_with_the_wrong_shape_keeps_the_novelai_node(self, tmp_path: Path) -> None:
        # 能解析、结构却不对：判断不了，不是没配，原因要说到是哪一项
        from clonoth_runtime import novelai_status

        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, "api:\n  api_key: nai-key\nparams: 5\n")
        status = novelai_status(root)

        assert status.problem.endswith("settings.yaml 读不了：params 不是映射（key: value）")
        assert not status.unconfigured
        assert self._disabled(root) == set()


class TestImageToolStatus:
    def test_a_configured_tool_is_available(self, tmp_path: Path) -> None:
        from clonoth_runtime import image_tool_status

        status = image_tool_status(_drawtools_workspace(tmp_path, _OPENAI_MAIN, None), "gpt_image_2")

        assert (status.available, status.problem, status.unconfigured) == (True, "", False)

    def test_a_tool_with_nothing_to_borrow_is_unconfigured(self, tmp_path: Path) -> None:
        from clonoth_runtime import image_tool_status

        status = image_tool_status(_drawtools_workspace(tmp_path, _OPENAI_MAIN, None), "gemini_image")

        assert (status.available, status.problem, status.unconfigured) == (False, "", True)

    def test_an_unreadable_models_file_is_a_problem_not_a_verdict(self, tmp_path: Path) -> None:
        from clonoth_runtime import image_tool_available, image_tool_status

        root = _drawtools_workspace(tmp_path, _BROKEN_MODELS, None)
        status = image_tool_status(root, "gpt_image_2")

        assert status.problem.startswith("config/models.yaml 读不了")
        assert "sk-secret-in-a-bad-line" not in status.problem
        assert not status.available and not status.unconfigured
        assert image_tool_available(root, "gpt_image_2") is False

    def test_novelai_reports_an_unreadable_settings_file(self, tmp_path: Path) -> None:
        from clonoth_runtime import novelai_available, novelai_status

        root = _drawtools_workspace(tmp_path, _GEMINI_MAIN, "api:\n  api_key: [nai-secret-key\n")
        status = novelai_status(root)

        assert "settings.yaml 读不了" in status.problem
        assert "nai-secret-key" not in status.problem
        assert not status.available and not status.unconfigured
        assert novelai_available(root) is False

    def test_a_judgement_that_breaks_is_a_problem_not_a_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 判定自己出错时说「没配」是在撒谎；异常消息里可能带着配置里的值，只报类型
        import clonoth_runtime
        from engine.builtin.image_gen_gating import disabled_image_tools

        root = _drawtools_workspace(tmp_path, _OPENAI_MAIN, None)

        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise KeyError("sk-quoted-by-the-error")

        monkeypatch.setattr(clonoth_runtime._image_channel_module(root), "resolve_image_channel", broken)
        status = clonoth_runtime.image_tool_status(root, " gpt_image_2 ")

        assert status.problem == "判断 gpt_image_2 能不能用时出错（KeyError）"
        assert not status.available and not status.unconfigured
        assert disabled_image_tools(root) == set()

    def test_the_default_channel_comes_through_the_same_reader(self, tmp_path: Path) -> None:
        from clonoth_runtime import image_default_channel

        root = _drawtools_workspace(tmp_path, _OPENAI_MAIN + "image_gen:\n  default_channel: gemini_image\n", None)

        assert image_default_channel(root) == "gemini_image"

    @pytest.mark.parametrize("extra", ["image_gen:\n  default_channel: nai\n", "image_gen: on\n", ""])
    def test_anything_but_an_image_tool_name_is_no_default(self, tmp_path: Path, extra: str) -> None:
        from clonoth_runtime import image_default_channel

        assert image_default_channel(_drawtools_workspace(tmp_path, _OPENAI_MAIN + extra, None)) == ""

    def test_an_unreadable_models_file_has_no_default(self, tmp_path: Path) -> None:
        from clonoth_runtime import image_default_channel

        assert image_default_channel(_drawtools_workspace(tmp_path, _BROKEN_MODELS, None)) == ""


class TestAnUnreadableModelsFile:
    """models.yaml 写坏了：常驻进程沿用上一份能读的，没有上一份就说清读不了，不拿环境变量拼一条渠道去发计费请求。"""

    def test_the_reason_says_where_but_not_what(self, tmp_path: Path) -> None:
        root = _workspace(
            tmp_path, _BROKEN_MODELS, dotenv="OPENAI_API_KEY=k-env\nOPENAI_BASE_URL=https://env.example.com/v1\n",
        )

        resolved = channel.resolve_image_channel("image_gpt", root=root)

        assert not resolved.usable
        assert resolved.error.startswith("config/models.yaml 读不了：第 5 行第 11 列：")
        assert "sk-secret-in-a-bad-line" not in resolved.error
        assert (resolved.api_key, resolved.base_url) == ("", "")

    def test_the_vision_channel_reports_it_too(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _BROKEN_MODELS, dotenv="OPENAI_API_KEY=k-env\n")

        result = channel.resolve_vision_channel(root=root)

        assert not result.usable
        assert result.error.startswith("config/models.yaml 读不了")

    def test_a_later_breakage_keeps_the_last_good_reading(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _workspace(tmp_path, _OPENAI_MAIN)
        assert channel.resolve_image_channel("image_gpt", root=root).usable
        (root / "config" / "models.yaml").write_text(_BROKEN_MODELS, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger=channel.__name__):
            first = channel.resolve_image_channel("image_gpt", root=root)
            again = channel.resolve_image_channel("image_gpt", root=root)

        assert first.usable and first.api_key == "main-key"
        assert again == first
        warnings = [record.getMessage() for record in caplog.records if "沿用上一份" in record.getMessage()]
        assert len(warnings) == 1
        assert "sk-secret-in-a-bad-line" not in warnings[0]

    def test_a_deleted_file_is_not_brought_back_from_memory(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _OPENAI_MAIN)
        assert channel.resolve_image_channel("image_gpt", root=root).usable

        (root / "config" / "models.yaml").unlink()
        resolved = channel.resolve_image_channel("image_gpt", root=root)

        assert not resolved.usable
        assert resolved.error == ""

    def test_a_repaired_file_is_read_again(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, _BROKEN_MODELS)
        assert channel.resolve_image_channel("image_gpt", root=root).error

        (root / "config" / "models.yaml").write_text(_OPENAI_MAIN, encoding="utf-8")
        resolved = channel.resolve_image_channel("image_gpt", root=root)

        assert resolved.usable and resolved.error == ""

    def test_what_is_handed_out_cannot_change_the_cache(self, tmp_path: Path) -> None:
        path = _workspace(tmp_path, _OPENAI_MAIN) / "config" / "models.yaml"
        channel.read_yaml_document(path, label="config/models.yaml").data["openai"]["api_key"] = "tampered"

        assert channel.read_yaml_document(path, label="config/models.yaml").data["openai"]["api_key"] == "main-key"

    @pytest.mark.parametrize("raw,said", [
        (b"- just\n- a list\n", "顶层不是映射"),
        # PyYAML 拿它造日期时抛的 ValueError，消息里带着这个值；换成只报位置和是哪种值
        (b"when: 2026-13-45\n", "第 1 行第 7 列：值不是合法的 timestamp"),
        (b"\xff\xfeabc: 1\n", "不是 UTF-8 编码"),
        # 别名、tag 照抄文件里写的，密码以 * 或 ! 开头又没加引号时抄出来的就是密码
        (b"api_key: *Pa55-2026-13-45\n", "found undefined alias '***'"),
        (b"api_key: !Pass-2026-13-45 x\n", "could not determine a constructor for the tag '***'"),
    ])
    def test_an_odd_file_is_unreadable_without_being_quoted(self, tmp_path: Path, raw: bytes, said: str) -> None:
        path = tmp_path / "odd.yaml"
        path.write_bytes(raw)

        document = channel.read_yaml_document(path, label="odd.yaml", keep_last_good=False)

        assert document.data == {}
        assert document.problem.startswith("odd.yaml 读不了")
        assert said in document.problem
        assert "2026-13-45" not in document.problem

    @pytest.mark.parametrize("text", [
        "openai:\n  api_key: [sk-secret-a\n  base_url: https://gw.example.com/v1\n",
        "openai:\n  api_key: sk-secret-b: x\n",
        'openai:\n  api_key: "sk-secret-c\n',
        "when: 2026-02-30\n",
        "api_key: *sk-secret-d\n",
    ])
    def test_the_reason_is_word_for_word_the_main_process_one(self, tmp_path: Path, text: str) -> None:
        # 屏蔽只有 config_layout 那一份：工具侧报的原因和主进程一字不差，没有第二套写法会漏。
        import config_layout
        import yaml

        path = tmp_path / "same.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(yaml.YAMLError) as caught:
            config_layout.load_yaml_text(text)

        document = channel.read_yaml_document(path, label="same.yaml", keep_last_good=False)

        assert document.problem == f"same.yaml 读不了：{config_layout.yaml_error_text(caught.value)}"
        assert "sk-secret" not in document.problem

    @pytest.mark.parametrize("raw", [b"", b"# only a comment\n"])
    def test_an_empty_file_is_an_empty_table(self, tmp_path: Path, raw: bytes) -> None:
        path = tmp_path / "empty.yaml"
        path.write_bytes(raw)

        assert channel.read_yaml_document(path, label="empty.yaml") == channel.YamlDocument({})

    def test_a_first_read_failure_is_only_logged_when_the_caller_says_how_it_copes(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # 脚本工具每次都是新进程，原因由它交给模型；常驻进程的调用方才需要日志。
        quiet, noted = tmp_path / "quiet.yaml", tmp_path / "noted.yaml"
        for path in (quiet, noted):
            path.write_bytes(b"k: [1\n")

        with caplog.at_level(logging.WARNING, logger=channel.__name__):
            channel.read_yaml_document(quiet, label="quiet.yaml")
            channel.read_yaml_document(noted, label="noted.yaml", unread_note="按空表算")
            channel.read_yaml_document(noted, label="noted.yaml", unread_note="按空表算")

        messages = [record.getMessage() for record in caplog.records]
        noted_messages = [message for message in messages if "noted.yaml" in message]
        assert not any("quiet.yaml" in message for message in messages)
        assert len(noted_messages) == 1
        assert noted_messages[0].endswith("按空表算")


class TestPromptWhenChannelsCannotBeJudged:
    def test_the_text_keeps_every_possible_tool_and_says_why(self) -> None:
        text = inject.build_unjudgeable_text(
            {"gpt_image_2": True, "gemini_image": True}, ["config/models.yaml 读不了：x（第 3 行第 1 列）"],
        )

        assert "判断不了" in text and "第 3 行" in text
        assert "`gpt_image_2`" in text and "`gemini_image`" in text
        assert "finish" not in text

    def test_a_tool_known_to_be_unconfigured_is_left_out(self) -> None:
        text = inject.build_unjudgeable_text({"gpt_image_2": False, "gemini_image": True}, ["p"])

        assert "gpt_image_2" not in text

    def test_the_injector_keeps_both_tools_when_models_yaml_is_broken(self, tmp_path: Path) -> None:
        root = _drawtools_workspace(tmp_path, _BROKEN_MODELS, None)
        ctx = SimpleNamespace(
            node=SimpleNamespace(id="draw.image_gen"),
            rctx=SimpleNamespace(workspace_root=root),
            messages=[{"role": "system", "content": _image_gen_prompt()}],
        )

        result = asyncio.run(inject.ImageGenAvailabilityInjector().handle(ctx))

        rendered = ctx.messages[0]["content"]
        assert result is not None
        assert "判断不了" in rendered
        assert "gpt_image_2" in rendered and "gemini_image" in rendered
        assert "<<IF:" not in rendered and "<<IMAGE_TOOLS_AVAILABILITY>>" not in rendered
        assert "sk-secret-in-a-bad-line" not in rendered

    def test_a_reason_is_logged_once_while_it_lasts_and_again_when_it_comes_back(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 每一步都会重新判一遍：不能每步都记，好了再坏也不能一声不吭
        monkeypatch.setattr(inject, "_warned_problems", set())

        with caplog.at_level(logging.WARNING, logger=inject.__name__):
            for problems in (["p"], ["p"], [], ["p"]):
                inject._warn_unjudgeable(problems)

        assert len([record for record in caplog.records if "判断不了生图渠道" in record.getMessage()]) == 2


class TestVisionChannel:
    """看图渠道。读图工具和表情包打标共用这一份，两边判定必须一致。"""

    def _resolve(self, root: Path):
        return channel.resolve_vision_channel(root=root)

    def test_dedicated_slot_wins(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, """
version: 1
provider: openai
openai:
  base_url: https://main.example.com/v1
  api_key: main-key
system_models:
  image:
    base_url: https://vision.example.com/v1
    api_key: vision-key
    model: my-vision
""")
        result = self._resolve(root)

        assert result.usable
        assert result.base_url == "https://vision.example.com/v1"
        assert result.api_key == "vision-key"
        assert result.model == "my-vision"

    def test_follows_an_openai_compatible_main(self, tmp_path: Path) -> None:
        # 常见部署就是同一个中转站换个模型名，地址和 key 该借就借。
        root = _workspace(tmp_path, """
version: 1
provider: openai
openai:
  base_url: https://main.example.com/v1
  api_key: main-key
""")
        result = self._resolve(root)

        assert result.usable
        assert result.base_url == "https://main.example.com/v1"
        assert result.api_key == "main-key"

    def test_model_never_follows_the_main_channel(self, tmp_path: Path) -> None:
        # 主渠道配的是聊天模型，多半正是那个看不了图的。
        root = _workspace(tmp_path, """
version: 1
provider: openai
openai:
  base_url: https://main.example.com/v1
  api_key: main-key
  model: some-text-only-model
""")
        assert self._resolve(root).model != "some-text-only-model"

    def test_a_non_openai_main_is_borrowed_with_its_own_wire(self, tmp_path: Path) -> None:
        # 整条跟随时格式也跟着一起借，不能拿别家的地址发 OpenAI 的请求体。
        root = _workspace(tmp_path, """
version: 1
provider: gemini
gemini:
  base_url: https://generativelanguage.googleapis.com
  api_key: main-key
""")
        result = self._resolve(root)

        assert result.usable
        assert result.wire == "gemini"
        assert result.family == "gemini"
        assert result.base_url == "https://generativelanguage.googleapis.com"
        assert ":generateContent" in result.endpoint()

    def test_a_main_without_a_model_default_says_so(self, tmp_path: Path) -> None:
        # 这家没有能替它猜的模型名，与其发一个别家的名字去撞 404，不如直说。
        root = _workspace(tmp_path, """
version: 1
provider: anthropic
anthropic:
  base_url: https://api.anthropic.com
  api_key: main-key
""")
        result = self._resolve(root)

        assert not result.usable
        assert "model" in result.error

    def test_an_explicit_provider_decides_the_wire(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, """
version: 1
provider: openai
openai:
  base_url: https://main.example.com/v1
  api_key: main-key
system_models:
  image:
    provider: anthropic
    base_url: https://api.anthropic.com
    api_key: vision-key
    model: claude-4-vision
""")
        result = self._resolve(root)

        assert result.usable and result.wire == "anthropic"
        assert result.endpoint() == "https://api.anthropic.com/v1/messages"

    def test_a_slot_url_without_a_provider_is_treated_as_openai(self, tmp_path: Path) -> None:
        # 地址和格式是绑死的一对：自己指了地址就不能假定对面还说主渠道那套话。
        root = _workspace(tmp_path, """
version: 1
provider: gemini
gemini:
  base_url: https://generativelanguage.googleapis.com
  api_key: main-key
system_models:
  image:
    base_url: https://relay.example.com/v1
    api_key: vision-key
""")
        result = self._resolve(root)

        assert result.usable and result.wire == "openai"
        assert result.endpoint() == "https://relay.example.com/v1/chat/completions"

    def test_a_gemini_slot_does_not_gain_a_v1_suffix(self, tmp_path: Path) -> None:
        # 端点自己拼 /v1beta/models/...，地址上再带一个版本段就发到不存在的路径去了。
        root = _workspace(tmp_path, """
version: 1
provider: openai
system_models:
  image:
    provider: gemini
    base_url: https://generativelanguage.googleapis.com
    api_key: k
    model: flash
""")
        result = self._resolve(root)

        assert result.base_url == "https://generativelanguage.googleapis.com"
        assert result.endpoint().endswith("/v1beta/models/flash:generateContent")

    def test_it_still_resolves_when_loaded_by_path_instead_of_as_a_package(
        self, tmp_path: Path,
    ) -> None:
        """_channel.py 有三种被加载的方式，线格式那份不能靠模块级 import 拿。

        包内 `tools._channel`、工具子进程里的裸 `_channel`、以及按路径加载 —— 这个文件
        用的正是第三种，模块级 `from . import _wire` 会在这里直接炸掉整个模块。
        """
        assert not channel.__package__
        root = _workspace(tmp_path, """
version: 1
provider: openai
system_models:
  image:
    provider: gemini
    base_url: https://generativelanguage.googleapis.com
    api_key: k
    model: flash
""")
        result = self._resolve(root)

        assert result.wire == "gemini" and result.family == "gemini"
        assert ":generateContent" in result.endpoint()

    def test_missing_key_is_reported_not_guessed(self, tmp_path: Path) -> None:
        result = self._resolve(_workspace(tmp_path, "version: 1\nprovider: openai\n"))

        assert not result.usable and result.error

    def test_key_can_come_from_dotenv(self, tmp_path: Path) -> None:
        root = _workspace(
            tmp_path, "version: 1\nprovider: openai\n", "OPENAI_API_KEY=from-dotenv\n",
        )
        result = self._resolve(root)

        assert result.usable and result.api_key == "from-dotenv"

    def test_bare_host_gets_a_v1_suffix(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, """
version: 1
provider: openai
system_models:
  image:
    base_url: https://vision.example.com
    api_key: k
""")
        assert self._resolve(root).base_url == "https://vision.example.com/v1"

    def test_existing_version_segment_is_left_alone(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, """
version: 1
provider: openai
system_models:
  image:
    base_url: https://vision.example.com/v1beta/openai
    api_key: k
""")
        assert self._resolve(root).base_url == "https://vision.example.com/v1beta/openai"

    def test_endpoint_appends_chat_completions(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, """
version: 1
provider: openai
system_models:
  image:
    base_url: https://vision.example.com/v1
    api_key: k
""")
        assert self._resolve(root).endpoint() == "https://vision.example.com/v1/chat/completions"


_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nnot-really-a-png").decode("ascii")


@pytest.fixture()
def image_relay() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """本地的兼容网关：记下每次请求的路径、认证头和请求体，回一张 base64 的图。"""
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 的方法名
            payload = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization") or "",
                "body": json.loads(payload or b"{}"),
            })
            body = json.dumps({"data": [{"b64_json": _PNG}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_gpt_image(workspace: Path) -> dict[str, Any]:
    """照注册表起工具的样子跑：cwd 是工作区，环境里的密钥已经剥掉。"""
    # 开发机上的代理和同名渠道变量不能替测试里的配置做主。
    env = {
        key: value for key, value in safe_subprocess_env().items()
        if not key.upper().endswith("_PROXY") and not key.upper().startswith(("CLONOTH_IMAGE_GPT_", "OPENAI_"))
    }
    env.update({"NO_PROXY": "*", "PYTHONIOENCODING": "utf-8"})
    done = subprocess.run(
        [sys.executable, str(_ROOT / "tools" / "gpt_image_2.py")],
        input=json.dumps({"prompt": "a cat", "size": "1024x1024", "quality": "low"}),
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=workspace, env=env,
    )
    assert done.stdout.strip(), done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


class TestTheGptToolEndToEnd:
    def test_a_responses_main_channel_gets_its_images_endpoint(
        self, tmp_path: Path, image_relay: tuple[str, list[dict[str, Any]]],
    ) -> None:
        base_url, seen = image_relay
        root = _workspace(tmp_path, _RESPONSES_MAIN.replace("https://gw.example.com/v1", f"{base_url}/v1"))

        result = _run_gpt_image(root)

        assert result["ok"] is True, result
        assert [(sent["path"], sent["authorization"], sent["body"]["model"]) for sent in seen] == [
            ("/v1/images/generations", "Bearer main-key", "gpt-image-2"),
        ]
        assert (root / result["data"]["path"]).is_file()

    def test_an_unreadable_models_file_stops_it_before_any_request(
        self, tmp_path: Path, image_relay: tuple[str, list[dict[str, Any]]],
    ) -> None:
        # 环境变量里的地址和密钥够拼出一条渠道，但读不了配置时谁也说不准它是不是该用的那条。
        base_url, seen = image_relay
        root = _workspace(tmp_path, _BROKEN_MODELS, dotenv=f"OPENAI_API_KEY=k-env\nOPENAI_BASE_URL={base_url}/v1\n")

        result = _run_gpt_image(root)

        assert result["ok"] is False
        assert result["error"].startswith("config/models.yaml 读不了")
        assert "sk-secret-in-a-bad-line" not in result["error"]
        assert seen == []
