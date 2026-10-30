"""从控制台开一个新实例。

无特权的那一半（工作区、软链、.env、清单）在这里做；docker、systemd、cloudflared
由 root 侧的 kazebot-provision@.service 干，本模块只负责触发它并回读进度文件。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import config_layout
from engine.eventlog_rotation import append_lines
from platforms.descriptor import HOOK_INSTANCE_ENV, PlatformDescriptor
from platforms.ids import normalize_id
from platforms.registry import (
    DEFAULT_PLATFORM_ID,
    PLATFORM_ENV,
    MissingPlatformHook,
    UnknownPlatform,
    platform_hook,
    platform_named,
)
from platforms.wording import name_with

from .instances import (
    INSTANCE_ID_PATTERN,
    MAX_INDEX,
    RESERVED_IDS,
    InstanceDirectoryError,
    NoFreeIndex,
    account_owner_in,
    allocate_index,
    instances_file,
    is_instance_id,
    load_instances,
    normalize_prefix,
    ports_for,
    prefix_for,
    remove_instance,
    save_instance,
)

# 工作区里这几个名字必须解析到代码：workspace_root 直接拼接它们找 dist、system_nodes、工具和仓库默认。
_LINKED = config_layout.CODE_LINKS
# 磁盘每个实例每月涨 4G 左右，留不出这么多就别开新实例了。
_MIN_FREE_BYTES = 8 * 1024**3
# 只有 kazebot 自己能读：里面有 OneBot 的访问令牌。
_ENV_FILE_MODE = 0o600

CREATE_UNIT = "kazebot-provision@{instance_id}.service"
REMOVE_UNIT = "kazebot-deprovision@{instance_id}.service"
WRITABLE_UNIT = "kazebot-writable@{instance_id}.service"

# 带着「归档」：root 侧接手之前轮询读到的就是这一行，progress 据此认出这是在删
REMOVAL_REQUESTED = "已发起停用并归档，等 root 侧接手…"

_log = logging.getLogger(__name__)


class ProvisionError(RuntimeError):
    """给调用方直接当 400 用的人话错误。"""


class RosterUnreadable(ProvisionError):
    """实例清单读不懂：坏的是服务器上的文件，不是请求，接口回 503。"""


@dataclass(frozen=True)
class Plan:
    instance_id: str
    label: str
    idx: int
    workspace: Path
    ports: dict[str, int]
    prefix: str
    platform: str = DEFAULT_PLATFORM_ID
    # 建实例时还没登录的话是空串，扫码登上之后由账号页记进清单。
    account: str = ""


def validate_instance_id(raw: str) -> str:
    instance_id = str(raw or "").strip()
    if not is_instance_id(instance_id):
        raise ProvisionError(
            f"实例名要以小写字母开头、只含小写字母数字和下划线，2-32 位（不能叫 {'、'.join(sorted(RESERVED_IDS))}）；"
            "按 QQ 号开的老实例是 5-11 位数字"
        )
    return instance_id


def resolve_platform(raw: Any) -> PlatformDescriptor:
    """新实例要跑的平台。没写就是 QQ，和没声明平台的老实例一样。"""
    try:
        return platform_named(str(raw or "").strip() or DEFAULT_PLATFORM_ID)
    except UnknownPlatform as error:
        raise ProvisionError(str(error)) from None


def normalize_account(platform: PlatformDescriptor, raw: Any) -> str:
    """建实例时登记的账号，写法要合这个平台的 ID 形状。可以不给，登录之后再记。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    account = normalize_id(text, platform.id_shape)
    if not account:
        raise ProvisionError(name_with(platform.display_name, f"的账号写法不对：{text}"))
    return account


def platform_account_from(body: Mapping[str, Any]) -> tuple[PlatformDescriptor, str]:
    """请求里的平台和账号。

    没开页面刷新的旧控制台还会发一个实例挂几个平台时的 accounts 映射：只有一项时照认，
    多于一项说明它还以为一个实例能挂多个平台，直接拒掉，不替它挑。
    """
    if "platform" in body or "account" in body:
        platform = resolve_platform(body.get("platform"))
        return platform, normalize_account(platform, body.get("account"))
    legacy = body.get("accounts")
    if legacy is None:
        return resolve_platform(None), ""
    if not isinstance(legacy, Mapping):
        raise ProvisionError("accounts 要写成「平台 id: 账号」")
    filled = [(name, value) for name, value in legacy.items() if str(value or "").strip()]
    if len(filled) > 1:
        raise ProvisionError("一个实例只跑一个平台，请刷新控制台后重新建")
    if not filled:
        return resolve_platform(next(iter(legacy), None)), ""
    name, value = filled[0]
    platform = resolve_platform(name)
    return platform, normalize_account(platform, value)


def data_root(workspace_root: Path) -> Path:
    """几个实例的工作区都在清单文件旁边。序号 0 留在代码目录，不在这下面。"""
    return instances_file(workspace_root).parent


def link_shared_stickers(workspace: Path, workspace_root: Path) -> bool:
    """表情包库几个实例共用一份。

    做成软链而不是配一个绝对路径：附件白名单只认工作区内的 data/ 前缀，
    库放在工作区外就发不出去。
    """
    shared = data_root(workspace_root) / "stickers"
    shared.mkdir(parents=True, exist_ok=True)
    link = Path(workspace) / "data" / "stickers"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        return Path(os.readlink(link)) == shared
    if link.exists():
        # 已经是真目录说明这个实例自己攒过图，合并要人工决定，不能默默覆盖。
        return False
    link.symlink_to(shared, target_is_directory=True)
    return True


def log_path(workspace_root: Path, instance_id: str) -> Path:
    """进度文件放工作区外：删实例会把工作区改名，日志跟着搬走就再也读不到结果。"""
    return data_root(workspace_root) / f"provision-{instance_id}.log"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _unreadable_roster(error: InstanceDirectoryError, then: str = "先在服务器上修好它") -> RosterUnreadable:
    """给管理员看的那句：清单在哪、哪里读不懂（不带文件内容），接着说这一步因此怎么了。"""
    return RosterUnreadable(f"{error.reason}。{then}")


def _roster(workspace_root: Path) -> list[dict[str, Any]]:
    """要拿来分配序号、查重名的清单。读不懂就停下：当成没有别的实例，会分到正在跑的实例的端口。"""
    try:
        return load_instances(workspace_root, required=True)
    except InstanceDirectoryError as error:
        raise _unreadable_roster(error) from error


def plan_instance(
    workspace_root: Path,
    *,
    instance_id: str,
    label: str,
    platform: PlatformDescriptor | None = None,
    account: str = "",
) -> Plan:
    """算出新实例的落点。不落盘，纯计算，便于先给前端看一眼。"""
    instance_id = validate_instance_id(instance_id)
    platform = platform or resolve_platform(None)
    account = normalize_account(platform, account)
    rows = _roster(workspace_root)
    if any(row["id"] == instance_id for row in rows):
        raise ProvisionError(f"{instance_id} 已经在清单里了")
    owner = account_owner_in(rows, platform.id, account)
    if owner is not None:
        raise ProvisionError(f"{account} 已经归实例 {owner['label']} 管了，同一个号两边登会互相踢下线")

    root = data_root(workspace_root)
    target = root / instance_id
    if target.exists():
        raise ProvisionError(f"{target} 已存在，先在服务器上确认它不是还在跑的实例")

    existing = root if root.exists() else root.parent
    # 多开实例的 systemd 沙箱只放开它自己的工作区，数据根是只读挂载；access 认得出只读挂载。
    if not os.access(existing, os.W_OK):
        raise ProvisionError(f"这个实例写不了 {existing}，到主实例的控制台去建")

    free = shutil.disk_usage(existing).free
    if free < _MIN_FREE_BYTES:
        raise ProvisionError(f"磁盘只剩 {free / 1024**3:.1f}G，不够开新实例")

    try:
        idx = allocate_index(rows)
    except NoFreeIndex as error:
        raise ProvisionError(str(error)) from error
    return Plan(
        instance_id=instance_id,
        label=(label or "").strip() or instance_id,
        idx=idx,
        workspace=target,
        ports=ports_for(idx),
        prefix=prefix_for(idx, instance_id),
        platform=platform.id,
        account=account,
    )


def _platform_env(plan: Plan) -> list[str]:
    """只写实例自己那个平台的几行：别的平台的端口和令牌写进来，只会让人以为这个实例也在跑它。"""
    platform = platform_named(plan.platform)
    try:
        module = platform_hook(platform, HOOK_INSTANCE_ENV)
    except MissingPlatformHook:
        return []
    ports = {port.name: plan.ports[port.name] for port in platform.instance_ports}
    return list(module.instance_env(plan.idx, ports))


def _env_body(plan: Plan) -> str:
    lines = [
        f"# 实例 {plan.instance_id} ({plan.label})",
        f"{PLATFORM_ENV}={plan.platform}",
        f"CLONOTH_PORT={plan.ports['supervisor']}",
        f"CLONOTH_BASE_URL=http://127.0.0.1:{plan.ports['supervisor']}{plan.prefix}",
        *_platform_env(plan),
    ]
    if plan.prefix:
        lines.append(f"CLONOTH_URL_PREFIX={plan.prefix.strip('/')}")
    return "\n".join(lines) + "\n"


def scaffold(plan: Plan, workspace_root: Path) -> None:
    """建目录骨架。到这一步为止全都在 kazebot 权限内，失败不会留下半个服务。"""
    repo = _repo_root()
    # config/ 有密钥、data/ 有令牌和会话，只给属主，和 bootstrap.sh 的 0700 对齐；mkdir 的 mode 受 umask 削，再 chmod 补上
    for name in ("data", "config"):
        directory = plan.workspace / name
        directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    # NapCat 要按同一个绝对路径读写附件，容器挂载前目录得先在。
    (plan.workspace / "data" / "attachments").mkdir(parents=True, exist_ok=True)

    for name in _LINKED:
        source = repo / name
        if not source.is_dir():
            raise ProvisionError(f"代码目录缺少 {name}")
        link = plan.workspace / name
        if link.is_symlink():
            if Path(os.readlink(link)) != source:
                link.unlink()
                link.symlink_to(source, target_is_directory=True)
        elif link.exists():
            raise ProvisionError(f"{link} 是真实目录，不敢覆盖")
        else:
            link.symlink_to(source, target_is_directory=True)

    link_shared_stickers(plan.workspace, workspace_root)

    # 已经有的 .env 不动：重试建实例时 root 侧可能已经往里回填过 WebUI 令牌。
    with contextlib.suppress(config_layout.ConfigConflict):
        config_layout.write_config_text(plan.workspace / ".env", _env_body(plan), base_hash="", mode=_ENV_FILE_MODE)

    # 先由 kazebot 建出来，root 之后只 append —— 属主保持不变，控制台才读得到后续进度。
    log_path(workspace_root, plan.instance_id).write_text(
        "工作区就绪，等 root 侧接手…\n", encoding="utf-8",
    )

    try:
        save_instance(
            workspace_root,
            instance_id=plan.instance_id,
            label=plan.label,
            idx=plan.idx,
            platform=plan.platform,
            account=plan.account,
        )
    except InstanceDirectoryError as error:
        raise _unreadable_roster(error, f"没有登记 {plan.instance_id}") from error


async def _systemctl(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def start_unit(unit: str) -> None:
    """--no-block：oneshot 单元会跑上一两分钟，不能把请求挂在这里。"""
    try:
        code, out = await _systemctl("start", "--no-block", unit)
    except OSError as error:
        raise ProvisionError(f"启动 {unit} 失败，调不起 systemctl：{error}") from error
    if code != 0:
        raise ProvisionError(f"启动 {unit} 失败（polkit 规则和 root 侧脚本是最新的吗？）：{out}")


def _append_progress(workspace_root: Path, instance_id: str, line: str) -> None:
    """往进度文件末尾接一行。写不进去只记日志：进度是给人看的，不该因此拦下要做的事。"""
    path = log_path(workspace_root, instance_id)
    try:
        # append_lines 遇到上一次停在半行会先换行：粘成一行，DONE 开头的旧结果就成了这一次的
        append_lines(path, line.encode("utf-8") + b"\n")
    except OSError as error:
        _log.warning("实例 %s 的进度文件 %s 没写进去：%s", instance_id, path, error)


async def hand_to_root(workspace_root: Path, instance_id: str, unit: str, *, opening: str = "") -> None:
    """启动 root 侧单元。root 只往进度文件后面追加，从不截断。

    opening 先接进去：root 接手之前轮询读到的末行就不是上一次留下的 DONE。单元没起来时补一行 DONE fail，
    控制台不会一直显示「进行中」。
    """
    if opening:
        _append_progress(workspace_root, instance_id, opening)
    try:
        await start_unit(unit)
    except ProvisionError as error:
        _append_progress(workspace_root, instance_id, f"DONE fail: {error}")
        raise


async def unit_state(unit: str) -> str:
    _, out = await _systemctl("show", "-p", "ActiveState", "--value", unit)
    return out or "unknown"


def read_log(path: Path, *, limit: int = 200) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def progress(workspace_root: Path, instance_id: str) -> dict[str, Any]:
    """给前端轮询。脚本最后一行写 DONE，据此判成败，不必去读 journal。

    只认最后一行：重试删除时新进度接在上一次的 DONE fail 后面，往回找 DONE 会把上一次的失败当成这一次的结果。
    """
    instance_id = validate_instance_id(instance_id)
    lines = read_log(log_path(workspace_root, instance_id))
    last = next((line for line in reversed(lines) if line.strip()), "")
    done = last if last.startswith("DONE") else ""
    payload: dict[str, Any] = {
        "id": instance_id,
        "lines": lines,
        "finished": bool(done),
        "ok": done.startswith("DONE ok"),
        "detail": done[len("DONE fail:"):].strip() if done.startswith("DONE fail:") else "",
    }
    if not done:
        payload.update({
            "state": "running" if lines else "queued",
            "retryable": True,
            "action": "remove" if any("归档" in line or "停服务" in line for line in lines) else "create",
        })
    return payload


def forget(workspace_root: Path, instance_id: str) -> None:
    """从清单摘掉。工作区由 root 侧改名归档，这里不碰文件。"""
    try:
        remove_instance(workspace_root, validate_instance_id(instance_id))
    except InstanceDirectoryError as error:
        raise _unreadable_roster(error) from error


def guard_removable(workspace_root: Path, instance_id: str, *, current_prefix: str) -> dict[str, Any]:
    """不许删自己，也不许删清单外的实例 —— 后者意味着参数是猜出来的。"""
    instance_id = validate_instance_id(instance_id)
    rows = _roster(workspace_root)
    row = next((r for r in rows if r["id"] == instance_id), None)
    if row is None:
        raise ProvisionError(f"{instance_id} 不在清单里")
    if row["path"] == normalize_prefix(current_prefix):
        raise ProvisionError("不能删掉正在用的这个实例")
    if row["idx"] == 0:
        raise ProvisionError("序号 0 是主实例，只能在服务器上手动处理")
    return row


__all__ = [
    "CREATE_UNIT",
    "INSTANCE_ID_PATTERN",
    "MAX_INDEX",
    "Plan",
    "ProvisionError",
    "REMOVAL_REQUESTED",
    "REMOVE_UNIT",
    "RESERVED_IDS",
    "RosterUnreadable",
    "data_root",
    "forget",
    "guard_removable",
    "hand_to_root",
    "log_path",
    "normalize_account",
    "plan_instance",
    "platform_account_from",
    "resolve_platform",
    "progress",
    "scaffold",
    "start_unit",
    "unit_state",
    "validate_instance_id",
]
