"""控制台 schema：键自带控件说明，分区只引用键名；布局写错在建 schema 时就报出来，不等前端画歪。"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import _console_fixture  # noqa: E402
from adapters._shared import capability, features, trigger_policy  # noqa: E402
from adapters._shared import console_schema as cs  # noqa: E402
from adapters._shared.features_layout import (  # noqa: E402
    FEATURE_SECTIONS,
    STICKER_LIBRARY_PATHS,
    STICKER_SENDING_PATHS,
)
from adapters._shared.live_config import (  # noqa: E402
    BOOL,
    GLOBAL_AND_PLATFORM,
    LAYER_GLOBAL,
    LAYER_PLATFORM,
    PLAIN_STR,
    TRISTATE_BOOL,
    WORD_LIST,
    LiveKey,
)
from adapters.onebot import console_layout  # noqa: E402
from adapters.onebot.live_config import LIVE_KEYS  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import HOOK_CONSOLE_SCHEMA  # noqa: E402
from platforms.registry import all_platforms, platform_hook  # noqa: E402

_SWITCH = LiveKey("switch", "demo.switch", BOOL, True, ui=cs.toggle("开关", "说明"))
_WORDS = LiveKey(
    "words", "demo.words", WORD_LIST, "", layers=GLOBAL_AND_PLATFORM,
    ui=cs.word_list("词", empty="没有词", placeholder="输入一个词"),
)
_MODE = LiveKey("mode", "demo.mode", PLAIN_STR, "a")
_SIGNAL = LiveKey(
    "signal", "demo.signal", TRISTATE_BOOL, None, ui=cs.tristate("信号", "说明", follows="mode", when=("b", "a")),
)
_PICK = LiveKey("pick", "demo.pick", PLAIN_STR, "x", ui=cs.choice("挑一个", (("x", "甲"), ("y", "乙"))))
_KEYS = (_SWITCH, _WORDS, _MODE, _SIGNAL, _PICK)


def _demo(*nodes: cs.Node, paths: tuple[str, ...] = ("demo",), section_id: str = "demo") -> cs.Section:
    return cs.section(section_id, "演示", *nodes, icon="tune", note="演示分区", paths=paths)


def _problems(*sections: cs.Section, keys: tuple[LiveKey, ...] = _KEYS) -> list[str]:
    return cs.schema_problems(sections, keys)


def _placed(sections: tuple[cs.Section, ...]) -> list[str]:
    return cs.field_keys(sections)


def _custom_names(sections: tuple[cs.Section, ...]) -> set[str]:
    return {node.name for part in sections for node in cs.walk(part.nodes) if isinstance(node, cs.Custom)}


_ICON_SOURCE = _ROOT / "adapters" / "web" / "frontend" / "src" / "components" / "common" / "Icon.tsx"


def _schema_icons(node: Any) -> set[str]:
    """schema 里每一层的 icon 名：分区、块，以后新加的节点也按同一条规则收。"""
    if isinstance(node, dict):
        found = {value for key, value in node.items() if key == "icon" and isinstance(value, str) and value}
        return found.union(*(_schema_icons(value) for key, value in node.items() if key != "icon"), set())
    if isinstance(node, list):
        return set().union(*(_schema_icons(item) for item in node), set())
    return set()


def _console_icons() -> set[str]:
    """控制台会画的全部图标名：全局页一份，每个有控制台界面的平台各一份。"""
    schemas = [features.schema()] + [
        platform_hook(platform, HOOK_CONSOLE_SCHEMA).schema()
        for platform in all_platforms()
        if HOOK_CONSOLE_SCHEMA in platform.hooks
    ]
    return set().union(*(_schema_icons(document) for document in schemas))


def _icon_map_keys() -> set[str]:
    source = _ICON_SOURCE.read_text(encoding="utf-8")
    body = re.search(r"const ICON_MAP[^{]*\{(.*?)\n\};", source, re.S)
    assert body is not None, "Icon.tsx 里找不到 ICON_MAP"
    return set(re.findall(r"^\s*([A-Za-z0-9_]+)\s*:", body.group(1), re.M))


class TestControls:
    def test_a_toggle_carries_only_what_was_filled_in(self) -> None:
        assert cs.toggle("开关").payload() == {"kind": "bool", "label": "开关"}
        assert cs.toggle("开关", "说明").payload() == {"kind": "bool", "label": "开关", "desc": "说明"}

    def test_a_number_names_its_ceiling_max(self) -> None:
        control = cs.number("单图上限", unit="MB", step=1, max_value=64, scale=1024 * 1024)

        assert control.payload() == {
            "kind": "number", "label": "单图上限", "unit": "MB", "step": 1, "max": 64, "scale": 1024 * 1024,
        }

    def test_zero_is_a_value_not_a_blank(self) -> None:
        assert cs.number("概率", step=0.05, max_value=0).payload()["max"] == 0

    def test_choices_go_out_as_value_label_pairs(self) -> None:
        payload = cs.choice("收多严", (("strict", "严格"), ("none", "全收"))).payload()

        assert payload["choices"] == [["strict", "严格"], ["none", "全收"]]
        assert json.loads(json.dumps(payload)) == payload

    def test_list_controls_carry_their_empty_text_and_placeholder(self) -> None:
        assert cs.id_list("群", empty="名单为空", placeholder="输入群号").payload() == {
            "kind": "ids", "label": "群", "empty": "名单为空", "placeholder": "输入群号",
        }
        assert cs.word_list("词", empty="没有", placeholder="输入").payload()["kind"] == "words"

    def test_a_following_tristate_names_the_key_and_sorted_values(self) -> None:
        payload = cs.tristate("全量回应", "说明", follows="group_trigger", when=("always", "all")).payload()

        assert payload["follows"] == "group_trigger"
        assert payload["follows_when"] == ["all", "always"]
        assert "unset" not in payload

    @pytest.mark.parametrize("unset", [True, False])
    def test_a_constant_tristate_states_its_unset_value(self, unset: bool) -> None:
        payload = cs.tristate("被 @", "说明", unset=unset).payload()

        assert payload["unset"] is unset
        assert "follows" not in payload and "follows_when" not in payload

    def test_only_tristates_talk_about_unset(self) -> None:
        assert "unset" not in cs.toggle("开关").payload()
        assert "unset" not in cs.Control(cs.CONTROL_BOOL, "开关", unset=True).payload()


class TestNodes:
    def test_a_block_drops_blank_hint_and_frame(self) -> None:
        assert cs.block("标题", cs.note("注")).payload() == {
            "kind": "block", "title": "标题", "children": [{"kind": "note", "text": "注"}],
        }
        framed = cs.block("标题", hint="提示", frame=cs.FRAME_BOXED).payload()
        assert framed["hint"] == "提示" and framed["frame"] == cs.FRAME_BOXED

    def test_containers_keep_their_kind(self) -> None:
        assert [cs.grid().payload()["kind"], cs.panel().payload()["kind"], cs.loose().payload()["kind"]] == [
            "grid", "panel", "loose",
        ]

    def test_a_field_marks_sub_options_and_spacing_only_when_set(self) -> None:
        assert cs.field("switch").payload() == {"kind": "field", "key": "switch"}
        assert cs.field("switch", sub=True).payload() == {"kind": "field", "key": "switch", "sub": True}
        assert cs.field("words", spacing=cs.SPACING_PANEL).payload()["spacing"] == "panel"

    def test_a_field_nests_what_folds_under_it(self) -> None:
        payload = cs.field("switch", cs.field("words"), cs.paragraph("说明", indent=False)).payload()

        assert payload["children"] == [
            {"kind": "field", "key": "words"},
            {"kind": "desc", "text": "说明", "indent": False},
        ]

    def test_fixed_and_custom(self) -> None:
        assert cs.fixed("长度上限", cs.field("words"), desc="说明").payload() == {
            "kind": "fixed", "label": "长度上限", "desc": "说明", "children": [{"kind": "field", "key": "words"}],
        }
        assert cs.custom("audition").payload() == {"kind": "custom", "name": "audition"}

    def test_a_section_lists_its_paths_even_when_empty(self) -> None:
        payload = cs.section("account", "账号", cs.custom("account"), icon="person", note="说明").payload()

        assert payload == {
            "id": "account", "label": "账号", "icon": "person", "note": "说明", "paths": [],
            "nodes": [{"kind": "custom", "name": "account"}],
        }

    def test_walk_visits_parents_before_children(self) -> None:
        tree = (cs.block("甲", cs.field("switch", cs.field("words"))), cs.note("乙"))

        kinds = [type(node).__name__ for node in cs.walk(tree)]

        assert kinds == ["Block", "FieldNode", "FieldNode", "Text"]

    def test_field_keys_keep_order_and_repeats(self) -> None:
        sections = (_demo(cs.field("words"), cs.field("switch")), _demo(cs.field("words"), section_id="again"))

        assert cs.field_keys(sections) == ["words", "switch", "words"]

    @pytest.mark.parametrize(
        ("path", "prefixes", "expected"),
        [
            ("demo.words", ("demo",), True),
            ("demo", ("demo",), True),
            ("demolition.words", ("demo",), False),
            ("demo.words", ("other", "demo.words"), True),
            ("demo.words", (), False),
        ],
    )
    def test_covers_follows_the_console_pending_rule(self, path: str, prefixes: tuple[str, ...], expected: bool) -> None:
        assert cs.covers(prefixes, path) is expected


class TestValidation:
    def test_a_sound_layout_has_no_problems(self) -> None:
        layout = _demo(
            cs.block("甲", cs.grid(cs.field("switch", cs.field("signal"), cs.paragraph("说明"))), hint="提示"),
            cs.loose(cs.field("words", spacing=cs.SPACING_LOOSE), cs.note("注")),
            cs.fixed("一组", cs.field("pick")),
        )

        assert _problems(layout) == []

    def test_a_key_placed_twice(self) -> None:
        """两处的草稿会互相覆盖，「N 项待应用」也会算两遍。"""
        assert _problems(_demo(cs.field("words"), cs.loose(cs.field("words")))) == [
            "key 'words' appears more than once",
        ]

    def test_an_unknown_key(self) -> None:
        assert _problems(_demo(cs.field("nope"))) == ["section 'demo' places unknown key 'nope'"]

    def test_a_key_without_a_control(self) -> None:
        assert _problems(_demo(cs.field("mode"))) == ["key 'mode' has no console control"]

    def test_only_switches_hold_children(self) -> None:
        assert _problems(_demo(cs.field("words", cs.field("switch")))) == [
            "key 'words' is not a switch, so it cannot be a sub-option or hold children",
        ]

    def test_only_switches_shrink_into_sub_options(self) -> None:
        assert _problems(_demo(cs.field("pick", sub=True))) == [
            "key 'pick' is not a switch, so it cannot be a sub-option or hold children",
        ]

    def test_a_choice_needs_choices(self) -> None:
        empty = LiveKey("empty", "demo.empty", PLAIN_STR, "", ui=cs.choice("空", ()))

        assert _problems(_demo(cs.field("empty")), keys=(*_KEYS, empty)) == ["key 'empty' is a choice without choices"]

    def test_a_tristate_must_follow_a_known_key(self) -> None:
        stray = LiveKey("stray", "demo.stray", TRISTATE_BOOL, None, ui=cs.tristate("信号", "说明", follows="ghost"))

        assert _problems(_demo(cs.field("stray")), keys=(*_KEYS, stray)) == ["key 'stray' follows unknown key 'ghost'"]

    def test_unknown_control_and_spacing(self) -> None:
        odd = LiveKey("odd", "demo.odd", BOOL, False, ui=cs.Control("slider", "滑块"))

        assert _problems(_demo(cs.field("odd", spacing="tight")), keys=(*_KEYS, odd)) == [
            "key 'odd' has an unknown control 'slider'",
            "key 'odd' has an unknown spacing 'tight'",
        ]

    def test_unknown_containers_texts_and_frames(self) -> None:
        layout = _demo(
            cs.Group("carousel", ()),
            cs.Text("banner", "横幅"),
            cs.block("甲", frame="glow"),
            cs.custom(""),
        )

        assert _problems(layout) == [
            "section 'demo' has an unknown container 'carousel'",
            "section 'demo' has an unknown text kind 'banner'",
            "section 'demo' has an unknown frame 'glow'",
            "section 'demo' has a custom block without a name",
        ]

    def test_duplicate_sections(self) -> None:
        assert _problems(_demo(), _demo()) == ["duplicate section 'demo'"]

    def test_a_key_outside_its_section_paths(self) -> None:
        assert _problems(_demo(cs.field("switch"), paths=("demo.words",))) == [
            "section 'demo' places key 'switch' outside its paths",
        ]

    def test_a_section_without_paths_cannot_place_keys(self) -> None:
        assert _problems(_demo(cs.field("switch"), paths=())) == [
            "section 'demo' places key 'switch' outside its paths",
        ]

    def test_two_sections_claiming_overlapping_paths(self) -> None:
        problems = _problems(
            _demo(cs.field("switch"), paths=("demo",)),
            _demo(cs.field("words"), paths=("demo.words",), section_id="words"),
        )

        assert problems == ["sections 'demo' and 'words' both claim 'demo.words'"]

    def test_one_section_may_list_nested_paths(self) -> None:
        assert _problems(_demo(cs.field("switch"), paths=("demo", "demo.switch"))) == []

    def test_build_refuses_a_broken_layout(self) -> None:
        with pytest.raises(ValueError, match="invalid console schema: section 'demo' places unknown key 'nope'"):
            cs.build_schema((_demo(cs.field("nope")),), _KEYS)

    def test_build_describes_only_the_placed_keys(self) -> None:
        schema = cs.build_schema((_demo(cs.field("switch", cs.field("words")), cs.field("signal")),), _KEYS)

        assert schema["version"] == cs.SCHEMA_VERSION
        assert list(schema["fields"]) == ["switch", "words", "signal"]
        assert schema["fields"]["words"] == {
            "kind": "words", "label": "词", "empty": "没有词", "placeholder": "输入一个词",
            "path": "demo.words", "layers": [LAYER_GLOBAL, LAYER_PLATFORM], "default": "",
        }
        assert schema["fields"]["signal"]["default"] is None
        assert schema["fields"]["switch"]["layers"] == [LAYER_PLATFORM]

    def test_the_schema_is_plain_json(self) -> None:
        schema = cs.build_schema((_demo(cs.field("pick")),), _KEYS)

        assert json.loads(json.dumps(schema, ensure_ascii=False)) == schema


class TestGlobalPage:
    def test_it_builds(self) -> None:
        schema = features.schema()

        assert [part["id"] for part in schema["sections"]] == [part.id for part in FEATURE_SECTIONS]

    def test_every_key_but_the_legacy_mode_is_placed_exactly_once(self) -> None:
        placed = Counter(_placed(FEATURE_SECTIONS))

        assert {name for name, count in placed.items() if count > 1} == set()
        assert {key.name for key in features.FEATURE_KEYS} - set(placed) == {"group_trigger"}

    def test_the_legacy_mode_has_no_control(self) -> None:
        """group_mode 被七个信号取代，控制台只拿它推导三态的当前值。"""
        assert features.FEATURE_KEYS_BY_NAME["group_trigger"].ui is None

    def test_every_other_key_has_a_control(self) -> None:
        assert [key.name for key in features.FEATURE_KEYS if key.ui is None] == ["group_trigger"]

    def test_the_global_page_has_no_platform_pieces(self) -> None:
        assert _custom_names(FEATURE_SECTIONS) == set()

    def test_every_key_falls_under_some_section(self) -> None:
        claimed = [path for part in FEATURE_SECTIONS for path in part.paths]

        assert [key.name for key in features.FEATURE_KEYS if not cs.covers(claimed, key.path)] == []

    def test_the_library_section_holds_exactly_the_global_only_keys(self) -> None:
        library = next(part for part in FEATURE_SECTIONS if part.id == "sticker_library")
        global_only = {key.name for key in features.FEATURE_KEYS if LAYER_PLATFORM not in key.layers}

        assert set(_placed((library,))) == global_only
        assert set(STICKER_LIBRARY_PATHS) == features.GLOBAL_ONLY_PATHS

    def test_the_sending_paths_match_the_sending_keys(self) -> None:
        picking = next(part for part in FEATURE_SECTIONS if part.id == "sticker")
        paths = {features.FEATURE_KEYS_BY_NAME[name].path for name in _placed((picking,))}

        assert paths == set(STICKER_SENDING_PATHS)

    @pytest.mark.parametrize(
        ("name", "legacy"),
        [("signal_all", trigger_policy.LEGACY_ALL_MODES), ("signal_prefix", trigger_policy.LEGACY_PREFIX_MODES)],
    )
    def test_derived_tristates_follow_the_trigger_policy(self, name: str, legacy: frozenset[str]) -> None:
        """三态没配时的推导只在 trigger_policy 写一份，控制台标的「当前」必须和它一致。"""
        payload = features.schema()["fields"][name]

        assert payload["follows"] == "group_trigger"
        assert set(payload["follows_when"]) == set(legacy)

    @pytest.mark.parametrize("name", ["signal_at", "signal_reply"])
    def test_constant_tristates_default_to_on(self, name: str) -> None:
        assert features.schema()["fields"][name]["unset"] is True


class TestQQPage:
    def test_the_registry_hook_serves_the_layout(self) -> None:
        assert platform_hook(QQ_NAPCAT, HOOK_CONSOLE_SCHEMA) is console_layout

    def test_it_builds(self) -> None:
        schema = console_layout.schema()

        assert [part["id"] for part in schema["sections"]] == [
            "account", "channels", "trigger", "permissions", "input", "output", "interaction", "sticker", "link",
        ]

    def test_only_grants_and_the_legacy_mode_go_without_controls(self) -> None:
        """档位那一块由能力清单组件画，它们的控件不在键上。"""
        grants = {f"grant_{cap.key}" for cap in capability.CAPABILITIES}

        assert {key.name for key in LIVE_KEYS if key.ui is None} == grants | {"group_trigger"}

    def test_every_platform_key_with_a_control_is_placed_exactly_once(self) -> None:
        placed = Counter(_placed(console_layout.SECTIONS))
        expected = {key.name for key in LIVE_KEYS if key.ui is not None and LAYER_PLATFORM in key.layers}

        assert {name for name, count in placed.items() if count > 1} == set()
        assert set(placed) == expected

    def test_global_only_keys_stay_off_the_platform_page(self) -> None:
        global_only = {key.name for key in LIVE_KEYS if LAYER_PLATFORM not in key.layers}
        claimed = [path for part in console_layout.SECTIONS for path in part.paths]

        assert global_only
        assert global_only & set(_placed(console_layout.SECTIONS)) == set()
        # 分区认领了它们的路径的话，「N 项待应用」会替一个根本改不了的键计数。
        assert [name for name in global_only if cs.covers(claimed, features.FEATURE_KEYS_BY_NAME[name].path)] == []

    def test_every_platform_key_falls_under_some_section(self) -> None:
        claimed = [path for part in console_layout.SECTIONS for path in part.paths]
        uncovered = [key.name for key in LIVE_KEYS if LAYER_PLATFORM in key.layers and not cs.covers(claimed, key.path)]

        assert uncovered == []

    def test_shared_keys_look_the_same_on_both_pages(self) -> None:
        qq = console_layout.schema()["fields"]
        shared = features.schema()["fields"]

        assert {name: qq[name] for name in qq.keys() & shared.keys()} == {
            name: shared[name] for name in qq.keys() & shared.keys()
        }
        assert qq.keys() & shared.keys() == {
            key.name for key in features.FEATURE_KEYS if key.ui is not None and LAYER_PLATFORM in key.layers
        }

    def test_the_platform_pieces(self) -> None:
        """这些块由前端按「qq:分区」登记的组件画，改名要两边一起改。"""
        assert _custom_names(console_layout.SECTIONS) == {
            "workspace", "groupWarning", "audition", "adminWarning", "capabilities",
            "queueFacts", "historyFacts", "bridgeFacts", "environment", "restartEngine",
        }


class TestFrontendSnapshot:
    def test_the_frontend_snapshot_matches_the_declarations(self) -> None:
        """前端测试按这份快照画页面，对不上的话前端测的是一个已经不存在的界面。"""
        stored = json.loads(_console_fixture.FIXTURE.read_text(encoding="utf-8"))

        if stored != _console_fixture.current():
            pytest.fail("控制台 schema 变了：跑 python tests/_console_fixture.py 重新生成前端快照")

    def test_the_snapshot_covers_every_platform_with_a_console(self) -> None:
        stored = json.loads(_console_fixture.FIXTURE.read_text(encoding="utf-8"))

        assert set(stored["platforms"]) == {QQ_NAPCAT.id}


class TestFrontendIcons:
    """图标名由 schema 给，前端那张映射表是手工维护的：漏一个，分区头上就是两个字母的回退件。"""

    def test_every_icon_the_console_asks_for_is_drawable(self) -> None:
        missing = sorted(_console_icons() - _icon_map_keys())

        assert missing == [], f"Icon.tsx 的 ICON_MAP 缺这些图标：{missing}"

    def test_neither_side_reads_as_empty(self) -> None:
        # 正则或 schema 取法哪天失手，上面那条会因为两边都空而永远通过。
        assert _icon_map_keys()
        assert _console_icons()
