"""表情包库不能被定时清理扫掉。

库里的图是长期资产，而清理器按 mtime 删、不看扩展名、还递归收空目录。
现在保护它的只是"没被点名"这一条，谁往任务表里加一行就没了 —— 所以钉在这里。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SOURCE = (_ROOT / "engine" / "data_cleanup.py").read_text(encoding="utf-8")
# 任务表里每个 purge_dir 的目标，形如 DATA_DIR / "attachments"
_TARGETS = re.compile(r'DATA_DIR\s*/\s*"([a-z_]+)"')


def _swept_dirs() -> set[str]:
    return set(_TARGETS.findall(_SOURCE))


def test_stickers_is_not_on_the_sweep_list() -> None:
    assert "stickers" not in _swept_dirs()


def test_the_data_root_itself_is_never_swept() -> None:
    # 对 data/ 整体做 purge_dir 会把库连同别的长期数据一起按龄删光。
    assert not re.search(r"purge_dir\(\s*DATA_DIR\s*[,)]", _SOURCE)
    assert not re.search(r"directory\s*=\s*DATA_DIR\s*[,)]", _SOURCE)


def test_the_sweep_list_is_still_an_allowlist() -> None:
    # 遍历未知子目录，图库就会连带被收走。data 根下的 glob 是白名单前缀，另有专测。
    for hostile in ("DATA_DIR.iterdir()", "DATA_DIR.rglob("):
        assert hostile not in _SOURCE, f"{hostile} 会把没点名的目录也扫进来"


def test_temp_globs_cannot_reach_the_library() -> None:
    """data 根下按通配删文件的那条，pattern 放宽一点就会扫到库里。"""
    from fnmatch import fnmatch

    from engine.data_cleanup import TEMP_GLOBS

    for pattern in TEMP_GLOBS:
        assert not fnmatch("stickers", pattern), f"{pattern} 命中了表情包库目录"
        assert not fnmatch("stickers.sqlite3", pattern), f"{pattern} 命中了库文件"


def test_qq_cache_keywords_do_not_match_the_library() -> None:
    """QQ 内部缓存那条按目录名关键词匹配，命中就按 7 天删。"""
    from engine.data_cleanup import QQ_CACHE_DIR_KEYWORDS

    assert not any(word in "stickers" for word in QQ_CACHE_DIR_KEYWORDS)


@pytest.mark.parametrize("subdir", ["library", "pending", "backups"])
def test_library_subdirs_are_not_swept_either(subdir: str) -> None:
    assert subdir not in _swept_dirs()
