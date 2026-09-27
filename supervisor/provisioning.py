"""从控制台开一个新实例。

无特权的那一半（工作区、软链、.env、清单）在这里做；docker、systemd、cloudflared
由 root 侧的 kazebot-provision@.service 干，本模块只负责触发它并回读进度文件。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from platforms.descriptor import HOOK_INSTANCE_ENV
from platforms.ids import normalize_id
from platforms.registry import MissingPlatformHook, all_platforms, get_platform, platform_hook
from platforms.wording import name_with

from .instances import (
    MAX_INDEX,
    NoFreeIndex,
    account_owner_in,
    allocate_index,
    instances_file,
    load_instances,
    normalize_prefix,
    ports_for,
    prefix_for,
    remove_instance,
    save_instance,
)

# 实例名会一路传进 root 侧的 systemd 单元名，宽一格都是提权面。纯数字那支认改造前按 QQ 号开的实例。
INSTANCE_ID_PATTERN = r"[a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10}"
_INSTANCE_ID_RE = re.compile(rf"^(?:{INSTANCE_ID_PATTERN})$")
# 数据根下已有的共享目录。实例叫这个名字，工作区就和它是同一个目录。
RESERVED_IDS = frozenset({"stickers"})
# 工作区里这几个名字必须解析到代码：workspace_root 直接拼接它们找 dist、system_nodes 和工具。
_LINKED = ("adapters", "engine", "plugins", "tools")
# 磁盘每个实例每月涨 4G 左右，留不出这么多就别开新实例了。
_MIN_FREE_BYTES = 8 * 1024**3

CREATE_UNIT = "kazebot-provision@{instance_id}.service"
REMOVE_UNIT = "kazebot-deprovision@{instance_id}.service"


class ProvisionError(RuntimeError):
    """给调用方直接当 400 用的人话错误。"""


@dataclass(frozen=True)
class Plan:
    instance_id: str
    label: str
    idx: int
    workspace: Path
    ports: dict[str, int]
    prefix: str
    accounts: dict[str, str] = field(default_factory=dict)


def validate_instance_id(raw: str) -> str:
    instance_id = str(raw or "").strip()
    if not _INSTANCE_ID_RE.match(instance_id) or instance_id in RESERVED_IDS:
        raise ProvisionError(
            "实例名要以小写字母开头、只含小写字母数字和下划线，2-32 位（不能叫 stickers）；"
            "按 QQ 号开的老实例是 5-11 位数字"
        )
    return instance_id


def normalize_accounts(raw: Any) -> dict[str, str]:
    """建实例时登记的平台账号：平台要认得，账号写法要合这个平台的 ID 形状。"""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ProvisionError("accounts 要写成「平台 id: 账号」")
    accounts: dict[str, str] = {}
    for name, value in raw.items():
        text = str(value or "").strip()
        if not text:
            continue
        try:
            platform = get_platform(name)
        except KeyError:
            raise ProvisionError(f"不认识的平台：{name}") from None
        account = normalize_id(text, platform.id_shape)
        if not account:
            raise ProvisionError(name_with(platform.display_name, f"的账号写法不对：{text}"))
        accounts[platform.id] = account
    return accounts


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


def plan_instance(
    workspace_root: Path, *, instance_id: str, label: str, accounts: Any = None,
) -> Plan:
    """算出新实例的落点。不落盘，纯计算，便于先给前端看一眼。"""
    instance_id = validate_instance_id(instance_id)
    accounts = normalize_accounts(accounts)
    rows = load_instances(workspace_root)
    if any(row["id"] == instance_id for row in rows):
        raise ProvisionError(f"{instance_id} 已经在清单里了")
    for platform_id, account in accounts.items():
        owner = account_owner_in(rows, platform_id, account)
        if owner is not None:
            raise ProvisionError(f"{account} 已经归实例 {owner['label']} 管了，同一个号两边登会互相踢下线")

    root = data_root(workspace_root)
    target = root / instance_id
    if target.exists():
        raise ProvisionError(f"{target} 已存在，先在服务器上确认它不是还在跑的实例")

    free = shutil.disk_usage(root if root.exists() else root.parent).free
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
        accounts=accounts,
    )


def _platform_env(plan: Plan) -> list[str]:
    lines: list[str] = []
    for platform in all_platforms():
        try:
            module = platform_hook(platform, HOOK_INSTANCE_ENV)
        except MissingPlatformHook:
            continue
        ports = {port.name: plan.ports[port.name] for port in platform.instance_ports}
        lines.extend(module.instance_env(plan.idx, ports))
    return lines


def _env_body(plan: Plan) -> str:
    lines = [
        f"# 实例 {plan.instance_id} ({plan.label})",
        f"CLONOTH_PORT={plan.ports['supervisor']}",
        *_platform_env(plan),
    ]
    if plan.prefix:
        lines.append(f"CLONOTH_URL_PREFIX={plan.prefix.strip('/')}")
    return "\n".join(lines) + "\n"


def scaffold(plan: Plan, workspace_root: Path) -> None:
    """建目录骨架。到这一步为止全都在 kazebot 权限内，失败不会留下半个服务。"""
    repo = _repo_root()
    (plan.workspace / "data").mkdir(parents=True, exist_ok=True)
    (plan.workspace / "config").mkdir(parents=True, exist_ok=True)
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

    env_file = plan.workspace / ".env"
    if not env_file.exists():
        env_file.write_text(_env_body(plan), encoding="utf-8")
        env_file.chmod(0o600)

    # 先由 kazebot 建出来，root 之后只 append —— 属主保持不变，控制台才读得到后续进度。
    log_path(workspace_root, plan.instance_id).write_text(
        "工作区就绪，等 root 侧接手…\n", encoding="utf-8",
    )

    save_instance(
        workspace_root, instance_id=plan.instance_id, label=plan.label, idx=plan.idx, accounts=plan.accounts,
    )


async def _systemctl(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def start_unit(unit: str) -> None:
    """--no-block：oneshot 单元会跑上一两分钟，不能把请求挂在这里。"""
    code, out = await _systemctl("start", "--no-block", unit)
    if code != 0:
        raise ProvisionError(f"启动 {unit} 失败（polkit 规则和 root 侧脚本是最新的吗？）：{out}")


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
    """给前端轮询。脚本最后一行写 DONE，据此判成败，不必去读 journal。"""
    instance_id = validate_instance_id(instance_id)
    lines = read_log(log_path(workspace_root, instance_id))
    done = next((line for line in reversed(lines) if line.startswith("DONE")), "")
    return {
        "id": instance_id,
        "lines": lines,
        "finished": bool(done),
        "ok": done.startswith("DONE ok"),
        "detail": done[len("DONE fail:"):].strip() if done.startswith("DONE fail:") else "",
    }


def forget(workspace_root: Path, instance_id: str) -> None:
    """从清单摘掉。工作区由 root 侧改名归档，这里不碰文件。"""
    remove_instance(workspace_root, validate_instance_id(instance_id))


def guard_removable(workspace_root: Path, instance_id: str, *, current_prefix: str) -> dict[str, Any]:
    """不许删自己，也不许删清单外的实例 —— 后者意味着参数是猜出来的。"""
    instance_id = validate_instance_id(instance_id)
    rows = load_instances(workspace_root)
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
    "REMOVE_UNIT",
    "RESERVED_IDS",
    "data_root",
    "forget",
    "guard_removable",
    "log_path",
    "normalize_accounts",
    "plan_instance",
    "progress",
    "scaffold",
    "start_unit",
    "unit_state",
    "validate_instance_id",
]
