"""适配器 live 状态的约定：心跳、连接、账号、积压条数怎么读，文件怎么写。"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platforms import live  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import PlatformDescriptor  # noqa: E402
from platforms.live import (  # noqa: E402
    STALE_AFTER_SEC,
    file_fingerprint,
    live_state_path,
    live_status,
    read_live_state,
    read_live_status,
    same_file,
    write_live_state,
)

_NOW = 1_800_000_000.0
_PLATFORMS_DIR = Path(__file__).resolve().parents[1] / "platforms"
FRESHCHAT = PlatformDescriptor(id="freshchat", display_name="Fresh", short_name="F")


def _status(**fields: Any) -> live.LiveStatus:
    return live_status({"published_at": _NOW, **fields}, now=_NOW)


def _repo_top_level_modules() -> set[str]:
    root = _PLATFORMS_DIR.parent
    return {p.stem for p in root.glob("*.py")} | {p.name for p in root.iterdir() if (p / "__init__.py").is_file()}


def _non_stdlib_imports() -> list[tuple[str, str]]:
    """platforms/*.py 里 import 的非标准库模块，(文件名, 模块名)。"""
    found: list[tuple[str, str]] = []
    for path in sorted(_PLATFORMS_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            found.extend(
                (path.name, name) for name in names
                if name.split(".")[0] not in sys.stdlib_module_names and name != "__future__"
            )
    return found


class TestHeartbeat:
    def test_nothing_published_is_neither_fresh_nor_possibly_running(self) -> None:
        status = live_status({}, now=_NOW)

        assert (status.published, status.fresh, status.may_be_running, status.age_sec) == (False, False, False, None)
        assert status.stale is False

    @pytest.mark.parametrize("age, fresh", [(0, True), (STALE_AFTER_SEC, True), (STALE_AFTER_SEC + 0.01, False)])
    def test_the_heartbeat_window_is_three_beats(self, age: float, fresh: bool) -> None:
        status = live_status({"published_at": _NOW - age}, now=_NOW)

        assert (status.fresh, status.may_be_running, status.stale) == (fresh, fresh, not fresh)
        assert status.age_sec == round(age, 3)

    def test_a_heartbeat_from_the_future_is_not_trusted_but_not_ruled_out(self) -> None:
        # 显示上不认它，要求 bot 停着的操作也不能当它停了。
        status = live_status({"published_at": _NOW + 3600, "connected": True}, now=_NOW)

        assert (status.fresh, status.may_be_running, status.connected) == (False, True, False)
        assert status.age_sec == -3600

    @pytest.mark.parametrize("stamp", [None, "", "soon", True, 0, -5, float("inf"), float("nan"), [1], {"at": 1}])
    def test_an_unreadable_timestamp_is_no_heartbeat(self, stamp: Any) -> None:
        status = live_status({"published_at": stamp}, now=_NOW)

        assert status.published is True
        assert (status.age_sec, status.fresh, status.may_be_running, status.stale) == (None, False, False, True)

    def test_a_numeric_string_still_counts(self) -> None:
        assert live_status({"published_at": str(_NOW - 5)}, now=_NOW).fresh is True

    def test_the_clock_defaults_to_now(self) -> None:
        import time

        assert live_status({"published_at": time.time()}).fresh is True


class TestLinkFacts:
    def test_connected_needs_a_fresh_heartbeat_and_a_real_true(self) -> None:
        assert _status(connected=True).connected is True
        assert _status(connected="yes").connected is False
        assert _status(connected=1).connected is False
        assert live_status({"published_at": _NOW - STALE_AFTER_SEC - 1, "connected": True}, now=_NOW).connected is False

    @pytest.mark.parametrize("raw, account", [(" 10001 ", "10001"), (10001, "10001"), (True, ""), (None, ""), (["1"], "")])
    def test_the_account_is_text(self, raw: Any, account: str) -> None:
        assert _status(account=raw).account == account

    def test_the_account_is_kept_even_when_the_bot_went_quiet(self) -> None:
        # 归属查询要显示「bot 上次用的是哪个号」，心跳断了也照样有用。
        assert live_status({"published_at": _NOW - 3600, "account": "10001"}, now=_NOW).account == "10001"

    @pytest.mark.parametrize("raw, backlog", [
        (3, 3), ("4", 4), (2.9, 2), (-1, 0), (True, 0), (None, 0), ("many", 0), (float("inf"), 0), ([1], 0),
    ])
    def test_the_backlog_is_a_non_negative_count(self, raw: Any, backlog: int) -> None:
        assert _status(backlog=raw).backlog == backlog

    def test_config_facts_are_read_by_shape(self) -> None:
        loaded = {"exists": True, "mtime_ns": 1, "size": 2}
        status = _status(config_path=" /w/config/qq.yaml ", loaded=loaded, parse_error=" ParserError ")

        assert (status.config_path, status.config_loaded, status.parse_error) == ("/w/config/qq.yaml", loaded, "ParserError")
        broken = _status(config_path=7, loaded=[1], parse_error=None)
        assert (broken.config_path, broken.config_loaded, broken.parse_error) == ("", None, "")

    def test_a_non_mapping_reads_as_unpublished(self) -> None:
        assert live_status(["published_at"], now=_NOW).published is False  # type: ignore[arg-type]


class TestTheFile:
    def test_new_platforms_keep_it_in_their_data_directory(self, tmp_path: Path) -> None:
        assert live_state_path(FRESHCHAT, tmp_path) == tmp_path / "data" / "platforms" / "freshchat" / "live.json"
        # QQ 升级前就在用老文件名。
        assert live_state_path(QQ_NAPCAT, tmp_path) == tmp_path / "data" / "qq_live_state.json"

    @pytest.mark.parametrize("content", [None, "{broken", "[1, 2]", "\"text\""])
    def test_a_missing_or_odd_file_is_nothing_published(self, tmp_path: Path, content: str | None) -> None:
        path = live_state_path(FRESHCHAT, tmp_path)
        if content is not None:
            path.parent.mkdir(parents=True)
            path.write_text(content, encoding="utf-8")

        assert read_live_state(FRESHCHAT, tmp_path) == {}
        assert read_live_status(FRESHCHAT, tmp_path).published is False

    def test_what_is_written_reads_back(self, tmp_path: Path) -> None:
        path = live_state_path(FRESHCHAT, tmp_path)
        payload = {"published_at": _NOW, "connected": True, "account": "bot", "backlog": 2, "名字": "群"}

        write_live_state(path, payload)

        assert read_live_state(FRESHCHAT, tmp_path) == payload
        text = path.read_text(encoding="utf-8")
        assert text == json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        status = read_live_status(FRESHCHAT, tmp_path, now=_NOW)
        assert (status.fresh, status.connected, status.account, status.backlog) == (True, True, "bot", 2)
        assert list(path.parent.glob(f"{path.name}.*")) == []

    def test_a_write_that_dies_halfway_leaves_the_old_file_and_no_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # supervisor 随时在读，读到半份就当 bot 没公布。
        path = live_state_path(FRESHCHAT, tmp_path)
        write_live_state(path, {"published_at": _NOW})
        before = path.read_text(encoding="utf-8")
        real_write = Path.write_text

        def dying_write(self: Path, text: str, *args: Any, **kwargs: Any) -> int:
            real_write(self, text[: len(text) // 2], *args, **kwargs)
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", dying_write)
        with pytest.raises(OSError):
            write_live_state(path, {"published_at": _NOW + 1, "connected": True})
        monkeypatch.undo()

        assert path.read_text(encoding="utf-8") == before
        assert list(path.parent.glob(f"{path.name}.*")) == []


class TestFingerprint:
    def test_a_missing_file_has_an_empty_fingerprint(self, tmp_path: Path) -> None:
        assert file_fingerprint(tmp_path / "nope.yaml") == {"exists": False, "mtime_ns": 0, "size": 0}

    def test_an_existing_file_reports_its_version(self, tmp_path: Path) -> None:
        path = tmp_path / "qq.yaml"
        path.write_text("a: 1\n", encoding="utf-8")
        stat = path.stat()

        assert file_fingerprint(path) == {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


class TestSameFile:
    def test_another_spelling_of_the_same_path_is_the_same_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "config" / "qq.yaml"
        monkeypatch.chdir(tmp_path)

        assert same_file("data/../config/qq.yaml", target)
        assert same_file(str(target), target)

    def test_a_different_file_is_not(self, tmp_path: Path) -> None:
        assert not same_file(str(tmp_path / "config" / "other.yaml"), tmp_path / "config" / "qq.yaml")

    @pytest.mark.parametrize("reported", [None, "", "  ", 7])
    def test_nothing_reported_is_not_the_same_file(self, tmp_path: Path, reported: Any) -> None:
        assert not same_file(reported, tmp_path / "config" / "qq.yaml")

    @pytest.mark.skipif(os.name != "nt", reason="只有 Windows 的路径不分大小写")
    def test_case_does_not_matter_where_the_filesystem_ignores_it(self, tmp_path: Path) -> None:
        target = tmp_path / "config" / "qq.yaml"

        assert same_file(str(target).upper(), target)


class TestTheContract:
    def test_the_counters_that_move_every_tick_are_left_out_of_change_detection(self) -> None:
        assert live.VOLATILE_FIELDS == {"published_at", "backlog"}
        assert not {live.CONNECTED, live.ACCOUNT} & live.VOLATILE_FIELDS

    def test_a_bot_is_given_up_on_after_three_missed_beats(self) -> None:
        assert STALE_AFTER_SEC == 3 * live.HEARTBEAT_SEC

    def test_the_platforms_package_imports_nothing_but_the_standard_library(self) -> None:
        # 核心、工具和各平台适配器都从这里认平台，牵出任何第三方库都会让某一方装不起来。
        # 仓库自己的顶层模块不算：它们跟着同一份代码走，不会缺。
        first_party = _repo_top_level_modules()
        offenders = [
            f"{filename}: {name}" for filename, name in _non_stdlib_imports()
            if name.split(".")[0] not in first_party
        ]
        assert offenders == []

    def test_the_only_first_party_import_is_the_config_layout(self) -> None:
        """放开仓库自己的模块之后，这条钉住实际放进来的是哪几个，别顺手再拖进半个引擎。"""
        assert {name.split(".")[0] for _filename, name in _non_stdlib_imports()} == {"config_layout"}
