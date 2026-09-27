"""平台账号运维的约定：登录、切号、扫码由平台模块实现，supervisor 只转发、翻译错误、记审计。

平台在描述的 hooks 里声明 account_ops 模块，模块提供 account_ops(workspace_root) 返回下面这个协议的实现。
"""
from __future__ import annotations

from typing import Protocol, TypedDict


class AccountOpsError(RuntimeError):
    """失败原因可以直接展示给人。"""


class AccountUnreachable(AccountOpsError):
    """平台那头还没应答。换号重启和首次部署都会经过这里，不是故障。"""


class AccountRequestInvalid(AccountOpsError):
    """请求本身不对，比如账号的写法不合这个平台。"""


class AccountConflict(AccountOpsError):
    """眼下的状态做不了这件事，比如没登录任何号时要钉自动登录。"""


class QuickLoginTarget(TypedDict, total=False):
    """登录过、不用扫码就能切回去的号。"""

    account: str
    nick: str
    avatar: str
    # 平台自报能免扫码登录不可信，真切一次被拒过的记 False，控制台置灰。
    available: bool
    dead_reason: str


class AccountStatus(TypedDict, total=False):
    account: str
    nick: str
    online: bool
    is_login: bool
    login_error: str
    # 等扫码时一并带出，控制台不必再单发一次请求去撞重启窗口。
    qrcode: str
    quick_login: list[QuickLoginTarget]


class SwitchResult(TypedDict):
    ok: bool
    account: str
    # 重启后会不会自动登回来。号已经切过去了，这一步没成不算切换失败。
    pinned: bool
    pin_error: str


class AccountOps(Protocol):
    @property
    def configured(self) -> bool:
        """连平台后台要的凭据配没配。没配时控制台只提示去配，不去连。"""

    async def status(self) -> AccountStatus:
        """当前账号与可以直接切过去的号。"""

    async def switch(self, account: str) -> SwitchResult:
        """切到一个登录过的号。"""

    async def login_qrcode(self) -> str:
        """扫码登录用的二维码内容，拿不到给空串。"""

    async def relogin(self) -> None:
        """让平台停在等扫码的状态。"""

    async def pin(self) -> str:
        """把当前在线的号钉成重启后自动登录，返回这个号。"""
