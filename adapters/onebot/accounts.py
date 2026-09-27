"""NapCat 在工作区里留下的、登录过的 QQ 号。"""
from __future__ import annotations

from pathlib import Path


def known_accounts(workspace: Path) -> list[str]:
    """NapCat 给每个登录过的号留一份 onebot11_<QQ号>.json。部署脚本把 NapCat 装在工作区外面，那样的部署这里找不到。"""
    config_dir = Path(workspace) / "napcat" / "config"
    try:
        names = [path.stem.rsplit("_", 1)[-1] for path in config_dir.glob("onebot11_*.json")]
    except OSError:
        return []
    return sorted(name for name in names if name.isdigit())
