"""QQ Bot 启动入口：NoneBot2 宿主 + OneBot 11 适配器 + Clonoth 插件。

需先启动 Supervisor（python main.py），本进程只负责 QQ 侧收发。
NapCat 的反向 WebSocket 应指向 ws://<host>:<port>/onebot/v11/ws。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_instance_env() -> None:
    """灌本实例工作区的 .env，进程环境里已有的不覆盖。

    nonebot 的 .env 只进它自己的 Config 对象，而 adapters.onebot.config 在 import 期读 os.environ，
    不先灌进来白名单会静默为空。env 没指定工作区时先灌 cwd 下那份、没有再灌本文件旁边那份，工作区从里面认；
    指定了就只认那个工作区自己的 .env，缺了也不借代码目录那份：多开时那是主实例的端口和令牌。
    """
    import clonoth_runtime
    from workspace import resolve_workspace_root, workspace_env_root

    seeded = False
    if workspace_env_root() is None:
        local = Path.cwd() / ".env"
        candidate = local if local.is_file() else _REPO_ROOT / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            seeded = True
    root = resolve_workspace_root(_REPO_ROOT)
    if clonoth_runtime.load_workspace_env(root) is None and not seeded:
        logging.getLogger(__name__).warning("[qq] %s 下没有 .env：端口、令牌这些实例身份只剩进程环境里那些", root)


_load_instance_env()

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# supervisor 冷启动约需半分钟，留足余量；等不到也照常起，卡死更难查。
_SUPERVISOR_WAIT_SEC = 180.0
# 与 adapters/onebot/config.py 认同一组变量名。
_SUPERVISOR_URL = (
    os.environ.get("CLONOTH_BASE_URL")
    or os.environ.get("CLONOTH_SUPERVISOR_URL")
    or "http://127.0.0.1:8765"
)


def _bridge_stdlib_logging() -> None:
    """把插件、共享适配器层与 SDK 的标准 logging 接到 loguru。

    几处都用 logging.getLogger 打日志，而 NoneBot 只配置 loguru：这些 logger 没有
    handler，propagate 到 root 后由 lastResort 兜底，等于 INFO 全部消失、只有 WARNING
    以上漏得出来。审批归属、路由决策这类关键线索都打在 INFO 上，丢了就没法排障。
    """
    from nonebot.log import LoguruHandler

    # adapters 兜住按模块名取 logger 的那批：共享层和适配器根目录的兄弟模块；stickers 是表情包库。
    for name in ("nonebot.plugin.clonoth_agent", "adapters", "clonoth_sdk", "stickers"):
        target = logging.getLogger(name)
        target.handlers = [LoguruHandler()]
        target.setLevel(logging.INFO)
        target.propagate = False


def _require_qq_instance() -> None:
    """一个实例只跑一个平台。声明的不是 QQ 就别起：两个平台的进程挂在同一个工作区，会话和记忆会混成一份。"""
    from platforms.builtin import QQ_NAPCAT
    from platforms.registry import PLATFORM_ENV, UnknownPlatform, current_platform
    from workspace import CONFIG_ERROR_EXIT_CODE

    try:
        platform = current_platform()
    except UnknownPlatform as exc:
        logging.getLogger(__name__).error("[qq] %s；改 .env 里的 %s 后再启动", exc, PLATFORM_ENV)
        sys.exit(CONFIG_ERROR_EXIT_CODE)
    if platform.id != QQ_NAPCAT.id:
        logging.getLogger(__name__).error(
            "[qq] 这个实例声明跑的是 %s（%s=%s），bot.py 只接 QQ", platform.display_name, PLATFORM_ENV, platform.id,
        )
        sys.exit(CONFIG_ERROR_EXIT_CODE)


def _ensure_config_layout() -> None:
    """改造前的平台令牌在 data/config.yaml 里：搬家没做完就读，连接令牌是空的。supervisor 正在搬时这里等它的锁。"""
    import config_layout
    from workspace import CONFIG_ERROR_EXIT_CODE, resolve_workspace_root

    try:
        config_layout.ensure_layout(resolve_workspace_root(_REPO_ROOT))
    except Exception:
        logging.getLogger(__name__).exception("[qq] 配置搬进 config/ 失败，先修好再启动")
        sys.exit(CONFIG_ERROR_EXIT_CODE)


def main() -> None:
    # 用到时才取：入口冒烟是加载完本文件之后才换掉 wait_supervisor 的。
    from clonoth_runtime import SECRET_FROM_CONFIG, platform_secret, wait_supervisor
    from platforms.builtin import QQ_NAPCAT
    from workspace import resolve_workspace_root

    _require_qq_instance()
    _ensure_config_layout()

    # 先等 supervisor。systemd 的 After/BindsTo 只排启动顺序，不等它 ready，而
    # NoneBot 一 run 起来就开始收 QQ 消息 —— 那十几秒里的 submit inbound 全部失败，
    # 每次重启都静默吞掉几条群消息。
    wait_supervisor(_SUPERVISOR_URL, label="qq", max_wait_sec=_SUPERVISOR_WAIT_SEC)

    # NoneBot 只从环境变量读连接令牌，写在 data/platform_secrets.yaml 的那份得显式交给它。
    access = platform_secret(resolve_workspace_root(_REPO_ROOT), QQ_NAPCAT, "access_token")
    nonebot.init(**({"onebot_access_token": access.value} if access.source == SECRET_FROM_CONFIG else {}))
    _bridge_stdlib_logging()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("adapters.onebot.onebot_plugin")
    nonebot.run()


if __name__ == "__main__":
    main()
