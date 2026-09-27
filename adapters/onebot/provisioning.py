"""多开建新实例时 QQ 这一侧写进 .env 的几行：NoneBot 监听、转发桥端口、NapCat WebUI 地址与令牌。"""
from __future__ import annotations

import secrets
from typing import Mapping


def instance_env(index: int, ports: Mapping[str, int]) -> list[str]:
    lines = [
        # 序号 0 的 NapCat 跑 --network host，回环就够；其余用 bridge，容器过来是外部地址，
        # 只听回环等于永远收不到反向 WS。开放监听就必须配 token。
        "HOST=127.0.0.1" if index == 0 else "HOST=0.0.0.0",
        f"PORT={ports['bot']}",
        f"ONEBOT_FORWARD_BRIDGE_PORT={ports['bridge']}",
        f"NAPCAT_WEBUI_URL=http://127.0.0.1:{ports['napcat']}",
        # 容器首启才会生成，由 root 侧读出来回填。
        "NAPCAT_WEBUI_TOKEN=",
    ]
    if index != 0:
        lines.append(f"ONEBOT_ACCESS_TOKEN={secrets.token_hex(24)}")
    return lines
