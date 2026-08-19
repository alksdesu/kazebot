"""QQ Bot 启动入口：NoneBot2 宿主 + OneBot 11 适配器 + Clonoth 插件。

需先启动 Supervisor（python main.py），本进程只负责 QQ 侧收发。
NapCat 的反向 WebSocket 应指向 ws://<host>:<port>/onebot/v11/ws。
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# nonebot 的 .env 只进它自己的 Config 对象，而 adapters.onebot 在 import 期读 os.environ，
# 不先灌进来白名单会静默为空。按文件位置取而不是 cwd，换工作目录启动也读得到。
load_dotenv(_REPO_ROOT / ".env")

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


def main() -> None:
    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("adapters.onebot")
    nonebot.run()


if __name__ == "__main__":
    main()
