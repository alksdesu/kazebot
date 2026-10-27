"""节点、备选链、系统槽位指向一个命名渠道时的解析。

以前 provider 只能填线格式名，填渠道名会被静默清空或整条跳过。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
from clonoth_runtime import load_models_config, resolve_channel, resolve_system_model  # noqa: E402
from engine.builtin import fallback_provider as fallback_module  # noqa: E402
from engine.builtin.fallback_provider import _resolve_fallback_entry  # noqa: E402
from engine.inference import llm_call  # noqa: E402
from engine.model import resolve_provider  # noqa: E402
from engine.node import Node, load_node  # noqa: E402
from providers.base import ProviderResponse  # noqa: E402

CONFIG = """version: 1
provider: openai
openai:
  base_url: https://api.openai.com/v1
  api_key: sk-openai
  model: gpt-5.4
gemini-中转A:
  type: gemini
  base_url: https://relay-a.example/v1
  api_key: sk-relay-a
  model: gemini-3-pro
gemini-官方:
  type: gemini
  base_url: https://generativelanguage.googleapis.com
  api_key: sk-official
  model: gemini-3-flash
deepseek:
  base_url: https://api.deepseek.com
  api_key: sk-deepseek
  model: deepseek-v4
带参数的:
  type: anthropic
  base_url: https://api.anthropic.com
  api_key: sk-ant
  options:
    thinking: true
不是渠道:
  note: 只是一段备注
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "models.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


class TestResolveChannel:
    def test_a_named_channel_reports_its_declared_wire(self, root: Path) -> None:
        channel = resolve_channel(root, "gemini-中转A")
        assert channel.known
        assert channel.wire == "gemini"
        assert channel.base_url == "https://relay-a.example/v1"
        assert channel.api_key == "sk-relay-a"
        assert channel.model == "gemini-3-pro"

    def test_a_block_without_type_falls_back_to_its_name(self, root: Path) -> None:
        # 老配置里块名本来就是家族名，没有 type 也得照旧能用。
        assert resolve_channel(root, "deepseek").wire == "deepseek"

    def test_a_bare_wire_name_is_not_a_channel(self, root: Path) -> None:
        channel = resolve_channel(root, "anthropic")
        assert not channel.known
        assert channel.wire == "anthropic"
        assert channel.base_url == ""

    def test_the_name_is_case_sensitive(self, root: Path) -> None:
        # 块名原样存盘，转小写就查不到了。
        assert not resolve_channel(root, "GEMINI-中转A").known

    def test_a_non_channel_block_is_not_mistaken_for_one(self, root: Path) -> None:
        assert not resolve_channel(root, "不是渠道").known

    def test_meta_keys_are_never_channels(self, root: Path) -> None:
        # provider / fallbacks 这些顶层键不是渠道，认成渠道会解析出一堆空值。
        assert not resolve_channel(root, "fallbacks").known
        assert not resolve_channel(root, "provider").known

    def test_an_empty_name_resolves_to_nothing(self, root: Path) -> None:
        channel = resolve_channel(root, "")
        assert not channel.known
        assert channel.wire == ""

    def test_env_refs_are_expanded(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("RELAY_KEY", "sk-from-env")
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "models.yaml").write_text(
            "relay:\n  type: openai\n  api_key: $ENV{RELAY_KEY}\n", encoding="utf-8",
        )
        assert resolve_channel(tmp_path, "relay").api_key == "sk-from-env"


class TestNodeKeepsWhatWasWritten:
    def _node(self, root: Path, body: str) -> Node:
        (root / "config" / "nodes").mkdir(parents=True, exist_ok=True)
        (root / "config" / "nodes" / "n.yaml").write_text(body, encoding="utf-8")
        node = load_node(root, "n")
        assert node is not None
        return node

    def test_a_channel_name_survives_loading(self, root: Path) -> None:
        # 以前这里按线格式白名单过滤，渠道名会被静默换成空串。
        assert self._node(root, "id: n\ntype: ai\nprovider: gemini-中转A\n").provider == "gemini-中转A"

    def test_the_case_is_preserved(self, root: Path) -> None:
        assert self._node(root, "id: n\ntype: ai\nprovider: Relay-A\n").provider == "Relay-A"

    def test_an_absent_provider_is_still_empty(self, root: Path) -> None:
        assert self._node(root, "id: n\ntype: ai\n").provider == ""


class TestNodeProviderResolution:
    def _node(self, **kwargs) -> Node:
        return Node(id="n", name="n", type="ai", **kwargs)

    def test_pointing_at_a_named_channel_borrows_everything(self, root: Path) -> None:
        rp = resolve_provider(root, self._node(provider="gemini-中转A"), "gpt-5.4")
        assert rp.provider_type == "gemini"
        assert rp.base_url == "https://relay-a.example/v1"
        assert rp.api_key == "sk-relay-a"
        assert rp.model == "gemini-3-pro"

    def test_the_node_own_fields_beat_the_channel_block(self, root: Path) -> None:
        rp = resolve_provider(
            root,
            self._node(provider="gemini-中转A", model="gemini-3-flash",
                       base_url="https://node.example/v1"),
            "gpt-5.4",
        )
        assert rp.provider_type == "gemini"
        assert rp.model == "gemini-3-flash"
        assert rp.base_url == "https://node.example/v1"
        # 节点没写 key，仍然从渠道块继承。
        assert rp.api_key == "sk-relay-a"

    def test_two_channels_of_the_same_family_stay_apart(self, root: Path) -> None:
        a = resolve_provider(root, self._node(provider="gemini-中转A"), "gpt-5.4")
        b = resolve_provider(root, self._node(provider="gemini-官方"), "gpt-5.4")
        assert a.provider_type == b.provider_type == "gemini"
        assert a.base_url != b.base_url
        assert a.api_key != b.api_key

    def test_a_bare_wire_name_still_works(self, root: Path) -> None:
        # 只想换请求格式、地址走环境变量的节点仍然这么写。
        rp = resolve_provider(root, self._node(provider="anthropic"), "gpt-5.4")
        assert rp.provider_type == "anthropic"
        assert rp.base_url is None
        assert rp.api_key is None

    def test_an_old_config_now_inherits_its_block(self, root: Path) -> None:
        # 行为变更：以前只取 wire，url/key 得靠 DEEPSEEK_API_KEY 之类的环境变量。
        rp = resolve_provider(root, self._node(provider="deepseek"), "gpt-5.4")
        assert rp.provider_type == "deepseek"
        assert rp.base_url == "https://api.deepseek.com"
        assert rp.api_key == "sk-deepseek"

    def test_no_provider_follows_the_active_channel(self, root: Path) -> None:
        rp = resolve_provider(root, self._node(), "gpt-5.4", provider_default_type="openai")
        assert rp.provider_type == "openai"
        assert rp.model == "gpt-5.4"
        assert rp.base_url is None

    def test_the_channel_options_come_along(self, root: Path) -> None:
        rp = resolve_provider(root, self._node(provider="带参数的"), "gpt-5.4")
        assert rp.channel_options == {"thinking": True}

    def test_an_unknown_name_is_left_for_the_registry_to_reject(self, root: Path) -> None:
        # 拼错的名字不在这里吞掉：runner 拿它查 registry 落空时会打一行日志。
        rp = resolve_provider(root, self._node(provider="gemini-中转B"), "gpt-5.4")
        assert rp.provider_type == "gemini-中转b"
        assert rp.base_url is None


class TestSessionOverrideBeatsTheNode:
    def _node(self) -> Node:
        return Node(id="n", name="n", type="ai", provider="deepseek")

    def test_the_override_channel_replaces_the_node_one(self, root: Path) -> None:
        rp = resolve_provider(
            root, self._node(), "gpt-5.4",
            session_override={"provider": "gemini-中转A"},
        )
        assert rp.provider_type == "gemini"
        assert rp.base_url == "https://relay-a.example/v1"

    def test_an_inline_key_is_not_overwritten_by_the_block(self, root: Path) -> None:
        # 会话里临时贴一把 key 的用法，不能被渠道块顶掉。
        rp = resolve_provider(
            root, self._node(), "gpt-5.4",
            session_override={"provider": "gemini-中转A", "api_key": "sk-session"},
        )
        assert rp.api_key == "sk-session"
        assert rp.base_url == "https://relay-a.example/v1"


class TestFallbackEntries:
    def _cfg(self) -> dict:
        import yaml
        return yaml.safe_load(CONFIG)

    def test_a_named_channel_resolves_to_its_wire(self) -> None:
        # 以前这里把渠道名原样当线格式送进 registry，整条备选被静默跳过。
        entry = _resolve_fallback_entry({"provider": "gemini-中转A"}, self._cfg())
        assert entry["wire"] == "gemini"
        assert entry["provider"] == "gemini-中转A"
        assert entry["base_url"] == "https://relay-a.example/v1"
        assert entry["api_key"] == "sk-relay-a"
        assert entry["model"] == "gemini-3-pro"

    def test_a_family_named_block_still_works(self) -> None:
        entry = _resolve_fallback_entry({"provider": "deepseek"}, self._cfg())
        assert entry["wire"] == "deepseek"
        assert entry["base_url"] == "https://api.deepseek.com"

    def test_the_entry_overrides_the_block(self) -> None:
        entry = _resolve_fallback_entry(
            {"provider": "gemini-中转A", "model": "gemini-3-flash"}, self._cfg(),
        )
        assert entry["model"] == "gemini-3-flash"
        assert entry["api_key"] == "sk-relay-a"

    def test_block_options_are_inherited_then_overlaid(self) -> None:
        entry = _resolve_fallback_entry(
            {"provider": "带参数的", "options": {"max_tokens": 100}}, self._cfg(),
        )
        assert entry["options"] == {"thinking": True, "max_tokens": 100}

    def test_an_unconfigured_name_yields_no_credentials(self) -> None:
        entry = _resolve_fallback_entry({"provider": "gemini"}, self._cfg())
        assert entry["wire"] == "gemini"
        assert entry["base_url"] == ""
        assert entry["api_key"] == ""


BROKEN = "gemini-中转A: [oops\n"
FALLBACK_CONFIG = CONFIG + """fallbacks:
  - provider: gemini-中转A
system_models:
  intent:
    provider: gemini-官方
    model: gemini-3-flash-lite
"""


def _save(path: Path, text: str) -> None:
    # 照生产里的保存路径写：同目录临时文件替换，每次换 inode，同一个时钟刻度里连写两次也认得出来。
    config_layout.write_config_text(path, text)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == clonoth_runtime._log.name and r.levelno == logging.WARNING]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(clonoth_runtime, "_MODELS_CONFIG", clonoth_runtime._MODELS_CONFIG.fresh())
    config_layout.models_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestTheEngineCopyOfTheModelsFile:
    """Supervisor 给主渠道的那份配置在文件写坏时停在上一份能用的，engine 自己读的这份也得一样，不然两边各用各的。"""

    def test_a_broken_rewrite_keeps_the_last_good_copy(self, workspace: Path, caplog) -> None:
        path = config_layout.models_path(workspace)
        _save(path, CONFIG)
        assert resolve_channel(workspace, "gemini-中转A").known

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            _save(path, BROKEN)
            channel = resolve_channel(workspace, "gemini-中转A")

        assert (channel.wire, channel.base_url) == ("gemini", "https://relay-a.example/v1")
        assert len(_warnings(caplog)) == 1
        assert "沿用上一份能用的" in _warnings(caplog)[0]

    def test_the_warning_names_the_place_but_not_the_line(self, workspace: Path, caplog) -> None:
        # 坏的那一行多半就是密钥：日志会被收进诊断包、贴进工单。
        path = config_layout.models_path(workspace)
        _save(path, CONFIG)
        load_models_config(workspace)

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            _save(path, CONFIG.replace("api_key: sk-relay-a", "api_key: sk-relay-a: oops"))
            load_models_config(workspace)

        assert len(_warnings(caplog)) == 1
        assert "YAML 解析失败：第 10 行第 " in _warnings(caplog)[0]
        assert "sk-relay-a" not in _warnings(caplog)[0]

    def test_a_value_that_cannot_be_built_keeps_the_last_good_copy(self, workspace: Path, caplog) -> None:
        # PyYAML 造日期失败抛的是 ValueError：以前直接从 load_models_config 漏出去，每次解析渠道都崩。
        path = config_layout.models_path(workspace)
        _save(path, CONFIG)
        assert resolve_channel(workspace, "gemini-中转A").known

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            _save(path, CONFIG + "note: 2026-02-30\n")
            channel = resolve_channel(workspace, "gemini-中转A")

        assert channel.base_url == "https://relay-a.example/v1"
        assert len(_warnings(caplog)) == 1
        assert "值不是合法的 timestamp" in _warnings(caplog)[0]
        assert "2026-02-30" not in _warnings(caplog)[0]

    def test_a_top_level_that_is_not_a_mapping_counts_as_broken(self, workspace: Path) -> None:
        path = config_layout.models_path(workspace)
        _save(path, CONFIG)
        assert load_models_config(workspace)["deepseek"]["model"] == "deepseek-v4"

        _save(path, "- gemini-中转A\n- deepseek\n")

        assert load_models_config(workspace)["deepseek"]["model"] == "deepseek-v4"

    def test_fixing_the_file_takes_effect_again(self, workspace: Path) -> None:
        path = config_layout.models_path(workspace)
        _save(path, CONFIG)
        assert resolve_channel(workspace, "gemini-中转A").known
        _save(path, BROKEN)
        assert resolve_channel(workspace, "gemini-中转A").base_url == "https://relay-a.example/v1"

        _save(path, CONFIG.replace("https://relay-a.example/v1", "https://relay-b.example/v1"))

        assert resolve_channel(workspace, "gemini-中转A").base_url == "https://relay-b.example/v1"

    def test_a_file_broken_from_the_start_counts_as_empty(self, workspace: Path, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            _save(config_layout.models_path(workspace), BROKEN)
            assert load_models_config(workspace) == {}
            assert not resolve_channel(workspace, "gemini-中转A").known

        assert len(_warnings(caplog)) == 1
        assert "按空配置算" in _warnings(caplog)[0]

    def test_each_broken_version_is_reported_once(self, workspace: Path, caplog) -> None:
        path = config_layout.models_path(workspace)
        _save(path, CONFIG)
        load_models_config(workspace)

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            _save(path, BROKEN)
            for _ in range(3):
                resolve_channel(workspace, "gemini-中转A")
                resolve_system_model(workspace, "intent")
            assert len(_warnings(caplog)) == 1

            _save(path, "deepseek: [oops, again\n")
            load_models_config(workspace)
            load_models_config(workspace)

        assert len(_warnings(caplog)) == 2
        # 连着坏两版，沿用的仍是最后一份能用的。
        assert load_models_config(workspace)["deepseek"]["model"] == "deepseek-v4"

    def test_a_missing_file_counts_as_empty_even_after_a_good_read(self, workspace: Path, caplog) -> None:
        path = config_layout.models_path(workspace)
        _save(path, CONFIG)
        assert load_models_config(workspace)

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            path.unlink()
            assert load_models_config(workspace) == {}

        assert _warnings(caplog) == []

    def test_an_empty_file_is_an_empty_config(self, workspace: Path, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            _save(config_layout.models_path(workspace), "")
            assert load_models_config(workspace) == {}

        assert _warnings(caplog) == []

    def test_callers_get_their_own_copy(self, workspace: Path) -> None:
        _save(config_layout.models_path(workspace), CONFIG)
        load_models_config(workspace)["带参数的"]["options"]["thinking"] = False

        assert load_models_config(workspace)["带参数的"]["options"] == {"thinking": True}
        assert resolve_channel(workspace, "带参数的").options == {"thinking": True}

    def test_a_same_tick_replacement_is_noticed(self, workspace: Path) -> None:
        # 共享组里别的实例在同一个时钟刻度里写进等长内容：修改时间和大小都一样，只剩 inode 认得出来。
        path = config_layout.models_path(workspace)
        _save(path, CONFIG)
        assert resolve_channel(workspace, "deepseek").model == "deepseek-v4"
        before = path.stat()

        _save(path, CONFIG.replace("deepseek-v4", "deepseek-v9"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert (path.stat().st_mtime_ns, path.stat().st_size) == (before.st_mtime_ns, before.st_size)
        assert resolve_channel(workspace, "deepseek").model == "deepseek-v9"


class TestABrokenRewriteMidRun:
    """文件运行中写坏时，点名渠道、系统槽位、备选链都还照上一份能用的走，不会单独没了。"""

    def _break_after_a_good_read(self, workspace: Path) -> None:
        path = config_layout.models_path(workspace)
        _save(path, FALLBACK_CONFIG)
        load_models_config(workspace)
        _save(path, BROKEN)

    def test_a_node_naming_a_channel_still_resolves(self, workspace: Path) -> None:
        self._break_after_a_good_read(workspace)

        rp = resolve_provider(workspace, Node(id="n", name="n", type="ai", provider="gemini-中转A"), "gpt-5.4")

        assert (rp.provider_type, rp.base_url, rp.api_key) == ("gemini", "https://relay-a.example/v1", "sk-relay-a")

    def test_a_system_slot_still_resolves(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for suffix in ("MODEL", "BASE_URL", "API_KEY", "PROVIDER"):
            monkeypatch.delenv(f"CLONOTH_INTENT_{suffix}", raising=False)
        monkeypatch.delenv("CLONOTH_WORKSPACE", raising=False)
        monkeypatch.delenv("ONEBOT_WORKSPACE_ROOT", raising=False)
        self._break_after_a_good_read(workspace)

        slot = resolve_system_model(workspace, "intent")

        assert (slot.provider, slot.model) == ("gemini-官方", "gemini-3-flash-lite")

    def test_the_fallback_chain_still_runs(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._break_after_a_good_read(workspace)
        created: list[dict] = []

        class _Backup:
            name = "gemini"
            model = "gemini-3-pro"

            async def chat(self, *, messages, tools):
                return ProviderResponse(ok=True, text="done")

        def create(**kwargs):
            created.append(kwargs)
            return _Backup()

        monkeypatch.setattr(fallback_module, "_create_fallback_provider", create)
        monkeypatch.setattr(llm_call, "_build_messages_for_provider", lambda messages, formatter, provider: messages)
        monkeypatch.setattr(llm_call, "prepare_messages_for_llm", lambda messages, workspace_root: messages)
        ctx = SimpleNamespace(
            response=ProviderResponse(ok=False, error="primary down", status_code=502, error_kind="server"),
            rctx=SimpleNamespace(workspace_root=workspace, total_usage={}, serving_provider=None),
            node=SimpleNamespace(id="entry"), messages=[{"role": "user", "content": "hi"}], tools=[],
            provider=SimpleNamespace(model="primary-model", name="deepseek"),
            extra={"loop_state": SimpleNamespace(formatter=None)},
        )

        asyncio.run(fallback_module.FallbackProviderHandler().handle(ctx))

        assert ctx.response.ok
        assert [(c["provider_type"], c["base_url"], c["api_key"]) for c in created] == [
            ("gemini", "https://relay-a.example/v1", "sk-relay-a"),
        ]
