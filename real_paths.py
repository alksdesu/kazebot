"""路径解析到底后只认一种写法。

途经的目录或文件正被别的进程建出来、删掉时，CPython 在 Windows 上会把扩展长度前缀留在 resolve 的结果里。
"""
from __future__ import annotations

from pathlib import Path

_EXTENDED_PREFIX = "\\\\?\\"
_EXTENDED_UNC_PREFIX = _EXTENDED_PREFIX + "UNC\\"


def real_path(path: Path | str, *, strict: bool = False) -> Path:
    """resolve 跟着软链走到头，再去掉调用方没写的扩展长度前缀，同一位置只给一种写法，拿去比归属才比得上。"""
    original = Path(path)
    resolved = original.resolve(strict=strict)
    drive = resolved.drive
    if original.drive.startswith(_EXTENDED_PREFIX) or not drive.startswith(_EXTENDED_PREFIX):
        return resolved
    if drive.startswith(_EXTENDED_UNC_PREFIX):
        plain = "\\\\" + drive[len(_EXTENDED_UNC_PREFIX):]
    elif len(drive) == len(_EXTENDED_PREFIX) + 2 and drive.endswith(":"):
        plain = drive[len(_EXTENDED_PREFIX):]
    else:
        # 卷 GUID 这类没有盘符的路径离了前缀就不能用，原样给回。
        return resolved
    return Path(plain + str(resolved)[len(drive):])
