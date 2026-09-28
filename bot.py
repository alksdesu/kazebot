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

# nonebot 的 .env 只进它自己的 Config 对象，而 adapters.onebot.config 在 import 期读 os.environ，
# 不先灌进来白名单会静默为空。cwd 优先：多实例共用一份代码，端口与工作区只能靠各自那份
# .env 区分；找不到才回落代码目录，单实例照旧。
_LOCAL_ENV = Path.cwd() / ".env"
load_dotenv(_LOCAL_ENV if _LOCAL_ENV.is_file() else _REPO_ROOT / ".env")

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# supervisor 冷启动约需半分钟，留足余量；等不到也照常起，卡死更难查。
_SUPERVISOR_WAIT_SEC = 180.0
# 与 adapters/onebot/config.py 认同一组变量名。这里不 import 那个模块：本文件会被单独
# 复制出去做入口冒烟，import 期要求整个仓库在位就跑不起来。
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


def main() -> None:
    # 延迟到这里再取：本文件会被单独复制出去做入口冒烟，import 期要求整个仓库在位
    # 就跑不起来。
    from clonoth_runtime import SECRET_FROM_CONFIG, platform_secret, wait_supervisor
    from platforms.builtin import QQ_NAPCAT
    from workspace import resolve_workspace_root

    _require_qq_instance()

    # 先等 supervisor。systemd 的 After/BindsTo 只排启动顺序，不等它 ready，而
    # NoneBot 一 run 起来就开始收 QQ 消息 —— 那十几秒里的 submit inbound 全部失败，
    # 每次重启都静默吞掉几条群消息。
    wait_supervisor(_SUPERVISOR_URL, label="qq", max_wait_sec=_SUPERVISOR_WAIT_SEC)

    # NoneBot 只从环境变量读连接令牌，写在 data/config.yaml 的那份得显式交给它。
    access = platform_secret(resolve_workspace_root(_REPO_ROOT), QQ_NAPCAT, "access_token")
    nonebot.init(**({"onebot_access_token": access.value} if access.source == SECRET_FROM_CONFIG else {}))
    _bridge_stdlib_logging()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("adapters.onebot.onebot_plugin")
    nonebot.run()


if __name__ == "__main__":
    main()
