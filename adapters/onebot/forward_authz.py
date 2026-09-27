"""qq_forward 投递鉴权的纯判定逻辑。

不 import NoneBot，使权限规则在缺少平台依赖的环境下也能回归测试。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from platforms.builtin import QQ_NAPCAT
from platforms.ids import same_id
from platforms.protection import outbound_deny_reason

from .config import CLONOTH_WORKSPACE

CROSS_SESSION_DENIED = (
    "只有 Clonoth 管理员可以把内容发送给其他 QQ 用户或群。可以改为发给自己或发到本群。"
)
FILE_PATHS_DENIED = "只有 Clonoth 管理员可以把工作区里的文件发送到 QQ。"


def file_deny_reason(rel_path: str) -> str:
    """命中外传黑名单时返回拒绝原因，空串表示允许。

    黑名单由各平台声明的密钥与私密文件推导。经 QQ 外传即离开本机、过腾讯服务器且不可撤回，
    所以对管理员也拒绝：需要时走 read_file 审批流，顺带挡住管理员在群里被提示词注入骗走密钥。
    """
    return outbound_deny_reason(rel_path, via=QQ_NAPCAT.short_name, workspace=Path(CLONOTH_WORKSPACE))


def has_explicit_file_paths(raw_file_paths: Any) -> bool:
    """判断 op=file 是否给了显式路径（区别于只用 use_recent 发刚生成的图）。"""
    if isinstance(raw_file_paths, list):
        return any(str(item or "").strip() for item in raw_file_paths)
    return bool(str(raw_file_paths or "").strip())


def target_is_origin(
    *,
    target_type: str,
    target_id: Any,
    origin_user_id: Any,
    origin_group_id: Any,
) -> bool:
    """目标是否就是请求来源本人或本群。

    按解析后的真实 id 比对而非 target_type 字面值：用户写“发到群 12345”而 12345
    正是本群时应放行，反之写 target_type=self 也不能绕过。
    """
    kind = str(target_type or "").strip().lower()
    if kind == "private":
        return same_id(target_id, origin_user_id)
    if kind == "group":
        return same_id(target_id, origin_group_id)
    return False


def delivery_deny_reason(
    *,
    is_admin: bool,
    target_type: str,
    target_id: Any,
    origin_user_id: Any,
    origin_group_id: Any,
) -> str:
    """跨会话投递鉴权；空串表示允许。

    跨会话投递等于借 Bot 身份向任意人/群发消息，只有管理员可用；非管理员仍可把
    内容发回自己或本群，那些内容他本来就看得到。
    """
    if is_admin:
        return ""
    if target_is_origin(
        target_type=target_type,
        target_id=target_id,
        origin_user_id=origin_user_id,
        origin_group_id=origin_group_id,
    ):
        return ""
    return CROSS_SESSION_DENIED


def file_paths_deny_reason(*, is_admin: bool, raw_file_paths: Any) -> str:
    """op=file 显式路径鉴权；空串表示允许。

    显式路径 = 读取工作区任意文件并外传，与 read_file 对非管理员的敏感路径限制
    同级；use_recent 只发 Bot 自己刚生成的附件，不受此限。
    """
    if is_admin or not has_explicit_file_paths(raw_file_paths):
        return ""
    return FILE_PATHS_DENIED
