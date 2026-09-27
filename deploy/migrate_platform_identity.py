"""把一个会话的长期记忆、短期上下文和功能数据绑到另一个平台的会话上：离线、默认 dry-run、自动备份。

新平台的会话键和原来的对不上（QQ 官方的 openid 推不出 QQ 号），只能由管理员按真实会话手动对照。
先用 conversations 看两边各有哪些会话，再用 conversation --from --to 绑定。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 会话键怎么算必须和两边的 bot 同一份，所以直接用共享层的模块。
from adapters._shared.active_account import ActiveAccountStore, account_candidates, normalize_account  # noqa: E402
from adapters._shared.conversation_hash import (  # noqa: E402
    SECRET_EXPLICIT,
    SECRET_LEGACY,
    SECRET_PINNED,
    SECRET_UNSETTLED,
    ConversationKeys,
    CurrentSecret,
    current_secret,
    key_origin,
)
from clonoth_runtime import offline_workspace  # noqa: E402
from engine.conversation_migration import (  # noqa: E402
    ConversationRekey,
    MigrationPlan,
    RekeyCount,
    Rename,
    load_route_map,
    memory_root,
    move_memory,
    namespace_dir,
    record_route,
    route_state_file,
)
from platforms.descriptor import CHANNEL_GROUP, CHANNEL_PRIVATE, CONVERSATION_KINDS, PlatformDescriptor  # noqa: E402
from platforms.registry import all_platforms, conversation_channel, conversation_key_prefix, find_platform  # noqa: E402
from supervisor.conversation_stores import (  # noqa: E402
    ConversationStoreError,
    format_store_counts,
    rekey_conversation_stores,
)
from workspace import workspace_instance  # noqa: E402

KIND_LABELS = {CHANNEL_GROUP: "群聊", CHANNEL_PRIVATE: "私聊"}
SECRET_LABELS = {
    SECRET_EXPLICIT: "显式配置",
    SECRET_PINNED: "已钉住的文件",
    SECRET_LEGACY: "无盐（首次启动会钉在无盐上）",
    SECRET_UNSETTLED: "还没定下来",
}


class BindingRefused(RuntimeError):
    """对不上、会丢数据，或者还算不出新键：什么都不改，告诉人先做什么。"""


@dataclass(frozen=True)
class Side:
    """绑定的一头：真实会话、它在哪个平台的哪类渠道，以及在哪个机器人账号下算出的稳定键。"""

    real: str
    platform: PlatformDescriptor
    kind: str
    account: str
    stable: str

    @property
    def namespace(self) -> str:
        return namespace_dir(self.stable)


@dataclass
class BindingReport:
    workspace: Path
    apply: bool
    source: Side
    target: Side
    source_memory: bool
    target_memory: bool
    # 目标 bot 还没在那个会话里说过话时要补路由；dry-run 是要不要补，apply 是补了没有。
    route_recorded: bool = False
    # 按会话键存数据的各个库：dry-run 是预览，apply 是实际改写的行数。
    stores: dict[str, RekeyCount] = field(default_factory=dict)
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    hit_keys_rewritten: int = 0
    backup_dir: Path | None = None


@dataclass(frozen=True)
class ConversationRow:
    real: str
    stable: str
    # 认不出在哪个账号下算出来的是 None。
    account: str | None
    memory: bool


@dataclass(frozen=True)
class PlatformConversations:
    platform: PlatformDescriptor
    secret_source: str
    account: str
    rows: list[ConversationRow]
    # 这个平台算不出会话键时的原因，rows 这时是空的。
    problem: str = ""


def account_label(account: str) -> str:
    return account or "<无作用域>"


def platform_secret_state(platform: PlatformDescriptor, workspace: Path) -> CurrentSecret:
    try:
        return current_secret(platform, Path(workspace))
    except LookupError as error:
        raise BindingRefused(f"{platform.display_name} 没有声明会话键摘要密钥，算不出它的会话键") from error


def list_conversations(workspace: Path, platforms: Iterable[PlatformDescriptor]) -> list[PlatformConversations]:
    """各平台路由表里记着的会话：真实会话键、稳定键、在哪个账号下算出来的、有没有长期记忆。"""
    workspace = Path(workspace)
    root = memory_root(workspace)
    listed: list[PlatformConversations] = []
    for platform in platforms:
        account = ActiveAccountStore(platform, workspace).load()
        try:
            secret = platform_secret_state(platform, workspace)
        except BindingRefused as refused:
            listed.append(PlatformConversations(platform, "", account, [], problem=str(refused)))
            continue
        candidates = account_candidates(platform, workspace)
        rows = []
        routes = load_route_map(route_state_file(workspace, platform))
        for stable, real in sorted(routes.items(), key=lambda item: (item[1], item[0])):
            origin = key_origin(stable, real, candidate_secrets=(secret.value,), candidate_scopes=candidates)
            rows.append(ConversationRow(
                real=real, stable=stable, account=origin.bot_scope if origin else None,
                memory=(root / namespace_dir(stable)).is_dir(),
            ))
        listed.append(PlatformConversations(platform, secret.source, account, rows))
    return listed


def resolve_source(workspace: Path, real: str, account: str | None = None) -> Side:
    """源会话在哪个账号下的稳定键：点了名就按那个号算，没点名就从路由表里认，几个号下都有时让人挑。"""
    workspace = Path(workspace)
    platform, kind = _conversation_of(real, "--from")
    secret = platform_secret_state(platform, workspace)
    if not secret.settled:
        raise BindingRefused(f"{platform.display_name} 还没定下摘要密钥，也就还没有会话数据可绑")
    named = _named_account(account, platform, "--from-account")
    if named is not None:
        return Side(real, platform, kind, named, _stable_key(platform, secret, real, named, "--from-account"))
    channel = conversation_key_prefix(real)
    stables = [
        stable for stable, mapped in load_route_map(route_state_file(workspace, platform)).items()
        if mapped == real and conversation_key_prefix(stable) == channel
    ]
    if not stables:
        raise BindingRefused(f"{platform.display_name} 的路由表里没有 {real}；它的数据在哪个账号下，用 --from-account 点名")
    candidates = account_candidates(platform, workspace)
    origins = [
        key_origin(stable, real, candidate_secrets=(secret.value,), candidate_scopes=candidates) for stable in stables
    ]
    if len(stables) > 1:
        names = "、".join(account_label(origin.bot_scope) if origin else "认不出的账号" for origin in origins)
        raise BindingRefused(f"{real} 在几个账号下各有一个会话（{names}），用 --from-account 指定绑定哪一个")
    [stable], [origin] = stables, origins
    if origin is None:
        raise BindingRefused(f"认不出 {real} 的会话键是在哪个账号下算出来的，用 --from-account 点名")
    return Side(real, platform, kind, origin.bot_scope, stable)


def resolve_target(workspace: Path, real: str, account: str | None = None) -> Side:
    """目标会话以后在目标 bot 那里的稳定键：按目标平台的密钥和账号算，和目标 bot 自己算的是同一个。"""
    workspace = Path(workspace)
    platform, kind = _conversation_of(real, "--to")
    secret = platform_secret_state(platform, workspace)
    if not secret.settled:
        raise BindingRefused(f"{platform.display_name} 还没定下摘要密钥，启动一次目标 bot 再绑定")
    named = _named_account(account, platform, "--to-account")
    chosen = ActiveAccountStore(platform, workspace).load() if named is None else named
    if named is None and not chosen:
        raise BindingRefused(f"{platform.display_name} 还没记下账号，先让目标 bot 登录一次，或用 --to-account 指定")
    return Side(real, platform, kind, chosen, _stable_key(platform, secret, real, chosen, "--to-account"))


def run_binding(
    *, workspace: Path, source_real: str, target_real: str,
    source_account: str | None = None, target_account: str | None = None, apply: bool = False,
) -> BindingReport:
    """把源会话名下的长期记忆、会话注册表和功能数据挂到目标会话的稳定键下，再在目标平台补一条路由。

    源平台的路由原样留着：源 bot 还在那个会话里的话，之后说的话照常落在源会话名下。
    """
    workspace = Path(workspace)
    source_real, target_real = str(source_real or "").strip(), str(target_real or "").strip()
    if source_real == target_real:
        raise BindingRefused("--from 和 --to 是同一个会话")
    source = resolve_source(workspace, source_real, source_account)
    target = resolve_target(workspace, target_real, target_account)
    if source.kind != target.kind:
        raise BindingRefused(
            f"{source.real} 是{KIND_LABELS[source.kind]}，{target.real} 是{KIND_LABELS[target.kind]}，不能绑在一起",
        )
    root = memory_root(workspace)
    report = BindingReport(
        workspace=workspace, apply=apply, source=source, target=target,
        source_memory=(root / source.namespace).is_dir(), target_memory=(root / target.namespace).is_dir(),
    )
    if report.source_memory and report.target_memory:
        raise BindingRefused("两边都有长期记忆，合并语义不明；先在控制台处理掉一边再绑")

    rekey = ConversationRekey(
        {source.stable: target.stable},
        # 加账号隔离之前的功能记录，账号一栏记的是工作区实例号。
        bot_scopes={source.account or workspace_instance(workspace): target.account},
    )
    routes = route_state_file(workspace, target.platform)
    try:
        # 先按同一份对照整体预览：路由读不懂、哪个库读不了或有冲突，在动任何东西之前就停下。
        report.route_recorded = record_route(routes, target.stable, target.real, apply=False)
        report.stores = rekey_conversation_stores(workspace, rekey, apply=False)
    except (OSError, ValueError, ConversationStoreError) as error:
        raise BindingRefused(str(error)) from error
    if not report.source_memory and not any(report.stores.values()):
        raise BindingRefused(
            f"{source.real} 在账号 {account_label(source.account)} 下既没有长期记忆，也没有会话和功能数据，没有可以绑的",
        )
    if not apply:
        return report

    plan = MigrationPlan(renames=[Rename(
        real=source.real, old_stable=source.stable, new_stable=target.stable,
        old_ns=source.namespace, new_ns=target.namespace,
        dir_exists=report.source_memory, target_exists=report.target_memory,
    )])
    outcome = move_memory(workspace=workspace, platform=target.platform, plan=plan)
    report.moved_dirs = outcome.moved_dirs
    report.hit_keys_rewritten = outcome.hit_keys_rewritten
    report.backup_dir = outcome.backup_dir
    report.route_recorded = record_route(routes, target.stable, target.real, apply=True)
    report.stores = rekey_conversation_stores(workspace, rekey, apply=True, backup_dir=outcome.backup_dir)
    return report


def _conversation_of(real: str, flag: str) -> tuple[PlatformDescriptor, str]:
    found = conversation_channel(real)
    if found is None:
        raise BindingRefused(f"{flag} {real!r}：认不出是哪个平台的会话，写成「渠道:原始 ID」，比如 qq_group:123456")
    platform, channel = found
    if channel.kind not in CONVERSATION_KINDS:
        raise BindingRefused(f"{flag} {real}：{channel.name} 不是群聊或私聊，没有会话数据可绑")
    if not channel.hashed:
        raise BindingRefused(f"{flag} {real}：{channel.name} 的会话键就是原始 ID，不经过摘要，这个工具不处理")
    return platform, channel.kind


def _named_account(value: str | None, platform: PlatformDescriptor, flag: str) -> str | None:
    if value is None:
        return None
    named = normalize_account(value, platform)
    if not named and str(value).strip():
        # 写错的号规范化出来是空串，会被当成「无作用域」去算键。
        raise BindingRefused(f"{flag} {value!r} 不是 {platform.display_name} 的账号")
    return named


def _stable_key(platform: PlatformDescriptor, secret: CurrentSecret, real: str, account: str, flag: str) -> str:
    if not account and not platform.legacy_conversation_keys:
        raise BindingRefused(f"{platform.display_name} 的会话键要带机器人账号，{flag} 不能是空的")
    return ConversationKeys(platform, secret.value).stable_key(real, bot_scope=account)


def _print_conversations(listed: list[PlatformConversations]) -> None:
    for entry in listed:
        platform = entry.platform
        print(f"{platform.display_name}（{platform.id}）：当前账号 {account_label(entry.account)}")
        if entry.problem:
            print(f"  {entry.problem}")
            continue
        print(f"  摘要密钥: {SECRET_LABELS.get(entry.secret_source, entry.secret_source)}")
        if not entry.rows:
            print("  路由表里还没有会话。")
        for row in entry.rows:
            account = "认不出的账号" if row.account is None else f"账号 {account_label(row.account)}"
            memory = "有长期记忆" if row.memory else "没有长期记忆"
            print(f"  {row.real}  {account}  {memory}  {row.stable}")


def _print_binding(report: BindingReport, *, target_account_named: bool) -> None:
    source, target = report.source, report.target
    mode = "APPLY" if report.apply else "DRY-RUN"
    print(f"[{mode}] workspace: {report.workspace}")
    print(f"源会话: {source.real}（{source.platform.display_name}，账号 {account_label(source.account)}）")
    print(f"目标会话: {target.real}（{target.platform.display_name}，账号 {account_label(target.account)}）")
    if report.source_memory:
        state = "已搬过去" if report.moved_dirs else "将搬过去"
        print(f"长期记忆: {source.namespace} -> {target.namespace} [{state}]")
    else:
        print("长期记忆: 源会话没有，目标那边的原样不动")
    if report.route_recorded:
        print(f"目标平台路由: {'已补上' if report.apply else '将补上'} {target.stable} -> {target.real}")
    else:
        print("目标平台路由: 已经有这一条，不用补")
    print(f"{'已改写' if report.apply else '将改写'}的按会话键存的数据: {format_store_counts(report.stores)}")
    print("附件目录、源平台的路由、定时任务记的创建人都原样留着。")
    if report.apply:
        print(f"备份目录: {report.backup_dir}")
        print(f"命中缓存 key: {report.hit_keys_rewritten}")
    else:
        print("这是 dry-run，未改动任何文件。加 --apply 真正执行。")
    if target_account_named:
        print("目标 bot 登录的账号要和 --to-account 一致，不然它算出来的会话键对不上。")
    print("在 bot 与 supervisor 都停止的状态下运行本脚本。")


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--workspace", default=None,
        help="Clonoth 工作区根目录；不给就和 bot 一样取 CLONOTH_WORKSPACE / ONEBOT_WORKSPACE_ROOT，可以写在 .env 里，都没设是仓库根。",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "conversations", parents=[common], help="列出各平台路由表里的会话，绑定前先看两边的真实会话键。",
    )
    listing.add_argument("--platform", default=None, help="只看这个平台（平台 id）。")
    binding = commands.add_parser("conversation", parents=[common], help="把一个会话的数据绑到另一个会话上。")
    binding.add_argument("--from", dest="source", required=True, help="源会话的真实会话键，比如 qq_group:123456。")
    binding.add_argument("--to", dest="target", required=True, help="目标会话的真实会话键。")
    binding.add_argument(
        "--from-account", default=None,
        help='源会话的数据在哪个机器人账号下；不给就从路由表里认。加账号隔离之前的 QQ 存量数据传空串 ""。',
    )
    binding.add_argument("--to-account", default=None, help="目标平台的机器人账号；不给就用目标 bot 记下的当前账号。")
    binding.add_argument("--apply", action="store_true", help="真正执行；不加则只 dry-run。")
    args = parser.parse_args(argv)
    workspace = offline_workspace(args.workspace)

    if args.command == "conversations":
        if args.platform is None:
            platforms = all_platforms()
        else:
            platform = find_platform(args.platform)
            if platform is None:
                print(f"没有 id 为 {args.platform!r} 的平台", file=sys.stderr)
                return 2
            platforms = (platform,)
        _print_conversations(list_conversations(workspace, platforms))
        return 0

    try:
        report = run_binding(
            workspace=workspace, source_real=args.source, target_real=args.target,
            source_account=args.from_account, target_account=args.to_account, apply=args.apply,
        )
    except BindingRefused as refused:
        print(f"没有绑定，未改动任何文件: {refused}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"绑定中止（如已部分改动请从备份恢复）: {error}", file=sys.stderr)
        return 1
    _print_binding(report, target_account_named=args.to_account is not None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
