"""data/memory 的目录名算法只此一份。

算错一个字节，写进去的记忆下一轮就读不回来；认错一次，整理任务会被当成活跃会话再提取一遍。
"""
from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.memory_namespace import (  # noqa: E402
    CONVERSATION_PREFIX,
    SUBJECT_PREFIX,
    conversation_namespace,
    is_memory_namespace,
    is_valid_subject,
    subject_from_namespace,
    subject_namespace,
)

_KEY = "qq_group:123456789"
_HEX = "0123456789abcdef01234567"


def _digest(raw: str) -> str:
    return CONVERSATION_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class TestConversationNamespace:
    @pytest.mark.parametrize("empty", ["", "   ", "\t\n", None, 0, False])
    def test_nothing_in_nothing_out(self, empty: Any) -> None:
        # 空串拼进 data/memory 就是根目录本身，调用方靠这个空串判断「没有归属」。
        assert conversation_namespace(empty) == ""

    def test_the_digest_is_the_first_24_hex_of_sha256(self) -> None:
        assert conversation_namespace(_KEY) == _digest(_KEY)

    def test_surrounding_whitespace_does_not_change_the_directory(self) -> None:
        assert conversation_namespace(f"  {_KEY}\n") == conversation_namespace(_KEY)

    @pytest.mark.parametrize("namespace", [
        pytest.param(CONVERSATION_PREFIX + _HEX, id="会话目录"),
        pytest.param(SUBJECT_PREFIX + "UserA", id="人物档案目录"),
    ])
    def test_a_namespace_comes_back_as_itself(self, namespace: str) -> None:
        # dream 以要整理的目录名当 conversation_key 建任务，再算一次就落到别的目录 ——
        # 人物档案那次会落进一个没人认领的 conv_ 目录里。
        assert conversation_namespace(namespace) == namespace

    @pytest.mark.parametrize("namespace", [
        pytest.param(CONVERSATION_PREFIX + _HEX, id="会话目录"),
        pytest.param(SUBJECT_PREFIX + "UserA", id="人物档案目录"),
    ])
    def test_a_namespace_with_whitespace_still_comes_back_bare(self, namespace: str) -> None:
        assert conversation_namespace(f" {namespace} ") == namespace

    @pytest.mark.parametrize("lookalike", [
        pytest.param(CONVERSATION_PREFIX + _HEX.upper(), id="大写 hex"),
        pytest.param(CONVERSATION_PREFIX + _HEX[:23], id="23 位"),
        pytest.param(CONVERSATION_PREFIX + _HEX + "0", id="25 位"),
        pytest.param(CONVERSATION_PREFIX + "z" * 24, id="不是 hex"),
        pytest.param(SUBJECT_PREFIX + "1abc", id="别名数字开头"),
        pytest.param(SUBJECT_PREFIX + "a-b", id="别名带连字符"),
        pytest.param(SUBJECT_PREFIX, id="只有 user 前缀"),
        pytest.param(SUBJECT_PREFIX + "x" * 65, id="别名 65 个字符"),
        pytest.param("users_UserA", id="前缀差一个字母"),
    ])
    def test_something_that_only_looks_like_one_gets_hashed(self, lookalike: str) -> None:
        assert conversation_namespace(lookalike) == _digest(lookalike)

    @pytest.mark.parametrize("value", [12345, 3.5, ("qq_group", 1), {"key": "value"}])
    def test_a_non_string_key_is_hashed_by_its_text(self, value: Any) -> None:
        assert conversation_namespace(value) == _digest(str(value).strip())

    def test_two_different_keys_never_share_a_directory(self) -> None:
        assert conversation_namespace("qq_group:1") != conversation_namespace("qq_group:2")


class TestSubject:
    @pytest.mark.parametrize("subject", ["UserA", "UserAF", "GroupB", "a", "A_1", "x" * 64])
    def test_an_alias_from_the_anonymiser_is_accepted(self, subject: str) -> None:
        assert is_valid_subject(subject) is True
        assert subject_namespace(subject) == SUBJECT_PREFIX + subject

    @pytest.mark.parametrize("bad", [
        pytest.param("../x", id="向上穿越"),
        pytest.param("a/b", id="带路径分隔"),
        pytest.param("1abc", id="数字开头"),
        pytest.param("_abc", id="下划线开头"),
        pytest.param("x" * 65, id="65 个字符"),
        pytest.param("", id="空"),
        pytest.param("   ", id="全是空白"),
        pytest.param("User A", id="中间有空格"),
        pytest.param("User-A", id="带连字符"),
        pytest.param("用户甲", id="非 ASCII"),
        pytest.param(None, id="None"),
    ])
    def test_anything_else_gets_no_directory(self, bad: Any) -> None:
        # 别名直接拼进目录名，放宽一个字符就能越出 data/memory。
        assert is_valid_subject(bad) is False
        assert subject_namespace(bad) == ""

    def test_the_alias_survives_a_round_trip(self) -> None:
        assert subject_from_namespace(subject_namespace("UserA")) == "UserA"

    @pytest.mark.parametrize("bad", [
        pytest.param(f"{SUBJECT_PREFIX} UserA", id="前缀后有空格"),
        pytest.param(SUBJECT_PREFIX, id="只有前缀"),
        pytest.param(f"{SUBJECT_PREFIX}1abc", id="数字开头"),
        pytest.param(f"{CONVERSATION_PREFIX}{_HEX}", id="会话目录"),
        pytest.param("UserA", id="没有前缀"),
        pytest.param("", id="空"),
    ])
    def test_a_directory_that_is_not_a_profile_yields_no_alias(self, bad: str) -> None:
        assert subject_from_namespace(bad) == ""


class TestIsMemoryNamespace:
    @pytest.mark.parametrize("value", [
        CONVERSATION_PREFIX + _HEX,
        SUBJECT_PREFIX + "UserB",
        SUBJECT_PREFIX + "A",
    ])
    def test_both_shapes_are_recognised(self, value: str) -> None:
        assert is_memory_namespace(value) is True

    @pytest.mark.parametrize("value", [
        pytest.param("qq_group:abc", id="真人会话键"),
        pytest.param("system:dream", id="内部会话键"),
        pytest.param(CONVERSATION_PREFIX, id="只有 conv 前缀"),
        pytest.param(SUBJECT_PREFIX, id="只有 user 前缀"),
        pytest.param(CONVERSATION_PREFIX + _HEX[:23], id="23 位 hex"),
        pytest.param(CONVERSATION_PREFIX + _HEX + "0", id="25 位 hex"),
        pytest.param(CONVERSATION_PREFIX + _HEX.upper(), id="大写 hex"),
        pytest.param(f"{SUBJECT_PREFIX}1abc", id="别名数字开头"),
        pytest.param(f"{SUBJECT_PREFIX}a-b", id="别名带连字符"),
        pytest.param(f"users_{'UserB'}", id="前缀差一个字母"),
        pytest.param("", id="空"),
        pytest.param(None, id="None"),
    ])
    def test_anything_else_is_a_real_conversation(self, value: Any) -> None:
        assert is_memory_namespace(value) is False

    def test_every_key_it_computes_is_one_of_its_own(self) -> None:
        assert is_memory_namespace(conversation_namespace(_KEY))
        assert is_memory_namespace(subject_namespace("UserA"))


_SKIP_PARTS = {".venv", "node_modules", "tests", "__pycache__", ".git", ".ruff_cache", "data"}
_SKIP_TREES = (Path("adapters") / "web" / "frontend",)
_HOME = _ROOT / "engine" / "memory_namespace.py"
_FORBIDDEN_EXACT = {CONVERSATION_PREFIX, SUBJECT_PREFIX}
_FORBIDDEN_CONVERSATION = re.compile(CONVERSATION_PREFIX + "[0-9a-fA-F]")
# 别名一律大写开头（UserA / GroupB）；写成 [A-Za-z] 会把满仓库的 user_id、user_text 一起算进来。
_FORBIDDEN_SUBJECT = re.compile(SUBJECT_PREFIX + "[A-Z]")
# 手抄的正则：前缀后面紧跟字符类/分组/转义。被合并的那三份重复正好都长这样，上面两条一个也抓不到。
_FORBIDDEN_PATTERN = re.compile(rf"(?:{CONVERSATION_PREFIX}|{SUBJECT_PREFIX})[\[(\\]")


def _product_sources() -> list[Path]:
    files: list[Path] = []
    for path in _ROOT.rglob("*.py"):
        relative = path.relative_to(_ROOT)
        if _SKIP_PARTS & set(relative.parts) or path == _HOME:
            continue
        if any(relative.is_relative_to(tree) for tree in _SKIP_TREES):
            continue
        files.append(path)
    return files


def _literals(tree: ast.AST) -> list[str]:
    """所有字符串常量，包括 f-string 里写死的那几段。"""
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _offenders(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    return [
        text for text in _literals(tree)
        if text in _FORBIDDEN_EXACT
        or _FORBIDDEN_CONVERSATION.search(text)
        or _FORBIDDEN_SUBJECT.search(text)
        or _FORBIDDEN_PATTERN.search(text)
    ]


class TestOnlyOneCopyOfTheAlgorithm:
    """前缀散落在各处就会各写各的判定：有人认 conv_ 不认 user_，清理就漏掉半边。"""

    def test_no_product_module_spells_the_prefixes_itself(self) -> None:
        offenders = {
            str(path.relative_to(_ROOT)).replace("\\", "/"): sorted(set(found))
            for path in _product_sources()
            if (found := _offenders(path))
        }

        assert offenders == {}

    def test_the_guard_would_catch_a_second_copy(self, tmp_path: Path) -> None:
        # 规则本身写歪了就是一条永远绿的测试。后三行是被合并掉的那三份重复的原样写法。
        copy = tmp_path / "copy.py"
        copy.write_text(
            'PREFIX = "conv_"\nSUBJECT = "user_"\n'
            'SAMPLE = "conv_0123456789abcdef01234567"\nPROFILE = f"user_{1}"\nRE = r"user_A"\n'
            'NS_RE = r"^conv_[0-9a-f]{24}$"\n'
            'TASK_RE = r"(?:conv_[0-9a-f]{24}|user_[A-Za-z][A-Za-z0-9_]{0,63})"\n'
            'ESCAPED = r"conv_\\d+"\n',
            encoding="utf-8",
        )

        assert set(_offenders(copy)) == {
            "conv_",
            "user_",
            "conv_0123456789abcdef01234567",
            "user_A",
            "^conv_[0-9a-f]{24}$",
            "(?:conv_[0-9a-f]{24}|user_[A-Za-z][A-Za-z0-9_]{0,63})",
            r"conv_\d+",
        }

    def test_the_guard_leaves_ordinary_text_alone(self, tmp_path: Path) -> None:
        # 这几种蛇形字段名满仓库都是，判据把它们算进来就只能靠白名单活着。
        ordinary = tmp_path / "ordinary.py"
        ordinary.write_text(
            'A = "conversation_key"\nB = "user_id"\nC = "memory_user_ids"\nD = "cooldown_user_sec"\n'
            'E = "conv_key"\nF = "PRAGMA user_version"\nG = "user_language"\n',
            encoding="utf-8",
        )

        assert _offenders(ordinary) == []

    def test_it_actually_scans_the_repository(self) -> None:
        # 目录过滤写错就会扫出空集，上面那条断言跟着永远成立。
        scanned = {str(path.relative_to(_ROOT)).replace("\\", "/") for path in _product_sources()}

        assert "engine/memory_subjects.py" in scanned
        assert "supervisor/conversation_labels.py" in scanned
        assert "engine/memory_namespace.py" not in scanned
        assert not any(part in {".venv", "tests"} for name in scanned for part in name.split("/"))
