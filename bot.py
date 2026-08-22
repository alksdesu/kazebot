"""QQ Bot 启动入口：NoneBot2 宿主 + OneBot 11 适配器 + Clonoth 插件。

需先启动 Supervisor（python main.py），本进程只负责 QQ 侧收发。
NapCat 的反向 WebSocket 应指向 ws://<host>:<port>/onebot/v11/ws。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# nonebot 的 .env 只进它自己的 Config 对象，而 adapters.onebot 在 import 期读 os.environ，
# 不先灌进来白名单会静默为空。cwd 优先：多实例共用一份代码，端口与工作区只能靠各自那份
# .env 区分；找不到才回落代码目录，单实例照旧。
_LOCAL_ENV = Path.cwd() / ".env"
load_dotenv(_LOCAL_ENV if _LOCAL_ENV.is_file() else _REPO_ROOT / ".env")

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


def _bridge_stdlib_logging() -> None:
    """把插件与 SDK 的标准 logging 接到 loguru。

    两边都用 logging.getLogger 打日志，而 NoneBot 只配置 loguru：这些 logger 没有
    handler，propagate 到 root 后由 lastResort 兜底，等于 INFO 全部消失、只有 WARNING
    以上漏得出来。审批归属、路由决策这类关键线索都打在 INFO 上，丢了就没法排障。
    """
    from nonebot.log import LoguruHandler

    for name in ("nonebot.plugin.clonoth_agent", "clonoth_sdk"):
        target = logging.getLogger(name)
        target.handlers = [LoguruHandler()]
        target.setLevel(logging.INFO)
        target.propagate = False


def main() -> None:
    nonebot.init()
    _bridge_stdlib_logging()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("adapters.onebot")
    nonebot.run()


if __name__ == "__main__":
    main()
