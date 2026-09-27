"""按新的会话键摘要搬迁记忆目录、命中缓存与路由状态，并给出各个按会话键存数据的库共用的新旧对照。

换盐、换 bot 账号和跨平台绑定共用这一套：摘要怎么算由调用方决定，本模块只负责「算出新旧对照，
然后一样不落地搬过去」。路由状态文件由平台描述给出。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping

import engine.memory_hit_cache as hit_cache
from engine.eventlog_rotation import eventlog_file_lock
from engine.memory_namespace import CONVERSATION_PREFIX, conversation_namespace
from platforms.descriptor import STATE_ROUTES, PlatformDescriptor, normalize_name
from platforms.registry import conversation_channel, conversation_key_prefix, platform_state_path

HIT_KEY_SEP = "/"


class RekeyConflict(ValueError):
    """两边各有一份、谁顶掉谁都会丢东西的数据：整次迁移停下，让人先处理。"""


@dataclass(frozen=True)
class RekeyCount:
    """一个库改写的行数；replaced 是撞上目标已有的数据、被源会话顶掉的行。"""

    moved: int = 0
    replaced: int = 0

    def __add__(self, other: "RekeyCount") -> "RekeyCount":
        return RekeyCount(self.moved + other.moved, self.replaced + other.replaced)

    def __bool__(self) -> bool:
        return bool(self.moved or self.replaced)


@dataclass(frozen=True)
class ConversationRekey:
    """一次迁移里会话键的新旧对照，连同跟着换的机器人账号。

    只改会话键落在对照里的记录：渠道名原来就是旧键的前缀才跟着换（web 这类发起渠道不动），
    平台 id 原来写的是旧键所在的平台、新键又落在另一个平台才跟着换，账号按 bot_scopes 换。
    """

    keys: Mapping[str, str]
    bot_scopes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        pairs = {str(old): str(new) for old, new in self.keys.items() if str(old) and str(new) and str(old) != str(new)}
        # 链式、多对一的对照让结果取决于改写顺序：a→b、b→c 先改哪条都不对。
        if set(pairs.values()) & set(pairs):
            raise ValueError("a new conversation key is also an old one in the same rekey")
        if len(set(pairs.values())) != len(pairs):
            raise ValueError("two conversation keys would be merged into one")
        scopes = {str(old): str(new) for old, new in self.bot_scopes.items() if str(new) and str(old) != str(new)}
        object.__setattr__(self, "keys", MappingProxyType(pairs))
        object.__setattr__(self, "bot_scopes", MappingProxyType(scopes))

    def __bool__(self) -> bool:
        return bool(self.keys)

    def items(self) -> Iterator[tuple[str, str]]:
        return iter(self.keys.items())

    def key(self, value: Any) -> str | None:
        """对照里的新键；不在对照里返回 None。"""
        return self.keys.get(str(value or "").strip())

    def channel(self, old_key: str, new_key: str, value: Any) -> Any:
        text = str(value or "").strip()
        return conversation_key_prefix(new_key) if text and text == conversation_key_prefix(old_key) else value

    def platform(self, old_key: str, new_key: str, value: Any) -> Any:
        """跨平台绑定时，写着旧平台 id 或它历史别名的换成新平台 id；同平台迁移、认不出平台的键都原样。"""
        old_platform, new_platform = _key_platform(old_key), _key_platform(new_key)
        if old_platform is None or new_platform is None or old_platform.id == new_platform.id:
            return value
        return new_platform.id if normalize_name(value) in {old_platform.id, *old_platform.aliases} else value

    def bot_scope(self, value: Any) -> Any:
        return self.bot_scopes.get(str(value or "").strip(), value)

    def rewrite(
        self, document: dict[str, Any], old_key: str, new_key: str, *,
        keys: Iterable[str] = (), channels: Iterable[str] = (), platforms: Iterable[str] = (),
        bot_scopes: Iterable[str] = (),
    ) -> None:
        """改一条记录里列出的字段：keys 按对照换键，channels、platforms 按渠道和平台规则换，bot_scopes 按对照换号。"""
        for name in keys:
            mapped = self.key(document.get(name)) if name in document else None
            if mapped is not None:
                document[name] = mapped
        for name in channels:
            if name in document:
                document[name] = self.channel(old_key, new_key, document[name])
        for name in platforms:
            if name in document:
                document[name] = self.platform(old_key, new_key, document[name])
        for name in bot_scopes:
            if name in document:
                document[name] = self.bot_scope(document[name])


def _key_platform(conversation_key: str) -> PlatformDescriptor | None:
    found = conversation_channel(conversation_key)
    return found[0] if found is not None else None


@contextmanager
def sqlite_rekey_transaction(path: Path, *, apply: bool) -> Iterator[sqlite3.Connection]:
    """一个库的改写在一个事务里做完；不 apply 就回滚，预览和真正执行走同一段代码。"""
    db = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT" if apply else "ROLLBACK")
    finally:
        db.close()


def sqlite_tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rekey_sqlite_table(
    db: sqlite3.Connection, table: str, rekey: ConversationRekey, *,
    unique_with: tuple[str, ...] | None = None,
    bot_scope_column: str = "",
    document_column: str = "",
    rewrite_document: Callable[[str, ConversationRekey, str, str], str] | None = None,
) -> RekeyCount:
    """把一张表里 scope 列落在旧会话键下的行改挂到新键。表不存在（老库）当没有。

    unique_with 是和 scope 一起构成唯一键的列：空元组表示 scope 自己唯一，None 表示行有自己的 id、不会撞。
    撞上目标已有的行时删掉目标那行，源会话胜出。bot_scope_column 是按账号分的列，按对照换号，
    判断撞没撞用换过的号。document_column 存着整条记录的 JSON，交给 rewrite_document 同步改。
    """
    if table not in sqlite_tables(db):
        return RekeyCount()
    total = RekeyCount()
    for old, new in rekey.items():
        if bot_scope_column:
            accounts = [row[0] for row in db.execute(f"SELECT DISTINCT {bot_scope_column} FROM {table} WHERE scope=?", (old,))]
            groups = [
                ([("scope", old), (bot_scope_column, account)], [("scope", new), (bot_scope_column, rekey.bot_scope(account))])
                for account in accounts
            ]
        else:
            groups = [([("scope", old)], [("scope", new)])]
        for source, target in groups:
            total += _rekey_sqlite_group(db, table, source, target, unique_with, document_column, rewrite_document, rekey, old, new)
    return total


def rekey_sqlite_documents(
    db: sqlite3.Connection, table: str, rekey: ConversationRekey, *,
    document_column: str,
    rewrite_document: Callable[[str, ConversationRekey, str, str], str],
) -> RekeyCount:
    """没有 scope 列、只在 JSON 里存着整条记录的表（历史版本、幂等结果）：按 JSON 里的 scope 认行，同步改写。"""
    if table not in sqlite_tables(db):
        return RekeyCount()
    moved = 0
    for old, new in rekey.items():
        rows = db.execute(
            f"SELECT rowid, {document_column} FROM {table} WHERE json_extract({document_column}, '$.scope')=?", (old,),
        ).fetchall()
        for rowid, raw in rows:
            db.execute(f"UPDATE {table} SET {document_column}=? WHERE rowid=?", (rewrite_document(raw, rekey, old, new), rowid))
            moved += 1
    return RekeyCount(moved)


def _rekey_sqlite_group(
    db: sqlite3.Connection, table: str,
    source: list[tuple[str, Any]], target: list[tuple[str, Any]],
    unique_with: tuple[str, ...] | None, document_column: str,
    rewrite_document: Callable[[str, ConversationRekey, str, str], str] | None,
    rekey: ConversationRekey, old: str, new: str,
) -> RekeyCount:
    source_where = " AND ".join(f"{column}=?" for column, _ in source)
    source_args = tuple(value for _, value in source)
    target_where = " AND ".join(f"{column}=?" for column, _ in target)
    target_args = tuple(value for _, value in target)
    replaced = 0
    if unique_with is not None:
        if unique_with:
            columns = ", ".join(unique_with)
            conflict = f"({columns}) IN (SELECT {columns} FROM {table} WHERE {source_where})"
        else:
            conflict = f"EXISTS (SELECT 1 FROM {table} WHERE {source_where})"
        replaced = db.execute(f"DELETE FROM {table} WHERE {target_where} AND {conflict}", target_args + source_args).rowcount
    if document_column and rewrite_document is not None:
        rows = db.execute(f"SELECT rowid, {document_column} FROM {table} WHERE {source_where}", source_args).fetchall()
        for rowid, raw in rows:
            db.execute(f"UPDATE {table} SET {document_column}=? WHERE rowid=?", (rewrite_document(raw, rekey, old, new), rowid))
    assignments = ", ".join(f"{column}=?" for column, _ in target)
    moved = db.execute(f"UPDATE {table} SET {assignments} WHERE {source_where}", target_args + source_args).rowcount
    return RekeyCount(moved, replaced)


@dataclass
class Rename:
    real: str
    old_stable: str
    new_stable: str
    old_ns: str
    new_ns: str
    dir_exists: bool
    target_exists: bool
    # 还有别的旧键重算到同一个新键。
    collides: bool = False

    @property
    def changed(self) -> bool:
        return self.old_stable != self.new_stable

    @property
    def blocked(self) -> bool:
        """两边都有长期记忆，或者和别的旧键撞到同一个新键：合并语义不明，整段会话原样留着让人判断。

        只跳过记忆目录、照样改路由的话，旧目录在路由表里再也查不到，以后哪次迁移都认领不回来。
        """
        return self.changed and ((self.dir_exists and self.target_exists) or self.collides)


@dataclass(frozen=True)
class Unresolved:
    """路由表里有、但按哪个候选都重算不出原样的会话键。"""

    real: str
    old_stable: str
    old_ns: str


@dataclass
class MigrationPlan:
    renames: list[Rename] = field(default_factory=list)
    unknown_conv_dirs: list[str] = field(default_factory=list)
    missing_source_dirs: list[str] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)

    @property
    def changed(self) -> list[Rename]:
        """真正要搬的：键变了，也没被挡下。"""
        return [r for r in self.renames if r.changed and not r.blocked]

    @property
    def blocked(self) -> list[Rename]:
        return [r for r in self.renames if r.blocked]


@dataclass
class MigrationOutcome:
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    hit_keys_rewritten: int = 0
    route_keys_changed: int = 0
    backup_dir: Path | None = None


def route_state_file(workspace: Path, platform: PlatformDescriptor) -> Path:
    return platform_state_path(platform, STATE_ROUTES, workspace)


def memory_root(workspace: Path) -> Path:
    return Path(workspace) / "data" / "memory"


def load_route_map(route_state: Path) -> dict[str, str]:
    """stable conversation_key -> 真实 QQ 会话键。真实值是重算摘要的唯一真相源。"""
    try:
        data = json.loads(Path(route_state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    real_map = data.get("real_conversation_keys")
    if not isinstance(real_map, dict):
        return {}
    return {str(k): str(v) for k, v in real_map.items() if str(k) and str(v)}


def plan_migration(
    *, workspace: Path, platform: PlatformDescriptor, restable: Callable[[str, str], str | None],
) -> MigrationPlan:
    """算出新旧对照。restable 收 (old_stable, real) 返回新的 stable key，认不出这个键的来历就返回 None。"""
    workspace = Path(workspace)
    root = memory_root(workspace)
    plan = MigrationPlan()
    known: set[str] = set()
    pairs: list[tuple[str, str, str]] = []

    for old_stable, real in load_route_map(route_state_file(workspace, platform)).items():
        new_stable = restable(old_stable, real)
        known.add(conversation_namespace(old_stable))
        # 空键也当认不出：它的目录名是空串，拼出来就是 data/memory 根目录本身。
        if not new_stable:
            plan.unresolved.append(Unresolved(real, old_stable, conversation_namespace(old_stable)))
            continue
        known.add(conversation_namespace(new_stable))
        pairs.append((old_stable, real, new_stable))

    targets = Counter(new_stable for old_stable, _real, new_stable in pairs if old_stable != new_stable)
    for old_stable, real, new_stable in pairs:
        old_ns = conversation_namespace(old_stable)
        new_ns = conversation_namespace(new_stable)
        dir_exists = (root / old_ns).is_dir()
        rename = Rename(
            real, old_stable, new_stable, old_ns, new_ns,
            dir_exists, old_ns != new_ns and (root / new_ns).exists(),
            collides=old_stable != new_stable and targets[new_stable] > 1,
        )
        plan.renames.append(rename)
        if rename.changed and not dir_exists:
            plan.missing_source_dirs.append(old_ns)

    if root.is_dir():
        # 路由表里查不到真实 key 的目录无法重算，只能报出来让人工判断。
        plan.unknown_conv_dirs = [
            child.name for child in sorted(root.glob(f"{CONVERSATION_PREFIX}*"))
            if child.is_dir() and child.name not in known
        ]
    return plan


def fresh_backup_dir(workspace: Path) -> Path:
    base = Path(workspace) / "data" / f"migration_backup_{time.strftime('%Y%m%d_%H%M%S')}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{suffix}")
        suffix += 1
    return candidate


def backup(*, workspace: Path, platform: PlatformDescriptor, backup_dir: Path, plan: MigrationPlan) -> None:
    """备份要被改名的记忆目录，以及会被原地重写的路由状态和命中缓存；各个按会话键存数据的库由它们的清单自己备份。"""
    workspace = Path(workspace)
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in (route_state_file(workspace, platform), hit_cache.hit_cache_path(workspace)):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    root = memory_root(workspace)
    for rename in plan.changed:
        src = root / rename.old_ns
        if src.is_dir():
            shutil.copytree(src, backup_dir / rename.old_ns)


def apply_migration(
    *, workspace: Path, platform: PlatformDescriptor, plan: MigrationPlan,
    make_backup: bool = True,
) -> MigrationOutcome:
    """同一个平台里换盐、换号：搬记忆目录和命中缓存，再把路由状态里原来的键改成新键。

    没有要改的东西就整个跳过，重复运行是幂等的空操作。只动 bot 和引擎这一侧的东西；
    会话注册表、定时任务这些按会话键存数据的库由调用方按同一份对照另外改写。
    """
    workspace = Path(workspace)
    outcome = move_memory(workspace=workspace, platform=platform, plan=plan, make_backup=make_backup)
    remap = {r.old_stable: r.new_stable for r in plan.changed}
    outcome.route_keys_changed = _rewrite_route_state(route_state_file(workspace, platform), remap)
    return outcome


def move_memory(
    *, workspace: Path, platform: PlatformDescriptor, plan: MigrationPlan,
    make_backup: bool = True,
) -> MigrationOutcome:
    """先备份，再把记忆目录搬到新键名下，命中缓存跟着改名；没有要改的东西就整个跳过。

    路由状态留给调用方：同平台迁移改写原来的键，跨平台绑定在目标平台补一条，所以备份带上 platform 的路由状态。
    附件目录不搬：消息里记的是带目录名的 file:// 路径，搬了历史里的图片全部指空；它们按保留期自己过期。
    """
    workspace = Path(workspace)
    outcome = MigrationOutcome()
    if not plan.changed:
        return outcome

    root = memory_root(workspace)
    moves = [rename for rename in plan.changed if rename.dir_exists]
    appeared = [rename.new_ns for rename in moves if (root / rename.new_ns).exists()]
    if appeared:
        # 目标目录是预演之后才冒出来的，说明 bot 还在跑；这时候 move 会把旧目录塞进新目录里面。
        raise RuntimeError(f"预演之后又出现了 {len(appeared)} 个目标记忆目录，确认 bot 已经停下再重跑")

    if make_backup:
        outcome.backup_dir = fresh_backup_dir(workspace)
        backup(workspace=workspace, platform=platform, backup_dir=outcome.backup_dir, plan=plan)

    ns_renames: dict[str, str] = {}
    for rename in moves:
        shutil.move(str(root / rename.old_ns), str(root / rename.new_ns))
        outcome.moved_dirs.append((rename.old_ns, rename.new_ns))
        ns_renames[rename.old_ns] = rename.new_ns

    outcome.hit_keys_rewritten = _rewrite_hit_cache(workspace, ns_renames)
    return outcome


def _rewrite_hit_cache(workspace: Path, ns_renames: dict[str, str]) -> int:
    """把命中缓存里被改名 namespace 的前缀换掉，复用侧车自己的锁与原子写。"""
    if not ns_renames:
        return 0
    path = hit_cache.hit_cache_path(workspace)
    if not path.exists():
        return 0
    with eventlog_file_lock(path):
        cache = hit_cache.read_hit_cache(workspace)
        rewritten: dict[str, str] = {}
        changed = 0
        for key, stamp in cache.items():
            ns_part, sep, rest = key.partition(HIT_KEY_SEP)
            if sep and ns_part in ns_renames:
                rewritten[f"{ns_renames[ns_part]}{HIT_KEY_SEP}{rest}"] = stamp
                changed += 1
            else:
                rewritten[key] = stamp
        if not changed:
            return 0
        _atomic_write_json(path, rewritten, indent=None)
        return changed


def _rewrite_route_state(route_state: Path, remap: dict[str, str]) -> int:
    """重写路由状态里的会话键。

    session_targets 里也存了一份 conversation_key，只改 real_conversation_keys 的话，
    在途回调会拿旧键去找目标，回复发不出去。
    """
    if not remap or not route_state.exists():
        return 0
    try:
        data = json.loads(route_state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0

    changed = 0
    real_map = data.get("real_conversation_keys")
    if isinstance(real_map, dict):
        changed += sum(1 for key in real_map if str(key) in remap)
        data["real_conversation_keys"] = {
            remap.get(str(k), str(k)): str(v) for k, v in real_map.items()
        }

    targets = data.get("session_targets")
    if isinstance(targets, dict):
        for target in targets.values():
            if not isinstance(target, dict):
                continue
            new_key = remap.get(str(target.get("conversation_key") or ""))
            if new_key:
                target["conversation_key"] = new_key
                changed += 1

    if not changed:
        return 0
    _atomic_write_json(route_state, data)
    return changed


def record_route(route_state: Path, stable: str, real: str, *, apply: bool) -> bool:
    """在路由状态里补一条稳定键到真实会话的对照；已经有同样一条就不动，返回要不要写。

    跨平台绑定时目标 bot 可能还没在那个会话里说过话：没有这一条，它投不出提醒，控制台也叫不出会话名。
    读不懂的文件、已经指向别的会话的键都直接抛，预览和执行走同一段检查。
    """
    stable, real = str(stable or "").strip(), str(real or "").strip()
    if not stable or not real:
        raise ValueError("a route needs both a stable and a real conversation key")
    path = Path(route_state)
    data: Any = {"version": 1, "real_conversation_keys": {}, "session_targets": {}}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"route state is not an object: {path}")
    real_map = data.setdefault("real_conversation_keys", {})
    if not isinstance(real_map, dict):
        raise ValueError(f"real_conversation_keys is not an object: {path}")
    # 空值读的时候本来就被丢掉，等于没有这一条。
    current = str(real_map.get(stable) or "")
    if current == real:
        return False
    if current:
        raise ValueError(f"{stable} already points at another conversation in {path}")
    if apply:
        real_map[stable] = real
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, data)
    return True


def _atomic_write_json(path: Path, payload: object, indent: int | None = 2) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, path)
