"""按新的会话键摘要搬迁记忆目录、命中缓存与路由状态。

换盐（deploy/migrate_qq_conversation_hash.py）和换 bot 账号共用这一套：摘要怎么算由
调用方给的 restable 决定，本模块只负责「算出新旧对照，然后一样不落地搬过去」。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Callable

import engine.memory_hit_cache as hit_cache
from engine.eventlog_rotation import eventlog_file_lock

HIT_KEY_SEP = "/"
# _stable_conversation_key 的三种 prefix 把 ":" 换成 "_" 就是附件目录名。
ATTACHMENT_DIR_PREFIXES = ("qq_group_", "qq_private_", "qq_unknown_")


def namespace_dir(stable_key: str) -> str:
    """stable conversation_key 对应的记忆目录名，与 knowledge_inject 同一个算法。"""
    return "conv_" + sha256(stable_key.encode("utf-8")).hexdigest()[:24]


@dataclass
class Rename:
    real: str
    old_stable: str
    new_stable: str
    old_ns: str
    new_ns: str
    dir_exists: bool
    target_exists: bool

    @property
    def changed(self) -> bool:
        return self.old_stable != self.new_stable


@dataclass
class MigrationPlan:
    renames: list[Rename] = field(default_factory=list)
    unknown_conv_dirs: list[str] = field(default_factory=list)
    missing_source_dirs: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[Rename]:
        return [r for r in self.renames if r.changed]


@dataclass
class MigrationOutcome:
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    skipped_target_exists: list[tuple[str, str]] = field(default_factory=list)
    moved_attachment_dirs: list[tuple[str, str]] = field(default_factory=list)
    hit_keys_rewritten: int = 0
    route_keys_changed: int = 0
    sessions_changed: int = 0
    backup_dir: Path | None = None


def route_state_file(workspace: Path) -> Path:
    return Path(workspace) / "data" / "onebot_plugin_state.json"


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


def plan_migration(*, workspace: Path, restable: Callable[[str, str], str]) -> MigrationPlan:
    """算出新旧对照。restable 收 (old_stable, real) 返回新的 stable key。"""
    workspace = Path(workspace)
    root = memory_root(workspace)
    plan = MigrationPlan()
    known: set[str] = set()

    for old_stable, real in load_route_map(route_state_file(workspace)).items():
        new_stable = restable(old_stable, real)
        old_ns = namespace_dir(old_stable)
        new_ns = namespace_dir(new_stable)
        known.add(old_ns)
        known.add(new_ns)
        dir_exists = (root / old_ns).is_dir()
        rename = Rename(
            real, old_stable, new_stable, old_ns, new_ns,
            dir_exists, old_ns != new_ns and (root / new_ns).exists(),
        )
        plan.renames.append(rename)
        if rename.changed and not dir_exists:
            plan.missing_source_dirs.append(old_ns)

    if root.is_dir():
        # 路由表里查不到真实 key 的目录无法重算，只能报出来让人工判断。
        plan.unknown_conv_dirs = [
            child.name for child in sorted(root.glob("conv_*"))
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


def backup(*, workspace: Path, backup_dir: Path, plan: MigrationPlan) -> None:
    """备份要被改名的记忆目录，以及会被原地重写的几个状态文件。"""
    workspace = Path(workspace)
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in (route_state_file(workspace), hit_cache.hit_cache_path(workspace),
                 workspace / "data" / "sessions.json"):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    root = memory_root(workspace)
    for rename in plan.changed:
        src = root / rename.old_ns
        if src.is_dir():
            shutil.copytree(src, backup_dir / rename.old_ns)


def apply_migration(
    *, workspace: Path, plan: MigrationPlan,
    move_attachments: bool = False, rewrite_sessions: bool = False,
    make_backup: bool = True,
) -> MigrationOutcome:
    """执行搬迁。没有要改的东西就整个跳过，重复运行是幂等的空操作。"""
    workspace = Path(workspace)
    outcome = MigrationOutcome()
    if not plan.changed:
        return outcome

    if make_backup:
        outcome.backup_dir = fresh_backup_dir(workspace)
        backup(workspace=workspace, backup_dir=outcome.backup_dir, plan=plan)

    root = memory_root(workspace)
    ns_renames: dict[str, str] = {}
    for rename in plan.changed:
        if not rename.dir_exists:
            continue
        if rename.target_exists:
            # 两个源会话撞进同一个目标：合并语义不明确，宁可原样留着让人来判。
            outcome.skipped_target_exists.append((rename.old_ns, rename.new_ns))
            continue
        shutil.move(str(root / rename.old_ns), str(root / rename.new_ns))
        outcome.moved_dirs.append((rename.old_ns, rename.new_ns))
        ns_renames[rename.old_ns] = rename.new_ns

    outcome.hit_keys_rewritten = _rewrite_hit_cache(workspace, ns_renames)
    remap = {r.old_stable: r.new_stable for r in plan.changed}
    outcome.route_keys_changed = _rewrite_route_state(route_state_file(workspace), remap)
    if move_attachments:
        outcome.moved_attachment_dirs = _move_attachment_dirs(workspace, remap)
    if rewrite_sessions:
        outcome.sessions_changed = _rewrite_sessions(workspace, remap)
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


def _rewrite_sessions(workspace: Path, remap: dict[str, str]) -> int:
    """把 supervisor 的会话注册表指向新会话键，让对话历史跟着一起搬。"""
    path = Path(workspace) / "data" / "sessions.json"
    if not remap or not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0

    rows = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(rows, dict):
        return 0
    changed = 0
    for row in rows.values():
        if not isinstance(row, dict):
            continue
        new_key = remap.get(str(row.get("conversation_key") or ""))
        if new_key:
            row["conversation_key"] = new_key
            changed += 1
    if not changed:
        return 0
    _atomic_write_json(path, data)
    return changed


def _move_attachment_dirs(workspace: Path, remap: dict[str, str]) -> list[tuple[str, str]]:
    """会话附件目录按 stable key 命名，不搬的话历史里的图片路径全部指空。"""
    root = Path(workspace) / "data" / "attachments"
    if not root.is_dir():
        return []
    moved: list[tuple[str, str]] = []
    for old_key, new_key in remap.items():
        old_name = old_key.replace(":", "_")
        new_name = new_key.replace(":", "_")
        if not old_name.startswith(ATTACHMENT_DIR_PREFIXES):
            continue
        src, dst = root / old_name, root / new_name
        if not src.is_dir() or dst.exists():
            continue
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            continue
        moved.append((old_name, new_name))
    return moved


def _atomic_write_json(path: Path, payload: object, indent: int | None = 2) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, path)
