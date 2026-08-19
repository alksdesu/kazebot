"""带图消息走主模型还是绕去视觉节点。

绕过去会把工具和委派全砍掉，所以只在主渠道自己收不下图时才值得绕。判据是渠道块里
写的 supports_vision，没写就按这家 provider 的默认算。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402

from providers import registry  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

def _store(tmp_path: Path, active: str = "openai", vision: object = None) -> ConfigStore:
    data: dict = {
        "version": 1,
        "provider": active,
        "openai": {
            "base_url": "https://api.openai.com/v1", "api_key": "sk-openai", "model": "gpt-4o-mini",
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com", "api_key": "sk-deepseek", "model": "deepseek-chat",
        },
    }
    if vision is not None:
        data[active]["supports_vision"] = vision
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return ConfigStore(path=path)


def _state(tmp_path: Path, store: ConfigStore | None, vision_enabled: str) -> SupervisorState:
    runtime = tmp_path / "config" / "runtime.yaml"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("\n".join([
        "shell:",
        "  entry_node_id: qq.orchestrator",
        "routing:",
        "  vision:",
        "    enabled: " + vision_enabled,
        "    entry_node_id: qq.vision",
    ]), encoding="utf-8")
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-vision"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    state.config_store = store
    return state


def _node(tmp_path: Path, node_id: str = "qq.vision", **fields: object) -> None:
    """写一个视觉入口节点定义。不给字段就是「三个变量全空」的开箱状态。"""
    nodes = tmp_path / "config" / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    data: dict = {"id": node_id, "type": "ai", "provider": "openai"}
    data.update({k: v for k, v in fields.items() if v is not None})
    (nodes / (node_id + ".yaml")).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )


def _route(state: SupervisorState, *, has_image: bool = True) -> str:
    from clonoth_runtime import load_runtime_config
    payload = {"channel": "qq_group"}
    if has_image:
        payload["attachments"] = [{"type": "image", "path": "a.png"}]
    return state._route_entry_node_for_capabilities(
        cfg=load_runtime_config(state.workspace_root),
        payload=payload,
        selected_entry_node="qq.orchestrator",
        default_node="qq.orchestrator",
        session_override="",
    )


class TestProviderDefaults:
    def test_deepseek_is_the_text_only_one(self) -> None:
        assert registry.default_vision_support()["deepseek"] is False

    def test_the_rest_are_assumed_to_read_images(self) -> None:
        defaults = registry.default_vision_support()
        assert defaults["openai"] is True
        assert defaults["gemini"] is True
        assert defaults["anthropic"] is True


class TestChannelVisionFlag:
    def test_it_follows_the_provider_default_when_unset(self, tmp_path) -> None:
        assert _store(tmp_path, "openai").supports_vision() is True
        assert _store(tmp_path, "deepseek").supports_vision() is False

    def test_an_explicit_flag_wins(self, tmp_path) -> None:
        # 中转站在 openai 块下挂个纯文本模型是常事，得能手动改掉。
        store = _store(tmp_path, "openai", vision=False)
        assert store.supports_vision() is False

    def test_it_can_be_turned_on_for_a_text_only_family(self, tmp_path) -> None:
        store = _store(tmp_path, "deepseek", vision=True)
        assert store.supports_vision() is True

    def test_switching_the_active_channel_switches_the_answer(self, tmp_path) -> None:
        store = _store(tmp_path, "openai")
        assert store.supports_vision() is True
        store.set_active_provider("deepseek")
        assert store.supports_vision() is False

    def test_a_named_channel_can_be_asked_directly(self, tmp_path) -> None:
        store = _store(tmp_path, "openai")
        assert store.supports_vision("deepseek") is False

    def test_an_unknown_channel_is_assumed_capable(self, tmp_path) -> None:
        assert _store(tmp_path, "openai").supports_vision("nosuch") is True


class TestPublicShape:
    def test_unset_reads_back_as_none_not_false(self, tmp_path) -> None:
        # 「没配」和「配了个 false」要分得开，否则界面存一次就把跟随默认写死成 false。
        blocks = _store(tmp_path, "openai").get_providers_public()["providers"]
        assert blocks["openai"]["supports_vision"] is None

    def test_an_explicit_flag_reads_back(self, tmp_path) -> None:
        store = _store(tmp_path, "openai", vision=False)
        assert store.get_providers_public()["providers"]["openai"]["supports_vision"] is False

    def test_auto_removes_the_key_again(self, tmp_path) -> None:
        store = _store(tmp_path, "openai", vision=False)
        store.upsert_provider("openai", supports_vision="auto")
        assert store.get_providers_public()["providers"]["openai"]["supports_vision"] is None
        assert store.supports_vision() is True

    def test_writing_yes_and_no(self, tmp_path) -> None:
        store = _store(tmp_path, "openai")
        store.upsert_provider("openai", supports_vision="no")
        assert store.supports_vision() is False
        store.upsert_provider("openai", supports_vision="yes")
        assert store.supports_vision() is True

    def test_not_passing_it_leaves_the_flag_alone(self, tmp_path) -> None:
        store = _store(tmp_path, "openai", vision=False)
        store.upsert_provider("openai", model="gpt-4o")
        assert store.supports_vision() is False


class TestRouting:
    def test_auto_keeps_a_seeing_model_on_the_main_node(self, tmp_path) -> None:
        # 绕去视觉节点等于丢掉全部工具和委派，主模型自己看得见就没理由绕。
        state = _state(tmp_path, _store(tmp_path, "openai"), "auto")
        assert _route(state) == "qq.orchestrator"

    def test_auto_diverts_a_text_only_model(self, tmp_path) -> None:
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.vision"

    def test_auto_follows_an_explicit_flag_over_the_family_default(self, tmp_path) -> None:
        state = _state(tmp_path, _store(tmp_path, "openai", vision=False), "auto")
        assert _route(state) == "qq.vision"

    def test_true_still_always_diverts(self, tmp_path) -> None:
        # 想把带图消息挡在工具外面的人靠这一档，不能被 auto 悄悄改掉语义。
        state = _state(tmp_path, _store(tmp_path, "openai"), "true")
        assert _route(state) == "qq.vision"

    def test_false_never_diverts(self, tmp_path) -> None:
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "false")
        assert _route(state) == "qq.orchestrator"

    def test_a_message_without_images_is_never_touched(self, tmp_path) -> None:
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state, has_image=False) == "qq.orchestrator"

    def test_without_a_config_store_it_behaves_as_before(self, tmp_path) -> None:
        # 读不到渠道配置时按老行为绕，不能让带图消息撞上一个看不见图的模型。
        state = _state(tmp_path, None, "auto")
        assert _route(state) == "qq.vision"

    def test_a_broken_config_store_does_not_take_routing_down(self, tmp_path) -> None:
        class Exploding:
            def supports_vision(self, name: str = "") -> bool:
                raise RuntimeError("config unreadable")

        state = _state(tmp_path, Exploding(), "auto")
        assert _route(state) == "qq.vision"


class TestLegacyValues:
    @pytest.mark.parametrize("value,diverts", [
        ("true", True), ("false", False), ("yes", True), ("no", False),
        ("on", True), ("off", False), ("auto", None),
    ])
    def test_the_switch_accepts_what_people_actually_write(
        self, tmp_path, value: str, diverts: bool | None,
    ) -> None:
        state = _state(tmp_path, _store(tmp_path, "openai"), value)
        # auto 下 openai 看得见图，所以不绕。
        expected = "qq.vision" if (diverts is True) else "qq.orchestrator"
        assert _route(state) == expected

    def test_a_yaml_bool_still_works(self, tmp_path) -> None:
        state = _state(tmp_path, _store(tmp_path, "openai"), "true")
        assert state._vision_routing_applies(True) is True
        assert state._vision_routing_applies(False) is False


class TestTheVisionNodeMustBeWorthTheDetour:
    """绕去视觉节点只有在它真有自己的渠道时才有意义。

    engine/model.py 里空 model/api_key/base_url 一律回落全局，所以一个三项全空的
    qq.vision 解析出来就是主渠道那个模型 —— 绕过去换来的是同一个看不了图的模型，
    外加工具和委派被砍光。这比不绕更糟，而且失败得静悄悄。
    """

    def test_an_unconfigured_node_is_not_worth_it(self, tmp_path) -> None:
        _node(tmp_path)
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.orchestrator"

    def test_an_env_placeholder_that_resolves_to_nothing_counts_as_unconfigured(
        self, tmp_path, monkeypatch,
    ) -> None:
        # .env 里留着空值的 QQ_VISION_MODEL= 是开箱默认，别把它当成「配过了」。
        monkeypatch.delenv("QQ_VISION_MODEL", raising=False)
        monkeypatch.delenv("QQ_VISION_BASE_URL", raising=False)
        _node(tmp_path, model="$ENV{QQ_VISION_MODEL}", base_url="$ENV{QQ_VISION_BASE_URL}")
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.orchestrator"

    def test_a_resolved_env_placeholder_makes_the_detour_real(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("QQ_VISION_MODEL", "gpt-4o-mini")
        _node(tmp_path, model="$ENV{QQ_VISION_MODEL}")
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.vision"

    def test_a_literal_model_makes_the_detour_real(self, tmp_path) -> None:
        _node(tmp_path, model="gpt-4o-mini")
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.vision"

    def test_its_own_base_url_is_enough(self, tmp_path, monkeypatch) -> None:
        # 同名模型换个能读图的中转站也是有效配置。
        monkeypatch.setenv("QQ_VISION_BASE_URL", "https://relay.example/v1")
        _node(tmp_path, base_url="$ENV{QQ_VISION_BASE_URL}")
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.vision"

    def test_an_api_key_alone_is_not_enough(self, tmp_path, monkeypatch) -> None:
        # 只有 key 意味着模型名和地址都还是主渠道的，那就是原地打转。
        monkeypatch.setenv("QQ_VISION_API_KEY", "sk-vision")
        _node(tmp_path, api_key="$ENV{QQ_VISION_API_KEY}")
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.orchestrator"

    def test_a_missing_node_definition_keeps_the_old_behaviour(self, tmp_path) -> None:
        # 读不到就按配了算：这里不该因为读文件失败而悄悄改变路由。
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.vision"

    def test_the_node_is_found_by_id_not_by_filename(self, tmp_path) -> None:
        nodes = tmp_path / "config" / "nodes"
        nodes.mkdir(parents=True, exist_ok=True)
        (nodes / "whatever.yaml").write_text(
            yaml.safe_dump({"id": "qq.vision", "type": "ai"}, sort_keys=False), encoding="utf-8",
        )
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        assert _route(state) == "qq.orchestrator"

    @pytest.mark.parametrize("written", ["true", '"true"', "yes", "on"])
    def test_explicit_true_still_diverts_to_an_unconfigured_node(self, tmp_path, written: str) -> None:
        # 写死 true 的人要的是隔离本身：带图消息不许碰工具。节点配没配不改变这个诉求。
        # 不加引号会被 yaml 解析成 bool，加了是字符串 —— 两条分支都得照绕。
        _node(tmp_path)
        state = _state(tmp_path, _store(tmp_path, "openai"), written)
        assert _route(state) == "qq.vision"

    def test_a_seeing_main_channel_never_reaches_the_node_check(self, tmp_path) -> None:
        _node(tmp_path, model="gpt-4o-mini")
        state = _state(tmp_path, _store(tmp_path, "openai"), "auto")
        assert _route(state) == "qq.orchestrator"

    def test_the_warning_names_the_env_vars_and_fires_once(self, tmp_path, caplog) -> None:
        # 这条日志是运营者唯一能发现「绕了个寂寞」的地方，但不能每张图刷一行。
        import logging

        _node(tmp_path)
        state = _state(tmp_path, _store(tmp_path, "deepseek"), "auto")
        with caplog.at_level(logging.WARNING, logger="supervisor.task_store"):
            _route(state)
            _route(state)

        hits = [r for r in caplog.records if "QQ_VISION_MODEL" in r.getMessage()]
        assert len(hits) == 1
        assert "qq.vision" in hits[0].getMessage()
