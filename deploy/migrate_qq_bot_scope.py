"""把一个 bot QQ 号名下的会话与长期记忆整体搬到另一个号名下：离线、可 dry-run、自动备份。

会话键摘要按 bot 账号分作用域，换号即换命名空间。存量数据是在没有作用域时攒的，
用 --from "" --to <当前号> 把它们一次性划给当前账号；别的账号名下的会话原样留着。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# 摘要算法与账号作用域必须与运行时同一份，所以直接用共享层的模块。
from adapters._shared import conversation_hash as _conversation_hash  # noqa: E402
from adapters._shared.active_account import ActiveAccountStore, account_candidates, normalize_account  # noqa: E402
from clonoth_runtime import offline_workspace  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from supervisor.conversation_stores import LiveRekey, format_store_counts, rekey_conversation_stores  # noqa: E402
from workspace import workspace_instance  # noqa: E402

rekey = _conversation_hash.rekey
key_origin = _conversation_hash.key_origin

from engine.conversation_migration import (  # noqa: E402
    ConversationRekey, RekeyCount, Rename, Unresolved, apply_migration, load_route_map, plan_migration,
    route_state_file,
)


class ScopeMismatch(RuntimeError):
    """--from 与盘上实际的会话键对不上，继续搬只会把数据搬进错误的命名空间。"""


class AmbiguousSource(ScopeMismatch):
    """目标以外还有几个账号名下有会话，不点名就不知道该搬哪一批。"""

    def __init__(self, scopes: Mapping[str, int]) -> None:
        self.scopes = dict(scopes)
        names = "、".join(scope_label(scope) for scope in self.scopes)
        super().__init__(f"盘上的会话键分属几个账号（{names}），用 --from 指定搬哪一个")


@dataclass
class ScopeReport:
    workspace: Path
    apply: bool
    source_scope: str
    target_scope: str
    renames: list[Rename] = field(default_factory=list)
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    moved_attachment_dirs: list[tuple[str, str]] = field(default_factory=list)
    blocked: list[Rename] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    # 源和目标以外的账号名下各有几个会话：不归这次搬，原样留着。
    other_scopes: dict[str, int] = field(default_factory=dict)
    missing_source_dirs: list[str] = field(default_factory=list)
    unknown_conv_dirs: list[str] = field(default_factory=list)
    hit_keys_rewritten: int = 0
    route_keys_changed: int = 0
    # 按会话键存数据的各个库：dry-run 是预览，apply 是实际改写的行数。
    stores: dict[str, RekeyCount] = field(default_factory=dict)
    scope_written: bool = False
    backup_dir: Path | None = None

    @property
    def changed_renames(self) -> list[Rename]:
        return [r for r in self.renames if r.changed]

    @property
    def sessions_changed(self) -> int:
        return self.stores.get("sessions", RekeyCount()).moved


def read_secret(workspace: Path) -> str:
    """bot 正在用的摘要密钥：data/config.yaml 或环境变量里配的优先，否则钉住的文件。无盐部署返回空串。"""
    return _conversation_hash.current_secret(QQ_NAPCAT, Path(workspace)).value


def recorded_scope(workspace: Path) -> str:
    """bot 上次记下的当前账号。supervisor 的归属查询读的也是这一份。"""
    return ActiveAccountStore(QQ_NAPCAT, Path(workspace)).load()


def scope_label(scope: str) -> str:
    return scope or "<无作用域>"


def route_key_scopes(workspace: Path, secret: str, candidates: Iterable[str]) -> dict[str, str | None]:
    """路由表里每个会话键是在哪个账号下算出来的，认不出的记 None。"""
    tried = tuple(candidates)
    scopes: dict[str, str | None] = {}
    for old_stable, real in load_route_map(route_state_file(workspace, QQ_NAPCAT)).items():
        origin = key_origin(old_stable, real, candidate_secrets=(secret,), candidate_scopes=tried)
        scopes[old_stable] = origin.bot_scope if origin else None
    return scopes


def scope_counts(key_scopes: Mapping[str, str | None]) -> dict[str, int]:
    return dict(Counter(scope for scope in key_scopes.values() if scope is not None))


def pick_source_scope(key_scopes: Mapping[str, str | None], *, target: str, recorded: str) -> str | None:
    """不点名时该搬哪个账号名下的数据；认不出返回 None，目标以外还有几个账号时抛 AmbiguousSource。

    记录的当前账号未必就是数据的归属：加账号隔离之前攒的数据没有作用域，而 bot 一登录就把当前号记下来了。
    换号之后旧号的键还留在路由表里，新号一说话又添上新号的键，所以只看目标以外的账号。
    """
    if not key_scopes:
        return recorded
    counts = scope_counts(key_scopes)
    others = {scope: count for scope, count in counts.items() if scope != target}
    if len(others) > 1:
        raise AmbiguousSource(others)
    if others:
        return next(iter(others))
    # 认得出的都已经在目标名下：没有要搬的。
    return target if counts else None


def detect_source_scope(workspace: Path, secret: str, *, target_scope: str) -> str | None:
    """按盘上的会话键认出这次该搬哪个账号名下的数据，规则见 pick_source_scope。"""
    workspace = Path(workspace)
    target = normalize_account(target_scope, QQ_NAPCAT)
    key_scopes = route_key_scopes(workspace, secret, account_candidates(QQ_NAPCAT, workspace, extra=(target,)))
    return pick_source_scope(key_scopes, target=target, recorded=recorded_scope(workspace))


def run_scope_migration(
    *, workspace: Path, target_scope: str, source_scope: str | None = None,
    apply: bool = False, adopt: bool = True, live_sessions: LiveRekey | None = None,
) -> ScopeReport:
    """只搬 source 名下的会话键，别的账号的原样留着。

    live_sessions 给在线迁移用：supervisor 自己持有的会话注册表，直接改内存再落盘。
    """
    workspace = Path(workspace)
    secret = read_secret(workspace)
    target = normalize_account(target_scope, QQ_NAPCAT)
    if not target:
        raise ValueError("--to 必须是目标 bot 的 QQ 号（纯数字）")
    named = None if source_scope is None else normalize_account(source_scope, QQ_NAPCAT)
    if named == "" and str(source_scope).strip():
        # 写错的号规范化出来是空串，会被当成「无作用域」去搬存量数据。
        raise ValueError("--from 必须是源 bot 的 QQ 号（纯数字）；存量无作用域的数据传空串")
    candidates = account_candidates(QQ_NAPCAT, workspace, extra=(target,) if named is None else (named, target))
    key_scopes = route_key_scopes(workspace, secret, candidates)

    if named is None:
        source = pick_source_scope(key_scopes, target=target, recorded=recorded_scope(workspace))
        if source is None:
            raise ScopeMismatch("认不出盘上这批会话键属于哪个账号，用 --from 显式指定")
    elif key_scopes and named not in key_scopes.values():
        # 认错源等于认错了「现在这批数据属于谁」：什么都不会搬，人却以为搬完了。
        raise ScopeMismatch(f"没有一个会话键是按 --from={scope_label(named)} 算出来的，确认源账号后重跑")
    else:
        source = named

    report = ScopeReport(
        workspace=workspace, apply=apply, source_scope=source, target_scope=target,
    )
    report.other_scopes = {
        scope: count for scope, count in scope_counts(key_scopes).items() if scope not in (source, target)
    }

    def restable(old_stable: str, real: str) -> str | None:
        scope = key_scopes.get(old_stable)
        if scope is None:
            return None
        return rekey(old_stable, real, secret, bot_scope=target) if scope == source else old_stable

    plan = plan_migration(workspace=workspace, platform=QQ_NAPCAT, restable=restable)
    report.renames = plan.renames
    report.blocked = plan.blocked
    report.unresolved = plan.unresolved
    report.missing_source_dirs = plan.missing_source_dirs
    report.unknown_conv_dirs = plan.unknown_conv_dirs

    conversation_rekey = ConversationRekey(
        {rename.old_stable: rename.new_stable for rename in plan.changed},
        # 无作用域时功能记录的账号记的是工作区实例号，不换成新号，新号上的人就看不到自己的提醒和资料。
        bot_scopes={source or workspace_instance(workspace): target},
    )
    live = {"sessions": live_sessions} if live_sessions is not None else None
    # 先按同一份对照预览：哪个库读不懂或有冲突，在动任何东西之前就停下。
    report.stores = rekey_conversation_stores(workspace, conversation_rekey, apply=False, live=live)

    if not apply:
        return report

    # 会话历史、长期记忆和各项功能数据共用一个会话键，一起搬才不会一半跟着走一半留在旧号。
    outcome = apply_migration(
        workspace=workspace, platform=QQ_NAPCAT, plan=plan, move_attachments=True,
    )
    report.moved_dirs = outcome.moved_dirs
    report.moved_attachment_dirs = outcome.moved_attachment_dirs
    report.hit_keys_rewritten = outcome.hit_keys_rewritten
    report.route_keys_changed = outcome.route_keys_changed
    report.backup_dir = outcome.backup_dir
    report.stores = rekey_conversation_stores(
        workspace, conversation_rekey, apply=True, backup_dir=outcome.backup_dir, live=live,
    )

    # 作用域写在最后：前面抛异常就不会到这里，bot 仍按旧作用域启动，脚本可重跑。
    if adopt:
        report.scope_written = ActiveAccountStore(QQ_NAPCAT, workspace).save(target)
    return report


def _print_report(report: ScopeReport) -> None:
    mode = "APPLY" if report.apply else "DRY-RUN"
    print(f"[{mode}] workspace: {report.workspace}")
    print(f"源账号: {scope_label(report.source_scope)}  ->  目标账号: {report.target_scope}")
    changed = report.changed_renames
    print(f"待搬迁会话数: {len(changed)}")
    for rename in changed:
        state = "moved" if (rename.old_ns, rename.new_ns) in report.moved_dirs else (
            "blocked" if rename.blocked else (
                "no-memory-dir" if not rename.dir_exists else "planned"))
        print(f"  {rename.real}: {rename.old_ns} -> {rename.new_ns} [{state}]")
    if report.unknown_conv_dirs:
        print("路由状态里查不到真实会话、无法重算的记忆目录（保持原样）:")
        for name in report.unknown_conv_dirs:
            print(f"  {name}")
    if report.blocked:
        print("两边都有长期记忆，整段会话原样留着（在控制台处理掉一边再重跑）:")
        for rename in report.blocked:
            print(f"  {rename.real}: {rename.old_ns} -> {rename.new_ns}")
    if report.unresolved:
        print("认不出在哪个账号下算出来的会话键，原样留着（出自别的旧号的话，用 --from 点名那个号单独搬）:")
        for item in report.unresolved:
            print(f"  {item.real}: {item.old_ns}")
    for scope, count in report.other_scopes.items():
        print(f"账号 {scope_label(scope)} 名下另有 {count} 个会话，不在这次搬迁里。")
    print(f"{'已改写' if report.apply else '将改写'}的按会话键存的数据: {format_store_counts(report.stores)}")
    if report.apply:
        print(f"备份目录: {report.backup_dir}")
        print(f"搬迁记忆目录: {len(report.moved_dirs)}；"
              f"附件目录: {len(report.moved_attachment_dirs)}；"
              f"命中缓存 key: {report.hit_keys_rewritten}；"
              f"路由状态 key: {report.route_keys_changed}；"
              f"当前账号已写入: {report.scope_written}")
        print("重启 bot 让它读到新的作用域，否则进程内还留着按旧账号算出的键。")
    else:
        print("这是 dry-run，未改动任何文件。加 --apply 真正执行。")
    print("在 bot 与 supervisor 都停止的状态下运行本脚本。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default=None,
        help="Clonoth 工作区根目录；不给就和 bot 一样取 CLONOTH_WORKSPACE / ONEBOT_WORKSPACE_ROOT，可以写在 .env 里，都没设是仓库根。",
    )
    parser.add_argument("--to", required=True, help="目标 bot 的 QQ 号。")
    parser.add_argument(
        "--from", dest="source", default=None,
        help='源 bot 的 QQ 号；存量无作用域数据传空串 ""。不给则按盘上的会话键自动认。',
    )
    parser.add_argument("--apply", action="store_true", help="真正执行；不加则只 dry-run。")
    parser.add_argument(
        "--no-adopt", action="store_true",
        help="搬完不把目标号记成当前账号（迁给一个还没登录的号时用）。",
    )
    args = parser.parse_args(argv)
    workspace = offline_workspace(args.workspace)

    try:
        report = run_scope_migration(
            workspace=workspace, target_scope=args.to, source_scope=args.source,
            apply=args.apply, adopt=not args.no_adopt,
        )
    except ScopeMismatch as error:
        print(f"源账号校验失败，未改动任何文件: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"迁移中止（如已部分改动请从备份恢复）: {error}", file=sys.stderr)
        return 1
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
