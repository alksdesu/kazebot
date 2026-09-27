"""把存量部署的会话键摘要从无盐迁移到加盐：离线、可 dry-run、失败可中止。

记忆目录名、.hit_cache.json 前缀、路由状态键按 real_conversation_keys 逐条重算，每个键保留它原来的账号作用域；
定时任务、提醒这些按会话键存的功能数据跟着换键；supervisor 的 sessions.json 不动，那几个会话下次说话各自新开。
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 摘要算法必须与运行时同一份，所以直接用共享层的模块。
from adapters._shared import conversation_hash as _conversation_hash  # noqa: E402
from adapters._shared.active_account import account_candidates, normalize_account  # noqa: E402
from clonoth_runtime import offline_workspace  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from supervisor.conversation_stores import format_store_counts, rekey_conversation_stores  # noqa: E402

rekey = _conversation_hash.rekey
key_origin = _conversation_hash.key_origin
_SECRET_RE = _conversation_hash._SECRET_RE

from engine.conversation_migration import (  # noqa: E402
    ConversationRekey, RekeyCount, Rename, Unresolved, apply_migration, plan_migration,
)

_SECRET_BYTES = 32


class SecretConflict(RuntimeError):
    """--secret 和 bot 用的显式密钥不是同一把，按它迁完 bot 也不会用。"""


@dataclass
class MigrationReport:
    workspace: Path
    apply: bool
    target_secret: str = ""
    already_salted: bool = False
    # bot 用的是 data/config.yaml 或环境变量里配的密钥：迁到它，不写密钥文件。
    explicit_secret: bool = False
    renames: list[Rename] = field(default_factory=list)
    # 认键时试过的账号作用域：认不出的键多半出自不在这里的旧号。
    accounts: tuple[str, ...] = ()
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    blocked: list[Rename] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    missing_source_dirs: list[str] = field(default_factory=list)
    unknown_conv_dirs: list[str] = field(default_factory=list)
    route_keys_changed: int = 0
    hit_keys_rewritten: int = 0
    # 按会话键存的功能数据：dry-run 是预览，apply 是实际改写的行数。
    stores: dict[str, RekeyCount] = field(default_factory=dict)
    secret_written: bool = False
    backup_dir: Path | None = None

    @property
    def changed_renames(self) -> list[Rename]:
        return [r for r in self.renames if r.changed]


def _resolve_target_secret(
    current: _conversation_hash.CurrentSecret, override: str | None,
) -> tuple[str, bool]:
    """返回 (target_secret, already_salted)。bot 用的显式密钥 > override > 文件里的已有密钥 > 新生成。"""
    if current.source == _conversation_hash.SECRET_EXPLICIT:
        if override is not None and override != current.value:
            raise SecretConflict("bot 用的是 data/config.yaml 或环境变量里配的密钥，--secret 不会生效；要换密钥先改那边")
        return current.value, True
    already = bool(current.value)
    if override is not None:
        return override, already
    if already:
        # 复用已钉住的密钥，让重复运行成为幂等的空操作。
        return current.value, already
    return secrets.token_hex(_SECRET_BYTES), already


def _write_secret(secret_file: Path, secret: str) -> None:
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = secret_file.with_name(f"{secret_file.name}.{os.getpid()}.tmp")
    tmp.write_text(secret, encoding="utf-8")
    os.replace(tmp, secret_file)
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass


def run_migration(
    *, workspace: Path, apply: bool, secret: str | None = None, accounts: Iterable[str] = (),
) -> MigrationReport:
    """accounts 是另外要试的 bot 账号：换过号、旧号已经不在记录里的时候由人补上。"""
    workspace = Path(workspace)
    secret_file = _conversation_hash.secret_file(QQ_NAPCAT, workspace)
    current = _conversation_hash.current_secret(QQ_NAPCAT, workspace)

    report = MigrationReport(workspace=workspace, apply=apply)
    report.explicit_secret = current.source == _conversation_hash.SECRET_EXPLICIT
    report.target_secret, report.already_salted = _resolve_target_secret(current, secret)
    report.accounts = account_candidates(QQ_NAPCAT, workspace, extra=accounts)
    # 盘上的键可能出自 bot 现在这把、加盐前的无盐，或上次跑到一半已经换上的目标密钥。
    tried_secrets = (current.value, "", report.target_secret)

    def restable(old_stable: str, real: str) -> str | None:
        origin = key_origin(old_stable, real, candidate_secrets=tried_secrets, candidate_scopes=report.accounts)
        if origin is None:
            return None
        return rekey(old_stable, real, report.target_secret, bot_scope=origin.bot_scope)

    plan = plan_migration(workspace=workspace, platform=QQ_NAPCAT, restable=restable)
    report.renames = plan.renames
    report.blocked = plan.blocked
    report.unresolved = plan.unresolved
    report.missing_source_dirs = plan.missing_source_dirs
    report.unknown_conv_dirs = plan.unknown_conv_dirs

    conversation_rekey = ConversationRekey({rename.old_stable: rename.new_stable for rename in plan.changed})
    # 先按同一份对照预览：哪个库读不懂或有冲突，在动任何东西之前就停下。
    report.stores = rekey_conversation_stores(workspace, conversation_rekey, apply=False, history=False)

    if not apply:
        return report

    outcome = apply_migration(workspace=workspace, platform=QQ_NAPCAT, plan=plan)
    report.moved_dirs = outcome.moved_dirs
    report.hit_keys_rewritten = outcome.hit_keys_rewritten
    report.route_keys_changed = outcome.route_keys_changed
    report.backup_dir = outcome.backup_dir
    # 定时任务、提醒到点要按新键找到会话，不跟着换就投不出去；短期上下文按既定做法不搬。
    report.stores = rekey_conversation_stores(
        workspace, conversation_rekey, apply=True, history=False, backup_dir=outcome.backup_dir,
    )

    if report.explicit_secret:
        # 显式配置的密钥由 bot 直接读，落一份文件也不会被用到。
        return report
    # 密钥写在最后：前面任何一步抛异常都不会到这里，摘要模式保持原样，脚本可重跑。
    current = ""
    if secret_file.exists():
        try:
            current = secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
    if current != report.target_secret:
        _write_secret(secret_file, report.target_secret)
        report.secret_written = True
    return report


def _print_report(report: MigrationReport) -> None:
    mode = "APPLY" if report.apply else "DRY-RUN"
    print(f"[{mode}] workspace: {report.workspace}")
    if report.explicit_secret:
        print("bot 用的是 data/config.yaml 或环境变量里配的密钥：按它重算，不写密钥文件。")
    if report.already_salted and not report.changed_renames:
        print("会话键摘要已是加盐状态，无需迁移。")
    changed = report.changed_renames
    print(f"待重算会话数: {len(changed)}")
    for rename in changed:
        state = "moved" if (rename.old_ns, rename.new_ns) in report.moved_dirs else (
            "blocked" if rename.blocked else (
                "missing-dir" if not rename.dir_exists else "planned"))
        print(f"  {rename.real}: {rename.old_ns} -> {rename.new_ns} [{state}]")
    if report.unknown_conv_dirs:
        print("路由状态里查不到、保持原样的记忆目录（无法重算，请勿手工改名）:")
        for name in report.unknown_conv_dirs:
            print(f"  {name}")
    if report.blocked:
        print("两边都有长期记忆、或几个旧键撞到同一个新键，整段会话原样留着（处理掉一边再重跑）:")
        for rename in report.blocked:
            print(f"  {rename.real}: {rename.old_ns} -> {rename.new_ns}")
    if report.unresolved:
        print("认不出按哪把密钥、哪个账号算出来的会话键，原样留着（出自换掉的旧号的话，用 --account 补上再跑）:")
        for item in report.unresolved:
            print(f"  {item.real}: {item.old_ns}")
    print(f"认键时试过的账号: {'、'.join(account or '<无作用域>' for account in report.accounts)}")
    print(f"{'已改写' if report.apply else '将改写'}的按会话键存的功能数据: {format_store_counts(report.stores)}")
    if report.apply:
        print(f"备份目录: {report.backup_dir}")
        print(f"改名记忆目录: {len(report.moved_dirs)}；"
              f"重写命中缓存 key: {report.hit_keys_rewritten}；"
              f"更新路由状态 key: {report.route_keys_changed}；"
              f"密钥文件已写入: {report.secret_written}")
    else:
        print("这是 dry-run，未改动任何文件。加 --apply 真正执行。")
    print("supervisor 会话不改：这些群下次说话会各自新建 session，只丢一次短期上下文；"
          "长期记忆随目录迁移，定时任务、提醒这些功能数据跟着换成新键。")
    print("在 bot 与 supervisor 都停止的状态下运行本脚本。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default=None,
        help="Clonoth 工作区根目录；不给就和 bot 一样取 CLONOTH_WORKSPACE / ONEBOT_WORKSPACE_ROOT，可以写在 .env 里，都没设是仓库根。",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正执行迁移；不加则只 dry-run 打印计划。",
    )
    parser.add_argument(
        "--secret", default=None,
        help="迁移到的目标密钥（≥64 位 hex）；不给则复用已有密钥或随机生成。",
    )
    parser.add_argument(
        "--account", dest="accounts", action="append", default=[],
        help="另外按这个 bot QQ 号认会话键，可以给多次；换过号、旧号已经不在记录里的时候用。",
    )
    args = parser.parse_args(argv)
    workspace = offline_workspace(args.workspace)

    for account in args.accounts:
        if not normalize_account(account, QQ_NAPCAT):
            parser.error(f"--account 必须是 bot 的 QQ 号（纯数字），收到 {account!r}")

    secret = args.secret
    if secret is not None:
        secret = secret.strip()
        if not _SECRET_RE.fullmatch(secret):
            parser.error("--secret 必须是 ≥64 位十六进制字符串")

    try:
        report = run_migration(workspace=workspace, apply=args.apply, secret=secret, accounts=args.accounts)
    except SecretConflict as error:
        print(f"密钥冲突，未改动任何文件: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"迁移中止（未写入密钥，可修复后重跑；如已部分改动请从备份恢复）: {error}", file=sys.stderr)
        return 1
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
