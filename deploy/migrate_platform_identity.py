"""跨平台绑定：会话的长期记忆、短期上下文和功能数据接到另一个平台的会话上，人和群在新平台上沿用原来的别名。

新平台的会话键和 ID 都和原来的对不上（QQ 官方的 openid 推不出 QQ 号），只能由管理员按真实会话、真实 ID 手动对照。
离线跑、默认 dry-run、执行前自动备份；先用 conversations 看两边各有哪些会话。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 会话键怎么算必须和两边的 bot 同一份，所以直接用共享层的模块。
from adapters._shared.active_account import ActiveAccountStore, account_candidates, normalize_account  # noqa: E402
from adapters._shared.aliases import GROUP_BUCKET, USER_BUCKET, AliasTaken, rebind_alias, write_alias_file  # noqa: E402
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
from adapters._shared.user_profiles import load_profiles  # noqa: E402
from clonoth_runtime import offline_workspace  # noqa: E402
from engine import memory_subjects  # noqa: E402
from engine.conversation_migration import (  # noqa: E402
    ConversationRekey,
    MigrationPlan,
    RekeyCount,
    Rename,
    fresh_backup_dir,
    load_route_map,
    memory_root,
    move_memory,
    namespace_dir,
    record_route,
    route_state_file,
)
from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    CHANNEL_PRIVATE,
    CONVERSATION_KINDS,
    ID_SCOPE_ACCOUNT,
    STATE_ALIASES,
    PlatformDescriptor,
    normalize_name,
)
from platforms.ids import identity_key, normalize_id  # noqa: E402
from platforms.registry import (  # noqa: E402
    all_platforms,
    conversation_channel,
    conversation_key_prefix,
    find_platform,
    platform_profiles_path,
    platform_state_path,
    split_conversation_key,
)
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
IDENTITY_COMMANDS = {"person": USER_BUCKET, "group": GROUP_BUCKET}
BUCKET_NOUNS = {USER_BUCKET: "人", GROUP_BUCKET: "群"}
PROFILE_FIELD_LABELS = {"display_name": "显示名", "address_as": "称呼", "title": "头衔", "note": "备注"}


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


@dataclass(frozen=True)
class Identity:
    """身份绑定的一头：哪个平台上的哪个原始 ID，以及别名表里认它用的键。"""

    platform: PlatformDescriptor
    raw: str
    # ID 整个平台唯一时是空串。
    account: str
    key: str

    @property
    def label(self) -> str:
        return f"{self.platform.id}:{self.raw}"


@dataclass
class IdentityReport:
    workspace: Path
    apply: bool
    bucket: str
    source: Identity
    target: Identity
    alias: str
    # 目标身份原来的别名，换掉以后不再指向谁。
    replaced: str = ""
    # 同一个平台里换号：原来的身份让出别名。
    released: bool = False
    changed: bool = False
    # 被换掉的别名建过档，档案并进沿用的别名。
    roster_merged: bool = False
    # 源平台的按人资料里给这个人写了哪几项；资料在 config/ 里归人管，只提醒不代改。
    profile_fields: tuple[str, ...] = ()
    target_profiles: Path | None = None
    backup_dir: Path | None = None


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


def resolve_identity(workspace: Path, spec: str, account: str | None, *, flag: str, account_flag: str) -> Identity:
    """「平台 id:原始 ID」认成别名表里的键。按账号分 ID 的平台，账号不点名就用记下的当前账号。"""
    name, separator, raw_text = str(spec or "").strip().partition(":")
    platform = find_platform(name) if separator else None
    if platform is None or normalize_name(name) not in {platform.id, *platform.aliases}:
        raise BindingRefused(f"{flag} {spec!r}：写成「平台 id:原始 ID」，比如 qq:12345")
    raw = normalize_id(raw_text, platform.id_shape)
    if not raw:
        raise BindingRefused(f"{flag} {spec!r}：{raw_text.strip()!r} 不是 {platform.display_name} 的 ID")
    if platform.id_scope != ID_SCOPE_ACCOUNT:
        return Identity(platform, raw, "", identity_key(platform, raw))
    named = _named_account(account, platform, account_flag)
    chosen = ActiveAccountStore(platform, Path(workspace)).load() if named is None else named
    if not chosen:
        raise BindingRefused(f"{platform.display_name} 的 ID 按机器人账号分，用 {account_flag} 指定是哪个账号名下的")
    return Identity(platform, raw, chosen, identity_key(platform, raw, account=chosen))


def run_identity_binding(
    *, workspace: Path, bucket: str, source: str, target: str,
    source_account: str | None = None, target_account: str | None = None,
    apply: bool = False, now: float | None = None,
) -> IdentityReport:
    """让目标身份沿用源身份的别名：人的长期记忆和建档按别名认，群的别名写在记忆和提示词里。

    同一个平台里换号时原来的身份让出别名。目标原来的别名换掉以后不再指向谁：人要是已经在那个别名下攒了记忆就拒绝。
    """
    workspace = Path(workspace)
    noun = BUCKET_NOUNS[bucket]
    src = resolve_identity(workspace, source, source_account, flag="--from", account_flag="--from-account")
    dst = resolve_identity(workspace, target, target_account, flag="--to", account_flag="--to-account")
    if (src.platform.id, src.key) == (dst.platform.id, dst.key):
        raise BindingRefused("--from 和 --to 是同一个身份")
    source_path = platform_state_path(src.platform, STATE_ALIASES, workspace)
    target_path = platform_state_path(dst.platform, STATE_ALIASES, workspace)
    source_doc = _read_alias_document(source_path)
    entries = source_doc.get(bucket)
    entry = entries.get(src.key) if isinstance(entries, dict) else None
    alias = str(entry.get("alias") or "").strip() if isinstance(entry, dict) else ""
    if not alias:
        raise BindingRefused(f"{src.platform.display_name} 的别名表里没有这个{noun}（{src.label}）")
    released = source_path == target_path
    if released:
        # 同一个平台里换号：原来的身份让出别名，一个别名只能反查回一个身份。
        target_doc = {**source_doc, bucket: {key: item for key, item in entries.items() if key != src.key}}
    else:
        target_doc = _read_alias_document(target_path)
    try:
        rebind = rebind_alias(target_doc, bucket, dst.key, alias, now=time.time() if now is None else now)
    except AliasTaken as taken:
        raise BindingRefused(
            f"{dst.platform.display_name} 的别名表里 {alias} 已经是另一个{noun}（{taken.holder}）",
        ) from taken
    except ValueError as error:
        raise BindingRefused(f"{target_path} 读不懂，先修好再绑：{error}") from error
    report = IdentityReport(
        workspace=workspace, apply=apply, bucket=bucket, source=src, target=dst, alias=alias,
        replaced=rebind.replaced, released=released, changed=rebind.changed or released,
    )
    if bucket == USER_BUCKET:
        _check_person(workspace, report)
    if not apply or not report.changed:
        return report

    roster = memory_subjects.roster_path(workspace)
    report.backup_dir = _backup_files(workspace, [target_path, *([roster] if report.roster_merged else [])])
    write_alias_file(target_path, rebind.document)
    if report.roster_merged:
        memory_subjects.merge_subject(workspace, report.replaced, alias)
    return report


def _check_person(workspace: Path, report: IdentityReport) -> None:
    """人的别名连着长期记忆和建档：被换掉的别名已经攒下记忆就拒绝，建过档就并进沿用的别名。"""
    if report.replaced:
        namespace = memory_subjects.subject_namespace(report.replaced)
        if namespace and (memory_root(workspace) / namespace).is_dir():
            raise BindingRefused(
                f"{report.target.label} 在 {report.target.platform.display_name} 上已经有自己的长期记忆（{namespace}），"
                "两个人的记忆不能合并",
            )
        report.roster_merged = memory_subjects.is_enrolled(workspace, report.replaced)
    profiles = platform_profiles_path(report.source.platform, workspace)
    profile = load_profiles(profiles).get(report.source.key, {}) if profiles is not None else {}
    report.profile_fields = tuple(label for name, label in PROFILE_FIELD_LABELS.items() if profile.get(name))
    report.target_profiles = platform_profiles_path(report.target.platform, workspace)


def _read_alias_document(path: Path) -> dict[str, Any]:
    """别名文件读不懂就拒绝：当成空表写回去，别人的别名就全没了。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BindingRefused(f"{path} 读不懂，先修好再绑：{error}") from error
    if not isinstance(data, dict):
        raise BindingRefused(f"{path} 读不懂，先修好再绑：顶层不是对象")
    return data


def _backup_files(workspace: Path, paths: Iterable[Path]) -> Path | None:
    """改之前把这几份文件按工作区里的相对位置拷进新的备份目录；一份都不在就不建目录。"""
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    backup_dir = fresh_backup_dir(workspace)
    for path in existing:
        try:
            relative = path.resolve().relative_to(workspace.resolve())
        except ValueError:
            relative = Path(path.name)
        saved = backup_dir / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, saved)
    return backup_dir


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
    command, noun = ("group", "这个群") if source.kind == CHANNEL_GROUP else ("person", "这个人")
    print(f"要让{noun}在新平台上沿用原来的别名，再跑: {command} {_identity_flags(source, '--from')} {_identity_flags(target, '--to')}")
    print("在 bot 与 supervisor 都停止的状态下运行本脚本。")


def _identity_flags(side: Side, flag: str) -> str:
    """会话那一头对应的身份参数：真实会话键冒号后面就是平台上的群号或用户 ID。"""
    text = f"{flag} {side.platform.id}:{split_conversation_key(side.real)[1]}"
    return f"{text} {flag}-account {side.account}" if side.platform.id_scope == ID_SCOPE_ACCOUNT else text


def _print_identity(report: IdentityReport) -> None:
    noun = BUCKET_NOUNS[report.bucket]
    source, target = report.source, report.target
    done = "已" if report.apply else "将"
    print(f"[{'APPLY' if report.apply else 'DRY-RUN'}] workspace: {report.workspace}")
    print(f"源{noun}: {source.label}（{source.platform.display_name}{_account_note(source)}），别名 {report.alias}")
    print(f"目标{noun}: {target.label}（{target.platform.display_name}{_account_note(target)}）")
    if not report.changed:
        print(f"目标{noun}已经是 {report.alias}，不用改。")
    elif report.replaced:
        print(f"目标原来的别名 {report.replaced} {done}换成 {report.alias}，{report.replaced} 以后不再指向任何{noun}。")
    else:
        print(f"目标{noun}{done}用上别名 {report.alias}。")
    if report.released:
        print(f"同一个平台里换号：{source.label} 让出 {report.alias}，它再出现时会领新的别名。")
    if report.roster_merged:
        print(f"建档名册: {report.replaced} 的档案{done}并进 {report.alias}。")
    if report.profile_fields:
        fields = "、".join(report.profile_fields)
        if report.target_profiles is not None:
            print(f"按人资料: 源平台给这个人写了{fields}，不会自动带过去；要沿用就抄进 {report.target_profiles} 的 {target.key} 下。")
        else:
            print(f"按人资料: 源平台给这个人写了{fields}，{target.platform.display_name} 没有按人资料文件，带不过去。")
    if report.bucket == USER_BUCKET:
        # 功能数据按人的归属跟着原平台的 ID 走，换过去等于把两个身份合成一个人，决策上先不合并。
        print("他在原平台建的提醒、定时任务、资料和执行计划仍记在原来的身份名下：新平台上的他改不了，管理员照常能管。")
    if report.backup_dir is not None:
        print(f"备份目录: {report.backup_dir}")
    if not report.apply:
        print("这是 dry-run，未改动任何文件。加 --apply 真正执行。")
    print("在 bot 与 supervisor 都停止的状态下运行本脚本。")


def _account_note(identity: Identity) -> str:
    return f"，账号 {identity.account}" if identity.account else ""


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
    for command, bucket in IDENTITY_COMMANDS.items():
        noun = BUCKET_NOUNS[bucket]
        identity = commands.add_parser(command, parents=[common], help=f"让新平台上的同一个{noun}沿用原来的别名。")
        identity.add_argument("--from", dest="source", required=True, help=f"原来的{noun}，写成「平台 id:原始 ID」，比如 qq:12345。")
        identity.add_argument("--to", dest="target", required=True, help=f"新平台上的{noun}，写法同 --from。")
        identity.add_argument(
            "--from-account", default=None, help="源平台的 ID 按机器人账号分时，是哪个账号名下的；不给就用记下的当前账号。",
        )
        identity.add_argument(
            "--to-account", default=None, help="目标平台的 ID 按机器人账号分时，是哪个账号名下的；不给就用记下的当前账号。",
        )
        identity.add_argument("--apply", action="store_true", help="真正执行；不加则只 dry-run。")
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
        if args.command == "conversation":
            report: BindingReport | IdentityReport = run_binding(
                workspace=workspace, source_real=args.source, target_real=args.target,
                source_account=args.from_account, target_account=args.to_account, apply=args.apply,
            )
        else:
            report = run_identity_binding(
                workspace=workspace, bucket=IDENTITY_COMMANDS[args.command], source=args.source, target=args.target,
                source_account=args.from_account, target_account=args.to_account, apply=args.apply,
            )
    except BindingRefused as refused:
        print(f"没有绑定，未改动任何文件: {refused}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"绑定中止（如已部分改动请从备份恢复）: {error}", file=sys.stderr)
        return 1
    if isinstance(report, BindingReport):
        _print_binding(report, target_account_named=args.to_account is not None)
    else:
        _print_identity(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
