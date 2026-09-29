"""热载配置引擎：键声明、类型转换、按文件指纹重载、全局默认与平台两层叠加、按上下文钉住快照、写回与公布。

平台只声明自己有哪些键、配置文件在哪；缓存、锁和上下文快照都在 LiveConfig 实例上。
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping

import yaml

import config_layout

from . import capability
from .env import split_list
from .yaml_loader import load_yaml

if TYPE_CHECKING:
    from .console_schema import Control

logger = logging.getLogger(__name__)

# 生效方式。live 读到即生效；reconciled 的键改了以后还要有人去重建运行期对象
# （deque、worker task、HTTP server），由适配器自己的对齐逻辑负责。
RELOAD_LIVE = "live"
RELOAD_RECONCILED = "reconciled"

# 配置能写在哪一层，由宽到窄：全局默认 → 平台。窄层写了就盖住宽层。
LAYER_GLOBAL = "global"
LAYER_PLATFORM = "platform"
LAYERS: tuple[str, ...] = (LAYER_GLOBAL, LAYER_PLATFORM)
PLATFORM_ONLY: frozenset[str] = frozenset({LAYER_PLATFORM})
GLOBAL_ONLY: frozenset[str] = frozenset({LAYER_GLOBAL})
GLOBAL_AND_PLATFORM: frozenset[str] = frozenset(LAYERS)

# 生效值从哪来。两层各自就是来源；legacy 是只准写在全局的键，在平台文件的老位置上被读到。
SOURCE_GLOBAL = LAYER_GLOBAL
SOURCE_PLATFORM = LAYER_PLATFORM
SOURCE_LEGACY = "legacy"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"
# 运营者在平台那边亲手写下的兜底。只认全局的键在全局文件里没写时，平台按它们取值。
FALLBACK_SOURCES: frozenset[str] = frozenset({SOURCE_LEGACY, SOURCE_ENV})


@dataclass(frozen=True)
class Coercer:
    """把 yaml 值或 env 字符串转成运行期类型。"""

    convert: Callable[[Any, Any], Any]
    # 布尔开关的 env 设成空串就是「关」，不继续往后找下一个 env 名。
    # 列表/字符串相反 —— 空串等于没配，继续 fallback。
    empty_env_is_value: bool = False
    # 列表类键的单项规则。控制台按这份规则校验新增项，跨语言只能靠公布同一份数据对齐。
    # compare=False：只读映射不可哈希，算进字段就会让这个 frozen dataclass 失去 __hash__。
    input_rule: Mapping[str, Any] | None = field(default=None, compare=False)

    def __call__(self, raw: Any, default: Any) -> Any:
        return self.convert(raw, default)


_TRUTHY = frozenset({"1", "true", "yes", "y", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


def coerce_bool(raw: Any, default: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    token = str(raw if raw is not None else "").strip().lower()
    if token in _FALSY:
        return False
    if token in _TRUTHY:
        return True
    # 既不是真值也不是假值的字面量（"maybe"、"是"）不该悄悄变成 True。
    logger.warning("live config: unrecognized boolean %r, using %r", raw, default)
    return bool(default)


BOOL = Coercer(coerce_bool, empty_env_is_value=True)


def int_in(min_value: int | None = None, max_value: int | None = None) -> Coercer:
    def convert(raw: Any, default: Any) -> int:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = int(default)
        if min_value is not None:
            value = max(min_value, value)
        if max_value is not None:
            value = min(max_value, value)
        return value

    return Coercer(convert)


def float_in(min_value: float | None = None, max_value: float | None = None) -> Coercer:
    def convert(raw: Any, default: Any) -> float:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            value = float(default)
        if min_value is not None:
            value = max(min_value, value)
        if max_value is not None:
            value = min(max_value, value)
        return value

    return Coercer(convert)


# 词表单项：去首尾空白、非空、按原样去重。
WORD_INPUT_RULE: Mapping[str, Any] = MappingProxyType({"kind": "word", "trim": True})


def warn_bool_literal(raw: Any) -> None:
    logger.warning("live config: %r is a YAML boolean; quote it to use it as text", raw)


def _coerce_prefix_list(raw: Any, default: Any) -> tuple[str, ...]:
    if isinstance(raw, bool):
        warn_bool_literal(raw)
        raw = default
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            # 想拿 true 当词就得加引号：YAML 1.2 里裸 true 就是布尔，别的工具也这么读。
            if isinstance(item, bool):
                warn_bool_literal(item)
                continue
            token = str(item if item is not None else "").strip()
            if token and token not in out:
                out.append(token)
        return tuple(out)
    return split_list(str(raw or ""))


PREFIX_LIST = Coercer(_coerce_prefix_list, input_rule=WORD_INPUT_RULE)


def _coerce_trigger_mode(raw: Any, default: Any) -> str:
    if isinstance(raw, bool):
        warn_bool_literal(raw)
        return str(default)
    return str(raw or "").strip().lower() or str(default)


TRIGGER_MODE = Coercer(_coerce_trigger_mode)


def _coerce_tristate_bool(raw: Any, default: Any) -> bool | None:
    """三态开关：None = 没配，由消费方回落旧语义。

    七个触发信号里有四个（all/at/reply/prefix）在旧的 group_mode 枚举下已经有确定行为，
    直接给 True/False 会在升级时静默改变判定，所以要能表达「这一项我没管」。
    """
    if raw is None:
        return None
    return coerce_bool(raw, default if default is not None else False)


# 空 env 只能算「没配」：三态的 False 是运营者的显式选择，空串表达不了它。
TRISTATE_BOOL = Coercer(_coerce_tristate_bool)


def _coerce_word_list(raw: Any, default: Any) -> tuple[str, ...]:
    """名字/关键词列表。和触发前缀同样的解析，只是不做前缀那套剥离约定。"""
    return _coerce_prefix_list(raw, default)


WORD_LIST = Coercer(_coerce_word_list, input_rule=WORD_INPUT_RULE)


def _coerce_plain_str(raw: Any, default: Any) -> str:
    if isinstance(raw, bool):
        warn_bool_literal(raw)
        return str(default)
    return str(raw if raw is not None else "").strip() or str(default)


PLAIN_STR = Coercer(_coerce_plain_str)


def grant_for(capability_key: str) -> Coercer:
    """能力授权档。夹紧就在这里做一次，各个判定点都不必再操心上限。"""
    return Coercer(lambda raw, default: capability.clamp(capability_key, raw))


@dataclass(frozen=True)
class DerivedDefault:
    """从别的 env 推导出的默认值，以及它是不是真取自 env：取自 env 时来源报 env，控制台显示的来源才对得上。"""

    value: Any
    from_env: bool


@dataclass(frozen=True)
class LiveKey:
    """一个热载键。name 是 call site 用的属性名，path 是配置文件里的点分路径，layers 是它能写在哪几层，
    ui 是控制台上怎么画它；没有 ui 的键控制台上没有控件，要么只读，要么由专门的组件管。
    """

    name: str
    path: str
    coerce: Coercer
    default: Any
    env: tuple[str, ...] = ()
    reload: str = RELOAD_LIVE
    # env 也没配时的兜底。只给那几个「默认值本身要从别的 env 推导」的键用。
    derive_default: Callable[[], DerivedDefault] | None = None
    note: str = ""
    layers: frozenset[str] = PLATFORM_ONLY
    ui: Control | None = None

    def resolve(self, documents: Mapping[str, Mapping[str, Any]]) -> tuple[Any, str]:
        """生效值和它的来源。documents 是层名到那一层文档的映射，没有的层不给。

        窄层盖宽层；只准写在全局的键，平台文件老位置上的写法排在全局之后兜底。
        env 排在两层文件之后：控制台改了全局值，不该被一行早就忘了的 env 顶掉。
        """
        for layer in reversed(LAYERS):
            document = documents.get(layer)
            if document is None or layer not in self.layers:
                continue
            raw = _dig(document, self.path)
            if raw is not None:
                return self.coerce(raw, self.default), layer
        platform = documents.get(LAYER_PLATFORM)
        if platform is not None and LAYER_PLATFORM not in self.layers:
            raw = _dig(platform, self.path)
            if raw is not None:
                return self.coerce(raw, self.default), SOURCE_LEGACY
        for env_name in self.env:
            value = os.environ.get(env_name)
            if value is None:
                continue
            if not value.strip() and not self.coerce.empty_env_is_value:
                continue
            return self.coerce(value, self.default), SOURCE_ENV
        if self.derive_default is not None:
            derived = self.derive_default()
            return self.coerce(derived.value, self.default), SOURCE_ENV if derived.from_env else SOURCE_DEFAULT
        return self.coerce(self.default, self.default), SOURCE_DEFAULT


def _dig(document: Mapping[str, Any], dotted: str) -> Any:
    cur: Any = document
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _proper_prefixes(dotted: str) -> list[str]:
    parts = dotted.split(".")
    return [".".join(parts[:end]) for end in range(1, len(parts))]


def _check_declarations(keys: tuple[LiveKey, ...], derived_names: tuple[str, ...]) -> None:
    """键表写错就在导入时报：重名会让后一个静默盖掉前一个，路径互为前缀会让写回时一个压扁另一个。"""
    names = [key.name for key in keys]
    paths = [key.path for key in keys]
    declared_paths = set(paths)
    problems = [
        *(f"duplicate key name {name!r}" for name, count in sorted(Counter(names).items()) if count > 1),
        *(f"duplicate path {path!r}" for path, count in sorted(Counter(paths).items()) if count > 1),
        *(f"derived name {name!r} shadows a declared key" for name in sorted(set(derived_names) & set(names))),
        *(f"key {key.name!r} declares no layer" for key in keys if not key.layers),
        *(
            f"key {key.name!r} declares unknown layers {sorted(set(key.layers) - set(LAYERS))}"
            for key in keys
            if not set(key.layers) <= set(LAYERS)
        ),
        *(
            f"path {prefix!r} is a prefix of {path!r}"
            for path in sorted(declared_paths)
            for prefix in _proper_prefixes(path)
            if prefix in declared_paths
        ),
    ]
    if problems:
        raise ValueError("invalid live config declarations: " + "; ".join(problems))


def _check_layering(keys: tuple[LiveKey, ...], layer: str, defaults: "LiveConfig | None") -> None:
    """层写错在构造时报：全局文件不认 env，平台键表里引用的全局键得和全局声明逐项一致。"""
    problems: list[str] = []
    if layer == LAYER_GLOBAL:
        if defaults is not None:
            problems.append("the global layer has nothing wider to fall back on")
        problems.extend(f"key {key.name!r} cannot be set globally" for key in keys if LAYER_GLOBAL not in key.layers)
        # env 兜底只留给平台已有的键：全局层再读 env，就分不清是哪个平台的旧设置。
        problems.extend(
            f"global key {key.name!r} reads env" for key in keys if key.env or key.derive_default is not None
        )
    elif layer == LAYER_PLATFORM:
        if defaults is not None and defaults.layer != LAYER_GLOBAL:
            problems.append("defaults must come from the global layer")
        for key in keys:
            if LAYER_GLOBAL not in key.layers:
                continue
            declared = defaults.keys_by_name.get(key.name) if defaults is not None else None
            if declared is None:
                problems.append(f"key {key.name!r} has a global layer that nobody declares")
            elif (declared.path, declared.coerce, declared.default, declared.layers, declared.ui) != (
                key.path, key.coerce, key.default, key.layers, key.ui,
            ):
                # 两边各写一份迟早会漂移：同一个键在全局文件里是一套值域、一套说法，在平台文件里是另一套。
                problems.append(f"key {key.name!r} drifted from its global declaration")
    else:
        problems.append(f"unknown layer {layer!r}")
    if problems:
        raise ValueError("invalid live config layering: " + "; ".join(problems))


# 文件不在时的指纹，和 config_layout.file_stamp 同形状。
_MISSING_STAMP: tuple[int, int, int] = (0, 0, 0)


def _file_stamp(path: Path) -> tuple[int, int, int]:
    return config_layout.file_stamp(path) or _MISSING_STAMP


def jsonable(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        # 集合没有顺序，落盘前定序，否则每次写出的 yaml 都不一样。
        return sorted(value)
    if isinstance(value, (tuple, list)):
        return list(value)
    return value


def _parse_document(path: Path, stamp: tuple[int, int, int]) -> tuple[Mapping[str, Any] | None, str]:
    """文件解析成文档。文件不存在是空文档；解析不了返回 None 和原因。"""
    if stamp == _MISSING_STAMP:
        return {}, ""
    try:
        loaded = load_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if loaded is None:
        return {}, ""
    if isinstance(loaded, Mapping):
        return loaded, ""
    return None, f"expected a mapping at the top level, got {type(loaded).__name__}"


@dataclass
class _Cache:
    path: str = ""
    # None = 强制重解析。和 valid 分开两个字段：invalidate() 只能让缓存过期，
    # 不能把「上一份有效配置」一起丢掉 —— 否则写坏 yaml 时白名单会塌成空。
    stamp: tuple[int, int, int] | None = None
    # values 是从哪个文件版本来的。和 stamp 不同：解析失败时 stamp 记的是坏文件，
    # values 还是旧的那份 —— 外部要靠这个才能区分「已生效」和「还在用旧配置」。
    values_stamp: tuple[int, int, int] = _MISSING_STAMP
    document: Mapping[str, Any] = field(default_factory=dict)
    # 采纳过几次文档。拿这一层当默认值的配置按它判断要不要重算：看的是这层换没换文档，
    # 全局文件写坏、留用上一份时不必重算。
    generation: int = 0
    values: Mapping[str, Any] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)
    # values 是按全局那层的第几次采纳算的；没有全局层时恒为 None。
    defaults_generation: int | None = None
    error: str = ""
    valid: bool = False


class LiveConfig:
    """一层热载配置：键表和配置文件位置，外加按文件指纹缓存的生效值、按上下文钉住的快照。

    平台那层挂上全局那层当 defaults：平台文件没写的键落到全局文件，两份文件哪份变了都重算。
    """

    def __init__(
        self,
        keys: Iterable[LiveKey],
        *,
        path: Callable[[], Path],
        derive: Callable[[dict[str, Any]], None] | None = None,
        derived_names: Iterable[str] = (),
        layer: str = LAYER_PLATFORM,
        defaults: "LiveConfig | None" = None,
    ) -> None:
        self.keys: tuple[LiveKey, ...] = tuple(keys)
        self.derived_names: tuple[str, ...] = tuple(derived_names)
        _check_declarations(self.keys, self.derived_names)
        self.keys_by_name: Mapping[str, LiveKey] = MappingProxyType({key.name: key for key in self.keys})
        _check_layering(self.keys, layer, defaults)
        self.layer = layer
        self.defaults = defaults
        self.reconciled_keys: tuple[str, ...] = tuple(
            key.name for key in self.keys if key.reload == RELOAD_RECONCILED
        )
        self._path = path
        self._derive = derive
        self._cache = _Cache()
        self._cache_lock = threading.Lock()
        self._snapshot: ContextVar[Mapping[str, Any] | None] = ContextVar("live_config_snapshot", default=None)
        # 只准写在全局的键还留在平台文件里时报过的状态，同一种情况只报一次。
        self._misplaced_reported: dict[str, str] = {}
        self.live = LiveReader(self)

    def path(self) -> Path:
        return self._path()

    def _build(
        self, document: Mapping[str, Any], defaults_document: Mapping[str, Any] | None,
    ) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        documents: dict[str, Mapping[str, Any]] = {self.layer: document}
        if defaults_document is not None:
            documents[LAYER_GLOBAL] = defaults_document
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for key in self.keys:
            values[key.name], sources[key.name] = key.resolve(documents)
        if self._derive is not None:
            self._derive(values)
        self._report_misplaced(document, sources)
        return MappingProxyType(values), MappingProxyType(sources)

    def _report_misplaced(self, document: Mapping[str, Any], sources: Mapping[str, str]) -> None:
        """只准写在全局的键还写在平台文件里：还在兜底的提醒挪走，已被全局盖住的提醒删掉。"""
        if self.defaults is None:
            return
        for key in self.keys:
            if LAYER_PLATFORM in key.layers or _dig(document, key.path) is None:
                self._misplaced_reported.pop(key.name, None)
                continue
            source = sources[key.name]
            if self._misplaced_reported.get(key.name) == source:
                continue
            self._misplaced_reported[key.name] = source
            here, there = self._path().name, self.defaults.path().name
            if source == SOURCE_LEGACY:
                logger.warning(
                    "%s still sets %s; it belongs in %s and is only read here as a fallback", here, key.path, there,
                )
            else:
                logger.warning(
                    "%s sets %s, but %s sets it too and wins; the line in %s has no effect", here, key.path, there, here,
                )

    def _adopt_file_locked(self, path: Path, key: str, stamp: tuple[int, int, int]) -> None:
        """重解析这一层的文件并采纳。持有 _cache_lock 时调。"""
        cache = self._cache
        document, error = _parse_document(path, stamp)
        if document is None:
            # 保留上一份有效配置而不是回落到默认值：群白名单空 = 全群静默，
            # 管理员名单空 = 审批全拒 —— 写坏一个字符不该等于把 bot 关掉。
            # stamp 照旧记下，否则每次读都重试解析同一个坏文件、刷满日志。
            logger.error("%s is unusable (%s); keeping the previous configuration", path.name, error)
            had_valid = cache.valid and cache.path == key
            cache.path = key
            cache.stamp = stamp
            cache.error = error
            if not had_valid:
                # 从没读到过有效内容（进程起来时文件就是坏的），只能用 env + 默认值兜。
                cache.document = {}
                cache.values_stamp = _MISSING_STAMP
                cache.generation += 1
                cache.valid = True
            return
        cache.path = key
        cache.stamp = stamp
        cache.values_stamp = stamp
        cache.document = document
        cache.generation += 1
        cache.error = ""
        cache.valid = True

    def _reload_if_changed(self) -> Mapping[str, Any]:
        """按文件指纹判断要不要重解析；解析失败保留上一份有效配置。全局那层换了文档也要重算。"""
        # 先在锁外拿全局那层：两把锁不嵌套，谁先谁后都不会死锁。
        defaults_document, defaults_generation = (
            self.defaults.adopted_document() if self.defaults is not None else (None, None)
        )
        path = self._path()
        stamp = _file_stamp(path)
        key = str(path)
        cache = self._cache
        with self._cache_lock:
            current = cache.valid and cache.path == key and cache.stamp == stamp
            if current and cache.defaults_generation == defaults_generation:
                return cache.values
            if not current:
                self._adopt_file_locked(path, key, stamp)
            cache.defaults_generation = defaults_generation
            cache.values, cache.sources = self._build(cache.document, defaults_document)
            return cache.values

    def resolved(self) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        """生效值和每个键的来源，出自同一次重载。不看上下文里钉住的快照：钉住的只有值，没有来源。"""
        self._reload_if_changed()
        with self._cache_lock:
            return self._cache.values, self._cache.sources

    def adopted_document(self) -> tuple[Mapping[str, Any], int]:
        """当前采纳的文档和第几次采纳。别的层拿它当默认值；文件写坏时给的是上一份有效的。"""
        self._reload_if_changed()
        with self._cache_lock:
            return self._cache.document, self._cache.generation

    def refresh(self) -> Mapping[str, Any]:
        """重读配置并钉进当前上下文。

        在每个入口（收到消息、外部请求、队列取件）调一次，之后整条处理链读到的都是
        同一份快照 —— 运营者中途改白名单不会让处理到一半的消息换标准。
        """
        snapshot = self._reload_if_changed()
        self._snapshot.set(snapshot)
        return snapshot

    def current(self) -> Mapping[str, Any]:
        """当前生效快照。上下文里钉过就用那份，没钉过就实时读文件。

        没钉过时选「实时」而不是「进程启动那份」：漏调 refresh 的后果变成多一次 stat，
        而不是配置永久冻结在启动时刻。
        """
        pinned = self._snapshot.get()
        return pinned if pinned is not None else self._reload_if_changed()

    @contextmanager
    def pinned(self, **overrides: Any) -> Iterator[Mapping[str, Any]]:
        """在当前上下文里钉住快照，可临时替换若干键。"""
        unknown = sorted(set(overrides) - set(self.keys_by_name) - set(self.derived_names))
        if unknown:
            raise KeyError(f"unknown live config keys: {unknown}")
        values = dict(self.current())
        values.update(overrides)
        if self._derive is not None:
            # 派生值跟着换掉的输入重算，调用方显式给的派生值优先。
            self._derive(values)
            values.update((name, overrides[name]) for name in self.derived_names if name in overrides)
        snapshot = MappingProxyType(values)
        token = self._snapshot.set(snapshot)
        try:
            yield snapshot
        finally:
            self._snapshot.reset(token)

    def invalidate(self) -> None:
        """让文件缓存过期，下一次读必定重新解析。

        只动缓存不动上下文里钉住的快照 —— 钉住的语义就是「这条消息处理期间配置不变」，
        别人改文件不该把它掀掉。
        """
        with self._cache_lock:
            self._cache.stamp = None

    def reload(self) -> Mapping[str, Any]:
        """缓存过期、解开当前上下文钉住的快照，再读一遍。改完配置要当场看到新值时调。"""
        self.invalidate()
        self._snapshot.set(None)
        return self._reload_if_changed()

    def save(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        """在本进程内写回配置文件并立刻重载，返回新的生效快照。

        生产写入在 supervisor（bot 没起来时也要能改配置），这里是测试与本地工具的入口。
        dump 阶段就抛掉表示不出来的值；写入和 supervisor 同一把锁、同一套同目录临时文件替换，避免 bot 读到半截 yaml。
        """
        path = self._path()
        if not isinstance(document, Mapping):
            raise TypeError(f"{path.name} must be a mapping, got {type(document).__name__}")
        text = yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=False)
        config_layout.write_config_text(path, text)
        # 连调用方自己钉住的快照一起解开：保存完立刻读到旧值会被当成「没生效」。
        return self.reload()

    def raw_document(self) -> dict[str, Any]:
        """配置文件的原始内容（已解析成 dict）。给 save() 的合并基底用。"""
        self._reload_if_changed()
        with self._cache_lock:
            return copy.deepcopy(dict(self._cache.document))

    def document_from(self, values: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """把 {键名: 值} 摊成配置文件的嵌套结构，可选择并进已有文档。

        调用方只认键名，点分路径只此一处知道 —— 换 yaml 布局不必改调用方。
        """
        unknown = sorted(set(values) - set(self.keys_by_name))
        if unknown:
            raise KeyError(f"unknown live config keys: {unknown}")
        misplaced = sorted(name for name in values if self.layer not in self.keys_by_name[name].layers)
        if misplaced:
            raise KeyError(f"not settable in {self._path().name}: {misplaced}")
        document: dict[str, Any] = copy.deepcopy(dict(base or {}))
        for name, value in values.items():
            cursor = document
            parts = self.keys_by_name[name].path.split(".")
            for part in parts[:-1]:
                nested = cursor.get(part)
                if not isinstance(nested, dict):
                    nested = {}
                    cursor[part] = nested
                cursor = nested
            cursor[parts[-1]] = jsonable(value)
        return document

    def file_state(self) -> dict[str, Any]:
        """这一层的文件：位置、磁盘上的指纹、生效值来自哪个版本、解析错误。"""
        self._reload_if_changed()
        path = self._path()
        stamp = _file_stamp(path)
        with self._cache_lock:
            error = self._cache.error
            values_stamp = self._cache.values_stamp
        return {
            "config_path": str(path),
            "file": {
                "exists": stamp != _MISSING_STAMP,
                "mtime_ns": stamp[0],
                "size": stamp[1],
            },
            # values 实际来自哪个文件版本。和 file 不一致就说明这份文件没被采纳。
            "loaded": {
                "exists": values_stamp != _MISSING_STAMP,
                "mtime_ns": values_stamp[0],
                "size": values_stamp[1],
            },
            "parse_error": error,
        }

    def state_payload(self) -> dict[str, Any]:
        """进程实际生效的配置快照 + 文件指纹，由适配器公布给控制台。"""
        values = self.current()
        payload = self.file_state()
        _, sources = self.resolved()
        payload.update({
            "layer": self.layer,
            "values": {name: jsonable(values[name]) for name in sorted(values)},
            # 每个键的值从哪来。控制台据此标「全局 / 本平台 / 旧位置 / .env / 默认」。
            "sources": {key.name: sources.get(key.name, SOURCE_DEFAULT) for key in self.keys},
            "layers": {key.name: sorted(key.layers) for key in self.keys},
            "reload": {key.name: key.reload for key in self.keys},
            "notes": {key.name: key.note for key in self.keys if key.note},
            # 点分路径一起公布：supervisor 校验 PUT 上来的 yaml 时要认得哪些路径有人消费，
            # 以正在运行的 bot 实际加载的这张表为准；自己导入会牵出读适配器环境变量的 config。
            "paths": {key.name: key.path for key in self.keys},
            # 列表项规则同样由 bot 公布：前端重写一遍就会和 coercer 漂移，
            # 而漂移的表现是「界面收下了、保存后消失」。
            "input_rules": {
                key.name: dict(key.coerce.input_rule)
                for key in self.keys
                if key.coerce.input_rule is not None
            },
        })
        if self.defaults is not None:
            # bot 看到的全局文件。和控制台读到的不一致，说明全局那层改完还没被这个进程采纳。
            payload["defaults"] = self.defaults.file_state()
        return payload


class LiveReader:
    """`live.allowed_groups` 形式的读取入口。拼错键名立刻 AttributeError。"""

    __slots__ = ("_config",)

    def __init__(self, config: LiveConfig) -> None:
        object.__setattr__(self, "_config", config)

    def __getattr__(self, name: str) -> Any:
        # 绕开 __getattr__ 取槽位：槽位没填时直接 AttributeError，而不是再进一次 __getattr__。
        values = object.__getattribute__(self, "_config").current()
        try:
            return values[name]
        except KeyError:
            raise AttributeError(f"no live config key named {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        config = object.__getattribute__(self, "_config")
        raise AttributeError(f"live config is read-only; edit {config.path().name} or use pinned()")

    def __dir__(self) -> list[str]:
        config = object.__getattribute__(self, "_config")
        return sorted(set(config.keys_by_name) | set(config.derived_names))
