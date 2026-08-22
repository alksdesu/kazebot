"""日志的只读访问：出站脱敏与路径边界。

日志里进过明文密钥不是假设 —— 写日志的地方太多，漏一处就是密钥进浏览器。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.log_access import MAX_LINES, LogReader, redact  # noqa: E402


class TestRedaction:
    @pytest.mark.parametrize("secret", [
        "sk-proj-abcdefghijklmnop",
        "sk-ant-api03-XYZ123456789",
        "AIzaSyD-1234567890abcdefg",
        "ghp_abcdefghijklmnopqrst",
        "xoxb-1234-5678-abcdefghij",
    ])
    def test_known_key_shapes_are_masked(self, secret: str) -> None:
        out = redact(f"[llm] using key {secret} now")
        assert secret not in out
        assert secret[-4:] in out  # 留个尾巴对号

    @pytest.mark.parametrize("line", [
        "api_key=relay-abcdefghij",
        "api_key: relay-abcdefghij",
        'api_key="relay-abcdefghij"',
        "API-KEY = relay-abcdefghij",
        "token: relay-abcdefghij",
        "Authorization: relay-abcdefghij",
        "password=relay-abcdefghij",
    ])
    def test_key_value_shapes_are_masked(self, line: str) -> None:
        # 中转站的 key 没有固定前缀，只能靠键名认。
        assert "relay-abcdefghij" not in redact(line)

    def test_keys_in_query_strings_are_masked(self) -> None:
        # Gemini 原生接口就是这么传 key 的。
        out = redact("POST https://generativelanguage.googleapis.com/v1?key=AIzaSecretValue1")
        assert "AIzaSecretValue1" not in out
        assert "generativelanguage.googleapis.com" in out

    def test_several_secrets_on_one_line_all_go(self) -> None:
        out = redact("sk-aaaaaaaaaaaa then sk-bbbbbbbbbbbb")
        assert "sk-aaaaaaaaaaaa" not in out
        assert "sk-bbbbbbbbbbbb" not in out

    def test_ordinary_text_is_left_alone(self) -> None:
        line = "[engine] worker engine-1 ready (max_concurrent=4)"
        assert redact(line) == line

    def test_urls_and_model_names_survive(self) -> None:
        # 脱敏过头就没法排障了：地址和模型名正是要看的东西。
        line = "[llm] POST https://relay.example/v1/chat/completions model=gemini-3-pro"
        assert redact(line) == line

    def test_a_short_value_is_not_partly_revealed(self) -> None:
        assert "abc" not in redact("token=abcdef")


class TestListing:
    def test_only_log_files_show_up(self, tmp_path: Path) -> None:
        (tmp_path / "engine-1.log").write_text("a", encoding="utf-8")
        (tmp_path / ".admin_token").write_text("secret", encoding="utf-8")
        (tmp_path / "config.yaml").write_text("b", encoding="utf-8")

        assert [row.name for row in LogReader(tmp_path).list()] == ["engine-1.log"]

    def test_newest_first(self, tmp_path: Path) -> None:
        import os
        for name, mtime in (("old.log", 1000), ("new.log", 2000)):
            path = tmp_path / name
            path.write_text("x", encoding="utf-8")
            os.utime(path, (mtime, mtime))

        assert [row.name for row in LogReader(tmp_path).list()] == ["new.log", "old.log"]

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert LogReader(tmp_path / "nope").list() == []


class TestPathBoundary:
    @pytest.fixture
    def reader(self, tmp_path: Path) -> LogReader:
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "engine-1.log").write_text("inside\n", encoding="utf-8")
        (tmp_path / "secret.log").write_text("outside\n", encoding="utf-8")
        (tmp_path / ".admin_token").write_text("tok\n", encoding="utf-8")
        return LogReader(logs)

    @pytest.mark.parametrize("name", [
        "../secret.log",
        "..\\secret.log",
        "../../etc/passwd",
        "sub/engine-1.log",
        "/etc/passwd",
        "",
    ])
    def test_escaping_the_directory_is_refused(self, reader: LogReader, name: str) -> None:
        assert reader.tail(name) is None

    @pytest.mark.parametrize("name", ["config.yaml", ".admin_token", "engine-1.log.bak"])
    def test_non_log_files_are_refused(self, reader: LogReader, name: str) -> None:
        assert reader.tail(name) is None

    def test_a_symlink_pointing_outside_is_refused(self, reader: LogReader, tmp_path: Path) -> None:
        # 名字合法、后缀合法，只有 resolve() 之后才看得出它指到外面去了。
        link = reader.log_dir / "sneaky.log"
        try:
            link.symlink_to(tmp_path / "secret.log")
        except (OSError, NotImplementedError):
            pytest.skip("这个环境建不了符号链接")
        assert reader.tail("sneaky.log") is None

    def test_a_real_log_reads_fine(self, reader: LogReader) -> None:
        assert reader.tail("engine-1.log") == ("inside", False)


class TestTail:
    def _reader(self, tmp_path: Path, body: str) -> LogReader:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "a.log").write_text(body, encoding="utf-8")
        return LogReader(tmp_path)

    def test_returns_the_last_n_lines(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path, "\n".join(str(i) for i in range(100)))
        text, truncated = reader.tail("a.log", lines=10)
        assert text.splitlines() == [str(i) for i in range(90, 100)]
        assert truncated is True

    def test_a_short_file_is_not_reported_as_truncated(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path, "one\ntwo\n")
        assert reader.tail("a.log", lines=10) == ("one\ntwo", False)

    def test_the_line_count_is_capped(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path, "\n".join(str(i) for i in range(MAX_LINES + 500)))
        text, _ = reader.tail("a.log", lines=99999)
        assert len(text.splitlines()) == MAX_LINES

    def test_a_huge_file_is_read_from_the_tail_only(self, tmp_path: Path) -> None:
        # 整个读进来的话 cleanup.log 这种会把内存和响应体一起撑爆。
        reader = self._reader(tmp_path, ("x" * 200 + "\n") * 20000)
        text, truncated = reader.tail("a.log", lines=50)
        assert truncated is True
        assert len(text) < 200 * 60

    def test_the_output_is_redacted(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path, "[llm] key sk-abcdefghijklmnop ready\n")
        text, _ = reader.tail("a.log")
        assert "sk-abcdefghijklmnop" not in text

    def test_undecodable_bytes_do_not_blow_up(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "a.log").write_bytes(b"ok\n\xff\xfe bad\n")
        text, _ = LogReader(tmp_path).tail("a.log")
        assert "ok" in text
