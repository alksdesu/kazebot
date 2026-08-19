"""provider 声明的可调参数必须自洽，且真能落到请求体上。

控制台整页是照 OPTIONS 渲染的，界面上没有一处硬编码。所以一个写错的 spec
不会报错，只会让那个控件静静地不出现、或者永远存不进去。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from providers import registry  # noqa: E402
from providers.options import BOOL, ENUM, FLOAT, INT, TEXT, OptionSet, OptionSpec  # noqa: E402

_KINDS = frozenset({BOOL, INT, FLOAT, ENUM, TEXT})
_NAMES = sorted(registry.list())


def _specs(name: str) -> tuple[OptionSpec, ...]:
    return tuple(getattr(registry.get(name), "OPTIONS", ()))


def _seed_variants(specs: tuple[OptionSpec, ...]) -> list[dict]:
    """几种起始状态：全默认、每个枚举各取一个值、数值取上下限。"""
    variants: list[dict] = [{spec.key: spec.default for spec in specs}]
    widest = max((len(spec.choices) for spec in specs if spec.kind == ENUM), default=1)
    for index in range(max(widest, 2)):
        seed: dict = {}
        for spec in specs:
            if spec.kind == ENUM:
                seed[spec.key] = spec.values()[index % len(spec.values())]
            elif spec.kind == BOOL:
                seed[spec.key] = index % 2 == 0
            elif spec.kind in (INT, FLOAT):
                bound = spec.maximum if index % 2 == 0 else spec.minimum
                seed[spec.key] = bound if bound is not None else 1
            else:
                seed[spec.key] = f"seed{index}"
        variants.append(seed)
    return variants


@pytest.mark.parametrize("name", _NAMES)
def test_every_provider_declares_options(name: str) -> None:
    assert _specs(name), f"{name} 一个可调参数都没声明，控制台上会是空白一页"


@pytest.mark.parametrize("name", _NAMES)
def test_option_keys_are_unique(name: str) -> None:
    keys = [spec.key for spec in _specs(name)]
    dupes = sorted({key for key in keys if keys.count(key) > 1})
    assert not dupes, f"{name} 有重名参数，后一个会盖掉前一个：{dupes}"


@pytest.mark.parametrize("name", _NAMES)
def test_kinds_are_known(name: str) -> None:
    bad = [spec.key for spec in _specs(name) if spec.kind not in _KINDS]
    assert not bad, f"{name} 的这些参数是控制台不认识的类型，控件不会渲染：{bad}"


@pytest.mark.parametrize("name", _NAMES)
def test_enums_have_choices_and_a_reachable_default(name: str) -> None:
    for spec in _specs(name):
        if spec.kind != ENUM:
            continue
        assert spec.choices, f"{name}.{spec.key} 是枚举却没有选项，界面上是一排空按钮"
        assert spec.default in spec.values(), (
            f"{name}.{spec.key} 的默认值 {spec.default!r} 不在选项里，"
            f"界面会显示成没有任何一项被选中：{spec.values()}"
        )


@pytest.mark.parametrize("name", _NAMES)
def test_numeric_defaults_sit_inside_their_own_range(name: str) -> None:
    for spec in _specs(name):
        if spec.kind not in (INT, FLOAT) or spec.default is None:
            continue
        if spec.minimum is not None:
            assert spec.default >= spec.minimum, f"{name}.{spec.key} 默认值低于自己的下限"
        if spec.maximum is not None:
            assert spec.default <= spec.maximum, f"{name}.{spec.key} 默认值高于自己的上限"


@pytest.mark.parametrize("name", _NAMES)
def test_depends_on_points_at_a_real_sibling_value(name: str) -> None:
    by_key = {spec.key: spec for spec in _specs(name)}
    for spec in _specs(name):
        if spec.depends_on is None:
            continue
        parent_key, parent_value = spec.depends_on
        parent = by_key.get(parent_key)
        assert parent is not None, (
            f"{name}.{spec.key} 依赖不存在的 {parent_key}，这个控件永远不会出现"
        )
        if parent.kind == ENUM:
            assert parent_value in parent.values(), (
                f"{name}.{spec.key} 等 {parent_key}={parent_value!r}，"
                f"但它取不到这个值：{parent.values()}"
            )
        elif parent.kind == BOOL:
            assert isinstance(parent_value, bool), (
                f"{name}.{spec.key} 依赖的 {parent_key} 是开关，值必须是 bool"
            )


@pytest.mark.parametrize("name", _NAMES)
def test_catalog_survives_json(name: str) -> None:
    """控制台是通过 HTTP 拿这份清单的，序列化不了就等于这一页打不开。"""
    published = registry.options_catalog()[name]
    assert json.loads(json.dumps(published, ensure_ascii=False)) == published


@pytest.mark.parametrize("name", _NAMES)
def test_bad_values_fall_back_instead_of_exploding(name: str) -> None:
    """手写 yaml 一定会写错类型，那时要回落默认值，不能让整条推理链路挂掉。"""
    garbage = {spec.key: {"nested": ["nonsense"]} for spec in _specs(name)}
    options = OptionSet(_specs(name), garbage)
    for spec in _specs(name):
        assert options.get(spec.key) == spec.default, f"{name}.{spec.key} 收到垃圾值没有回落"


@pytest.mark.parametrize("name", _NAMES)
def test_unset_and_set_to_default_are_distinguishable(name: str) -> None:
    """采样参数靠这个区分：没配就整个键都不发，发了默认值反而会被新模型拒。"""
    empty = OptionSet(_specs(name), {})
    for spec in _specs(name):
        assert not empty.is_set(spec.key)
    filled = OptionSet(_specs(name), {spec.key: spec.default for spec in _specs(name)})
    for spec in _specs(name):
        if spec.default is None:
            continue
        assert filled.is_set(spec.key), f"{name}.{spec.key} 显式配了却被当成没配"


@pytest.mark.parametrize("name", _NAMES)
def test_degrade_rules_can_actually_change_something(name: str) -> None:
    """规则命中却改不动任何东西，就会在同一个 400 上把重试次数耗光。"""
    specs = _specs(name)
    rules = tuple(getattr(registry.get(name), "DEGRADE_RULES", ()))
    assert rules, f"{name} 没有任何降级规则"
    for rule in rules:
        # 逐个候选值试：规则要么把某项改成别的值，要么把它清掉。只要有一种
        # 起始状态能让它改动，这条规则就是有效的。反过来，一条在任何状态下都
        # 改不动的规则会在同一个 400 上白耗一轮重试。
        moved = any(
            rule.apply(OptionSet(specs, seed), rule.match[0])
            for seed in _seed_variants(specs)
        )
        assert moved, f"{name} 的规则「{rule.name}」在任何起始值下都改不动参数，命中它等于白重试一轮"


@pytest.mark.parametrize("name", _NAMES)
def test_degrade_rules_match_lowercase_only(name: str) -> None:
    """错误文本在比对前被转成小写，规则里留大写就永远命中不了。"""
    for rule in getattr(registry.get(name), "DEGRADE_RULES", ()):
        upper = [text for text in rule.match if text != text.lower()]
        assert not upper, f"{name} 的规则「{rule.name}」有大写匹配串，永远不会命中：{upper}"


# ---------------------------------------------------------------------------
# 五个 provider 各自解析各家的 usage，归一化结果必须是同一套键名，
# 否则 usage_tracker 那边按键名累加时会漏掉其中几家。
# ---------------------------------------------------------------------------

def _usage_samples() -> dict[str, dict]:
    from providers.anthropic import _parse_usage as anthropic_usage
    from providers.gemini import _parse_usage as gemini_usage
    from providers.openai import _extract_usage as openai_usage
    from providers.openai_responses import _parse_usage as responses_usage

    return {
        "anthropic": anthropic_usage({"usage": {
            "input_tokens": 40, "output_tokens": 20,
            "cache_read_input_tokens": 60, "cache_creation_input_tokens": 0,
            "output_tokens_details": {"thinking_tokens": 8},
        }}),
        "gemini": gemini_usage({
            "promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 120,
            "cachedContentTokenCount": 60, "thoughtsTokenCount": 8,
        }),
        "openai": openai_usage({"usage": {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 60},
            "completion_tokens_details": {"reasoning_tokens": 8},
        }}),
        "deepseek": openai_usage({"usage": {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_cache_hit_tokens": 60,
            "completion_tokens_details": {"reasoning_tokens": 8},
        }}),
        "openai-responses": responses_usage({
            "input_tokens": 100, "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 60},
            "output_tokens_details": {"reasoning_tokens": 8},
        }),
    }


@pytest.mark.parametrize("name", sorted(_usage_samples()))
def test_usage_is_normalized_to_the_same_keys(name: str) -> None:
    usage = _usage_samples()[name]
    assert usage["prompt_tokens"] == 100, f"{name} 的输入量没算全（缓存读写也算输入）"
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 120
    assert usage["cache_read_tokens"] == 60, f"{name} 没报缓存命中量，开了缓存也看不出省了多少"
    assert usage["reasoning_tokens"] == 8, f"{name} 没报推理消耗，开了思考也看不出多花了多少"


def test_usage_tracker_accumulates_every_normalized_key() -> None:
    """provider 报了但汇总处不认的键会被静默丢掉，账就永远对不上。"""
    source = (_ROOT / "engine" / "builtin" / "usage_tracker.py").read_text(encoding="utf-8")
    for key in sorted({key for usage in _usage_samples().values() for key in usage}):
        assert f'"{key}"' in source, f"usage_tracker 没有累加 {key}，这一项在任务级用量里会凭空消失"
