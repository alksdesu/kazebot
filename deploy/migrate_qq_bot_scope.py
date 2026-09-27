"""把会话与长期记忆整体搬到另一个 bot QQ 号名下：离线、可 dry-run、自动备份。

会话键摘要按 bot 账号分作用域，换号即换命名空间。存量数据是在没有作用域时攒的，
用 --from "" --to <当前号> 把它们一次性划给当前账号。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# 摘要算法与账号作用域必须与运行时同一份，所以直接用共享层的模块。
from adapters._shared import conversation_hash as _conversation_hash  # noqa: E402
from adapters._shared.active_account import ActiveAccountStore, normalize_account  # noqa: E402
from clonoth_runtime import offline_workspace  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from supervisor.conversation_stores import LiveRekey, format_store_counts, rekey_conversation_stores  # noqa: E402
from workspace import workspace_instance  # noqa: E402

rekey = _conversation_hash.rekey

from engine.conversation_migration import (  # noqa: E402
    ConversationRekey, RekeyCount, Rename, apply_migration, load_route_map, plan_migration, route_state_file,
)


class ScopeMismatch(RuntimeError):
    """--from 与盘上实际的会话键对不上，继续搬只会把数据搬进错误的命名空间。"""


@dataclass
class ScopeReport:
    workspace: Path
    apply: bool
    source_scope: str
    target_scope: str
    renames: list[Rename] = field(default_factory=list)
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    moved_attachment_dirs: list[tuple[str, str]] = field(default_factory=list)
    skipped_target_exists: list[tuple[str, str]] = field(default_factory=list)
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


def scope_matches(workspace: Path, secret: str, scope: str) -> bool:
    """盘上的会话键是不是按这个账号算出来的。路由表为空时无从判断，一律算匹配。"""
    return not [
        old_stable
        for old_stable, real in load_route_map(route_state_file(workspace, QQ_NAPCAT)).items()
        if old_stable != rekey(old_stable, real, secret, bot_scope=scope)
    ]


def detect_source_scope(workspace: Path, secret: str) -> str | None:
    """认出盘上这批数据实际属于哪个账号，认不出返回 None。

    记录的当前账号未必就是数据的归属：加账号隔离之前攒的数据没有作用域，而 bot
    一登录就把当前号记下来了，两者天然对不上。
    """
    recorded = recorded_scope(workspace)
    seen: list[str] = []
    for candidate in (recorded, "", *_known_accounts(workspace)):
        if candidate in seen:
            continue
        seen.append(candidate)
        if scope_matches(workspace, secret, candidate):
            return candidate
    return None


def _known_accounts(workspace: Path) -> list[str]:
    """NapCat 留下的账号配置文件名里带 QQ 号，用作探测候选。"""
    config_dir = Path(workspace) / "napcat" / "config"
    try:
        names = [path.stem.rsplit("_", 1)[-1] for path in config_dir.glob("onebot11_*.json")]
    except OSError:
        return []
    return [name for name in names if name.isdigit()]


def verify_source_scope(workspace: Path, secret: str, source_scope: str) -> None:
    """用 --from 重算一遍，对不上就中止。

    搬迁本身只认盘上的 key，不需要 --from；但认错源等于认错了「现在这批数据属于谁」，
    搬完既找不回旧号也进不了新号。
    """
    mismatched = [
        old_stable
        for old_stable, real in load_route_map(route_state_file(workspace, QQ_NAPCAT)).items()
        if old_stable != rekey(old_stable, real, secret, bot_scope=source_scope)
    ]
    if mismatched:
        raise ScopeMismatch(
            f"{len(mismatched)} 个会话键不是按 --from={source_scope or '<无作用域>'} 算出来的，"
            "确认源账号后重跑",
        )


def run_scope_migration(
    *, workspace: Path, target_scope: str, source_scope: str | None = None,
    apply: bool = False, adopt: bool = True, live_sessions: LiveRekey | None = None,
) -> ScopeReport:
    """live_sessions 给在线迁移用：supervisor 自己持有的会话注册表，直接改内存再落盘。"""
    workspace = Path(workspace)
    secret = read_secret(workspace)
    if source_scope is None:
        # 不指定源就自己认：记录的当前账号未必是这批数据的归属。
        detected = detect_source_scope(workspace, secret)
        if detected is None:
            raise ScopeMismatch("认不出盘上这批会话键属于哪个账号，用 --from 显式指定")
        source = detected
    else:
        source = normalize_account(source_scope, QQ_NAPCAT)
    target = normalize_account(target_scope, QQ_NAPCAT)
    if not target:
        raise ValueError("--to 必须是目标 bot 的 QQ 号（纯数字）")

    verify_source_scope(workspace, secret, source)

    report = ScopeReport(
        workspace=workspace, apply=apply, source_scope=source, target_scope=target,
    )
    plan = plan_migration(
        workspace=workspace,
        platform=QQ_NAPCAT,
        restable=lambda old_stable, real: rekey(old_stable, real, secret, bot_scope=target),
    )
    report.renames = plan.renames
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
    report.skipped_target_exists = outcome.skipped_target_exists
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
    src = report.source_scope or "<无作用域>"
    print(f"[{mode}] workspace: {report.workspace}")
    print(f"源账号: {src}  ->  目标账号: {report.target_scope}")
    changed = report.changed_renames
    print(f"待搬迁会话数: {len(changed)}")
    for rename in changed:
        state = "moved" if (rename.old_ns, rename.new_ns) in report.moved_dirs else (
            "target-exists" if rename.target_exists else (
                "no-memory-dir" if not rename.dir_exists else "planned"))
        print(f"  {rename.real}: {rename.old_ns} -> {rename.new_ns} [{state}]")
    if report.unknown_conv_dirs:
        print("路由状态里查不到真实会话、无法重算的记忆目录（保持原样）:")
        for name in report.unknown_conv_dirs:
            print(f"  {name}")
    if report.skipped_target_exists:
        print("目标目录已存在、跳过搬迁:")
        for old_ns, new_ns in report.skipped_target_exists:
            print(f"  {old_ns} -> {new_ns}")
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
