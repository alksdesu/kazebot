"""控制台怎么画热载配置：键自带控件说明，分区是引用键名的布局树，前端只按这份数据渲染。

说明文字里用反引号标代码片段，前端渲染成等宽字。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, Union

from .live_config import jsonable

if TYPE_CHECKING:
    from .live_config import LiveKey

SCHEMA_VERSION = 1

CONTROL_BOOL = "bool"
CONTROL_TRISTATE = "tristate"
CONTROL_NUMBER = "number"
CONTROL_PERCENT = "percent"
CONTROL_TEXT = "text"
CONTROL_CHOICE = "choice"
CONTROL_WORDS = "words"
CONTROL_IDS = "ids"
CONTROLS: frozenset[str] = frozenset({
    CONTROL_BOOL, CONTROL_TRISTATE, CONTROL_NUMBER, CONTROL_PERCENT, CONTROL_TEXT, CONTROL_CHOICE,
    CONTROL_WORDS, CONTROL_IDS,
})
# 名单控件的排布：panel 放进带框的面板，loose 是分区底部那种松散排布。
SPACING_PANEL = "panel"
SPACING_LOOSE = "loose"
SPACINGS: frozenset[str] = frozenset({"", SPACING_PANEL, SPACING_LOOSE})
FRAME_RULED = "ruled"
FRAME_BOXED = "boxed"
FRAMES: frozenset[str] = frozenset({"", FRAME_RULED, FRAME_BOXED})

_GROUP_KINDS: frozenset[str] = frozenset({"grid", "panel", "loose"})
_TEXT_KINDS: frozenset[str] = frozenset({"desc", "note"})


def _compact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """去掉没填的可选项：前端按「没有这个字段」取默认，空串和 null 会被当成填了。"""
    return {name: value for name, value in payload.items() if value not in ("", None, [])}


@dataclass(frozen=True)
class Control:
    """键在控制台上的样子。同一个键不管出现在全局页还是平台页，标签和说明都是这一份。"""

    kind: str
    label: str
    desc: str = ""
    unit: str = ""
    step: float | None = None
    max_value: float | None = None
    # 界面单位和配置单位的倍数：配置按字节存、界面按 MB 填时是 1024*1024。
    scale: int | None = None
    choices: tuple[tuple[str, str], ...] = ()
    empty: str = ""
    placeholder: str = ""
    # 三态没配时算开还是关：跟着 follows 那个键推导（它取 follows_when 里的值时算开），不跟就是 unset。
    follows: str = ""
    follows_when: frozenset[str] = frozenset()
    unset: bool = False

    def payload(self) -> dict[str, Any]:
        constant_unset = self.kind == CONTROL_TRISTATE and not self.follows
        return _compact({
            "kind": self.kind,
            "label": self.label,
            "desc": self.desc,
            "unit": self.unit,
            "step": self.step,
            "max": self.max_value,
            "scale": self.scale,
            "choices": [list(pair) for pair in self.choices],
            "empty": self.empty,
            "placeholder": self.placeholder,
            "follows": self.follows,
            "follows_when": sorted(self.follows_when),
            "unset": self.unset if constant_unset else None,
        })


def toggle(label: str, desc: str = "") -> Control:
    return Control(CONTROL_BOOL, label, desc)


def tristate(label: str, desc: str, *, unset: bool = False, follows: str = "", when: Iterable[str] = ()) -> Control:
    return Control(CONTROL_TRISTATE, label, desc, follows=follows, follows_when=frozenset(when), unset=unset)


def number(
    label: str,
    *,
    unit: str = "",
    step: float | None = None,
    max_value: float | None = None,
    scale: int | None = None,
) -> Control:
    return Control(CONTROL_NUMBER, label, unit=unit, step=step, max_value=max_value, scale=scale)


def percent(label: str) -> Control:
    return Control(CONTROL_PERCENT, label)


def text_input(label: str) -> Control:
    return Control(CONTROL_TEXT, label)


def choice(label: str, choices: Iterable[tuple[str, str]]) -> Control:
    return Control(CONTROL_CHOICE, label, choices=tuple(choices))


def word_list(label: str, *, empty: str, placeholder: str) -> Control:
    return Control(CONTROL_WORDS, label, empty=empty, placeholder=placeholder)


def id_list(label: str, *, empty: str, placeholder: str) -> Control:
    return Control(CONTROL_IDS, label, empty=empty, placeholder=placeholder)


@dataclass(frozen=True)
class Block:
    title: str
    children: tuple[Node, ...] = ()
    hint: str = ""
    frame: str = ""

    def payload(self) -> dict[str, Any]:
        return _compact({
            "kind": "block", "title": self.title, "hint": self.hint, "frame": self.frame,
            "children": [child.payload() for child in self.children],
        })


@dataclass(frozen=True)
class Group:
    """只管排布的容器：grid 两列栅格，panel 带框面板，loose 松散排布。"""

    kind: str
    children: tuple[Node, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "children": [child.payload() for child in self.children]}


@dataclass(frozen=True)
class Fixed:
    """几项并排成一组、没有总开关的那种格子。"""

    label: str
    children: tuple[Node, ...] = ()
    desc: str = ""

    def payload(self) -> dict[str, Any]:
        return _compact({
            "kind": "fixed", "label": self.label, "desc": self.desc,
            "children": [child.payload() for child in self.children],
        })


@dataclass(frozen=True)
class FieldNode:
    """摆一个键的控件。children 挂在开关下面，开关关着时一起收起。"""

    key: str
    children: tuple[Node, ...] = ()
    sub: bool = False
    spacing: str = ""

    def payload(self) -> dict[str, Any]:
        return _compact({
            "kind": "field", "key": self.key, "sub": True if self.sub else None, "spacing": self.spacing,
            "children": [child.payload() for child in self.children],
        })


@dataclass(frozen=True)
class Text:
    kind: str
    text: str
    indent: bool | None = None

    def payload(self) -> dict[str, Any]:
        return _compact({"kind": self.kind, "text": self.text, "indent": self.indent})


@dataclass(frozen=True)
class Custom:
    """编不成字段的那一块（扫码、试听、能力清单），由前端按「平台:分区」登记的组件画。"""

    name: str

    def payload(self) -> dict[str, Any]:
        return {"kind": "custom", "name": self.name}


Node = Union[Block, Group, Fixed, FieldNode, Text, Custom]


def block(title: str, *children: Node, hint: str = "", frame: str = "") -> Block:
    return Block(title, children, hint=hint, frame=frame)


def grid(*children: Node) -> Group:
    return Group("grid", children)


def panel(*children: Node) -> Group:
    return Group("panel", children)


def loose(*children: Node) -> Group:
    return Group("loose", children)


def fixed(label: str, *children: Node, desc: str = "") -> Fixed:
    return Fixed(label, children, desc=desc)


def field(key: str, *children: Node, sub: bool = False, spacing: str = "") -> FieldNode:
    return FieldNode(key, children, sub=sub, spacing=spacing)


def paragraph(text: str, *, indent: bool | None = None) -> Text:
    return Text("desc", text, indent=indent)


def note(text: str) -> Text:
    return Text("note", text)


def custom(name: str) -> Custom:
    return Custom(name)


@dataclass(frozen=True)
class Section:
    """控制台上的一个分区。收起时那行摘要由前端画，正文全在 nodes 里。"""

    id: str
    label: str
    icon: str
    note: str
    nodes: tuple[Node, ...]
    # 归这个分区管的配置段，控制台按它把「N 项待应用」算到分区头上。空表示这一块不写配置。
    paths: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "icon": self.icon, "note": self.note,
            "paths": list(self.paths), "nodes": [node.payload() for node in self.nodes],
        }


def section(
    section_id: str, label: str, *nodes: Node, icon: str, note: str, paths: Iterable[str] = (),
) -> Section:
    return Section(section_id, label, icon, note, nodes, tuple(paths))


def walk(nodes: Iterable[Node]) -> Iterator[Node]:
    """深度优先走一遍布局树，先父后子。"""
    for node in nodes:
        yield node
        yield from walk(getattr(node, "children", ()))


def covers(prefixes: Iterable[str], path: str) -> bool:
    """path 落不落在这些配置段里。控制台按同一条规则把「N 项待应用」算到分区头上。"""
    return any(path == prefix or path.startswith(f"{prefix}.") for prefix in prefixes)


def field_keys(sections: Iterable[Section]) -> list[str]:
    """布局里摆出来的键，按出现顺序，重复的也列出来。"""
    return [node.key for part in sections for node in walk(part.nodes) if isinstance(node, FieldNode)]


def _node_problems(part: Section, node: Node, keys: Mapping[str, LiveKey]) -> list[str]:
    where = f"section {part.id!r}"
    if isinstance(node, Group) and node.kind not in _GROUP_KINDS:
        return [f"{where} has an unknown container {node.kind!r}"]
    if isinstance(node, Text) and node.kind not in _TEXT_KINDS:
        return [f"{where} has an unknown text kind {node.kind!r}"]
    if isinstance(node, Block) and node.frame not in FRAMES:
        return [f"{where} has an unknown frame {node.frame!r}"]
    if isinstance(node, Custom) and not node.name:
        return [f"{where} has a custom block without a name"]
    if not isinstance(node, FieldNode):
        return []
    key = keys.get(node.key)
    if key is None:
        return [f"{where} places unknown key {node.key!r}"]
    control = getattr(key, "ui", None)
    if control is None:
        return [f"key {node.key!r} has no console control"]
    problems: list[str] = []
    if not covers(part.paths, key.path):
        # 落在分区配置段外面的键，改了不会算进这个分区的待应用数。
        problems.append(f"{where} places key {node.key!r} outside its paths")
    if control.kind not in CONTROLS:
        problems.append(f"key {node.key!r} has an unknown control {control.kind!r}")
    if node.spacing not in SPACINGS:
        problems.append(f"key {node.key!r} has an unknown spacing {node.spacing!r}")
    if (node.sub or node.children) and control.kind != CONTROL_BOOL:
        # 只有开关能收起下面的项，也只有开关能缩成一行小勾选。
        problems.append(f"key {node.key!r} is not a switch, so it cannot be a sub-option or hold children")
    if control.kind == CONTROL_CHOICE and not control.choices:
        problems.append(f"key {node.key!r} is a choice without choices")
    if control.follows and control.follows not in keys:
        problems.append(f"key {node.key!r} follows unknown key {control.follows!r}")
    return problems


def schema_problems(sections: Iterable[Section], keys: Iterable[LiveKey]) -> list[str]:
    parts = tuple(sections)
    by_name = {key.name: key for key in keys}
    problems: list[str] = [
        *(f"duplicate section {part_id!r}" for part_id, count in sorted(Counter(p.id for p in parts).items()) if count > 1),
        # 同一个键摆两处，两处的草稿会互相覆盖，「N 项待应用」也会算两遍。
        *(f"key {name!r} appears more than once" for name, count in sorted(Counter(field_keys(parts)).items()) if count > 1),
        *_overlapping_claims(parts),
    ]
    for part in parts:
        for node in walk(part.nodes):
            problems.extend(_node_problems(part, node, by_name))
    return problems


def _overlapping_claims(parts: tuple[Section, ...]) -> list[str]:
    """两个分区认领了重叠的配置段，其中的改动会在两边各算一次待应用。"""
    claims = [(part.id, path) for part in parts for path in part.paths]
    return [
        f"sections {first!r} and {second!r} both claim {max(one, other, key=len)!r}"
        for (first, one), (second, other) in combinations(claims, 2)
        if first != second and (covers((one,), other) or covers((other,), one))
    ]


def build_schema(sections: Iterable[Section], keys: Iterable[LiveKey]) -> dict[str, Any]:
    """一份平台或全局页的 schema：分区的布局树，加上布局里用到的每个键的控件说明。"""
    parts = tuple(sections)
    by_name = {key.name: key for key in keys}
    problems = schema_problems(parts, by_name.values())
    if problems:
        raise ValueError("invalid console schema: " + "; ".join(problems))
    return {
        "version": SCHEMA_VERSION,
        "sections": [part.payload() for part in parts],
        "fields": {name: _field_payload(by_name[name]) for name in field_keys(parts)},
    }


def _field_payload(key: LiveKey) -> dict[str, Any]:
    return {
        **key.ui.payload(),
        "path": key.path,
        "layers": sorted(key.layers),
        "default": jsonable(key.default),
    }
