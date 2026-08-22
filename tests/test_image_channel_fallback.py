"""生图渠道的回退链，以及提示词按可用渠道的裁剪。

难点是「借主渠道」的边界：地址借错家不会报地址错误，只会得到一个跟配置毫无关系的
404。所以格式对不上时宁可判成没配。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "config.yaml").write_text(config, encoding="utf-8")
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

    def test_deepseek_counts_as_openai_compatible(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, """
version: 1
provider: deepseek
deepseek:
  base_url: https://ds.example.com/v1
  api_key: ds-key
  model: deepseek-v4
""")
        assert channel.resolve_image_channel("image_gpt", root=root).usable

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


class TestRealPrompt:
    """拿真实节点提示词跑一遍，标记漏配对的话这里会先炸。"""

    def _prompt(self) -> str:
        import yaml

        data = yaml.safe_load(
            (_ROOT / "config" / "nodes" / "draw.image_gen.yaml").read_text(encoding="utf-8"))
        return str(data["prompt"])

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
    """带 tools/ 的完整工作区。按路径加载靠的是模块位置，所以文件得真的在。"""
    root = _workspace(tmp_path, config)
    (root / "tools" / "drawtools").mkdir(parents=True, exist_ok=True)
    for name in ("_channel.py",):
        (root / "tools" / name).write_text(
            (_ROOT / "tools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    for name in ("common.py", "settings.example.yaml"):
        (root / "tools" / "drawtools" / name).write_text(
            (_ROOT / "tools" / "drawtools" / name).read_text(encoding="utf-8"), encoding="utf-8")
    if settings is not None:
        (root / "tools" / "drawtools" / "settings.yaml").write_text(settings, encoding="utf-8")
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
        用的正是第三种，模块级 `from . import _vision_wire` 会在这里直接炸掉整个模块。
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
