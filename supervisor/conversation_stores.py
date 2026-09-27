"""按会话键存数据的各个库：换号、加盐、跨平台绑定时按同一份对照一起改写、一起备份。

新加按会话键存数据的库要登记进 CONVERSATION_STORES，否则迁移会把它的数据留在旧会话名下。
"""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from engine.conversation_migration import ConversationRekey, RekeyConflict, RekeyCount
from engine.materials.storage import materials_index_path, rekey_materials

from .community.service import community_path, rekey_community
from .execution.store import plans_path, rekey_plans
from .reminders.service import reminders_path, rekey_reminders
from .scheduler import rekey_schedules, schedules_path
from .session_store import rekey_session_registry, session_registry_path

LiveRekey = Callable[..., RekeyCount]


class ConversationStoreError(RuntimeError):
    """某个库读不懂或写不进：整次迁移停下，报出是哪个库、哪个文件。"""


@dataclass(frozen=True)
class ConversationStore:
    name: str
    label: str
    path: Callable[[Path], Path]
    rekey: Callable[..., RekeyCount]
    sqlite: bool = False
    # 短期上下文：加盐迁移按既定做法不搬，那几个会话下次说话各自新开。
    history: bool = False


CONVERSATION_STORES: tuple[ConversationStore, ...] = (
    ConversationStore("sessions", "会话注册表", session_registry_path, rekey_session_registry, history=True),
    ConversationStore("schedules", "定时任务", schedules_path, rekey_schedules),
    ConversationStore("reminders", "提醒", reminders_path, rekey_reminders, sqlite=True),
    ConversationStore("community", "群协作", community_path, rekey_community, sqlite=True),
    ConversationStore("materials", "资料", materials_index_path, rekey_materials, sqlite=True),
    ConversationStore("execution", "执行计划", plans_path, rekey_plans, sqlite=True),
)


def conversation_stores(*, history: bool = True) -> tuple[ConversationStore, ...]:
    return tuple(store for store in CONVERSATION_STORES if history or not store.history)


def rekey_conversation_stores(
    workspace: Path,
    rekey: ConversationRekey,
    *,
    apply: bool,
    history: bool = True,
    backup_dir: Path | None = None,
    live: Mapping[str, LiveRekey] | None = None,
) -> dict[str, RekeyCount]:
    """按对照改写各个库，给出每个库改写的行数。不 apply 只算不写，和真正执行走同一段代码。

    apply 时每个库改写前先备份进 backup_dir。live 给出正在运行的进程里自己持有的库（在线迁移时的会话
    注册表），按名字顶替改文件的做法，收 (rekey, apply=...)。撞上 RekeyConflict 原样抛，其余读写错误包成
    ConversationStoreError：调用方先预览一遍，这类问题在动任何东西之前就会暴露。
    """
    workspace = Path(workspace)
    overrides = dict(live or {})
    results: dict[str, RekeyCount] = {}
    for store in conversation_stores(history=history):
        path = store.path(workspace)
        override = overrides.get(store.name)
        try:
            if apply and rekey and backup_dir is not None and path.exists():
                _backup(store, path, workspace, Path(backup_dir))
            if override is not None:
                results[store.name] = override(rekey, apply=apply)
            else:
                results[store.name] = store.rekey(path, rekey, apply=apply)
        except RekeyConflict:
            raise
        except Exception as exc:
            raise ConversationStoreError(f"{store.label}（{path}）改写失败：{exc}") from exc
    return results


def store_counts_payload(results: Mapping[str, RekeyCount]) -> dict[str, dict[str, Any]]:
    labels = {store.name: store.label for store in CONVERSATION_STORES}
    return {
        name: {"label": labels.get(name, name), "moved": count.moved, "replaced": count.replaced}
        for name, count in results.items()
    }


def format_store_counts(results: Mapping[str, RekeyCount]) -> str:
    """给人看的一行：「会话注册表 3 行（顶掉目标 1 行）；定时任务 2 行」，一行都没有是「无」。"""
    labels = {store.name: store.label for store in CONVERSATION_STORES}
    parts = [
        f"{labels.get(name, name)} {count.moved} 行" + (f"（顶掉目标 {count.replaced} 行）" if count.replaced else "")
        for name, count in results.items()
        if count
    ]
    return "；".join(parts) or "无"


def _backup(store: ConversationStore, path: Path, workspace: Path, backup_dir: Path) -> None:
    try:
        relative = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        relative = Path(path.name)
    target = backup_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if store.sqlite:
        # WAL 模式下直接拷文件会漏掉还没合并进主文件的提交。
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
    else:
        shutil.copy2(path, target)
