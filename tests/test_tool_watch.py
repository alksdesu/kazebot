"""config/tools 和 config/mcp.yaml 的指纹，以及 engine 每轮来问的重载序号。

共享组里别的实例、控制台、手改动了脚本工具或 MCP 配置，这边没人通知；engine 每轮都来问 reload-seq，
序号变了才跟上。扫目录的频率得压住，但一次改动必须恰好报一次。
"""
from __future__ import annotations

import asyncio
import functools
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import supervisor.api as supervisor_api  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
import supervisor.tool_watch as tool_watch  # noqa: E402
import toolbox.mcp_runtime as mcp_runtime  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.share_groups import _make_dir_link  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.tool_watch import McpConfigWatch, ToolTreeWatch  # noqa: E402
from toolbox.reload_seqs import ReloadSeqs  # noqa: E402

_TOKEN = "test-tool-watch-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

_SCRIPT = "SPEC = {'name': 'alpha'}\n"
_MCP = "clients:\n  fs:\n    command: npx\n"


def _tool(root: Path, rel: str, text: str = _SCRIPT) -> Path:
    path = root / "config" / "tools" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _mcp(root: Path, text: str = _MCP) -> Path:
    """按生产里的保存路径写 config/mcp.yaml：同目录临时文件替换，每次换 inode。"""
    path = config_layout.mcp_path(root)
    config_layout.write_config_text(path, text)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _tool(tmp_path, "alpha.py")
    return tmp_path


@pytest.fixture
def watch(root: Path) -> ToolTreeWatch:
    return ToolTreeWatch(root, min_interval=0.0)


class TestWhatCountsAsAChange:
    def test_an_untouched_tree_never_reports(self, watch: ToolTreeWatch) -> None:
        assert watch.changed() is False
        assert watch.changed() is False

    def test_a_new_tool_reports_once(self, watch: ToolTreeWatch, root: Path) -> None:
        _tool(root, "beta.py")

        assert watch.changed() is True
        assert watch.changed() is False

    def test_an_edited_tool_reports_once(self, watch: ToolTreeWatch, root: Path) -> None:
        _tool(root, "alpha.py", "SPEC = {'name': 'alpha', 'description': '改过'}\n")

        assert watch.changed() is True
        assert watch.changed() is False

    def test_a_removed_tool_reports_once(self, watch: ToolTreeWatch, root: Path) -> None:
        (root / "config" / "tools" / "alpha.py").unlink()

        assert watch.changed() is True
        assert watch.changed() is False

    def test_a_same_size_replacement_in_the_same_tick_is_noticed(
        self, watch: ToolTreeWatch, root: Path,
    ) -> None:
        """保存是换文件落盘：同一个时钟刻度里写进等长内容时，只有 inode 认得出来。

        Windows 的系统时钟约 15 毫秒跳一次，连着保存两次拿到的是同一个 mtime。
        """
        path = root / "config" / "tools" / "alpha.py"
        before = path.stat()
        replacement = "SPEC = {'name': 'beta_'}\n"
        assert len(replacement) == len(_SCRIPT)

        config_layout.write_config_text(path, replacement)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        after = path.stat()
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
        assert after.st_ino != before.st_ino
        assert watch.changed() is True
        assert watch.changed() is False

    def test_a_tool_in_a_subdirectory_counts(self, watch: ToolTreeWatch, root: Path) -> None:
        _tool(root, "group/nested.py")

        assert watch.changed() is True

    def test_compiled_caches_are_ignored(self, watch: ToolTreeWatch, root: Path) -> None:
        # 每次导入都会重写 __pycache__，认它就等于每轮都让 engine 重载一次。
        _tool(root, "__pycache__/alpha.cpython-312.py")
        _tool(root, "group/__pycache__/nested.cpython-312.py")

        assert watch.changed() is False

    def test_a_file_that_is_not_a_script_is_ignored(self, watch: ToolTreeWatch, root: Path) -> None:
        (root / "config" / "tools" / "notes.txt").write_text("闲话", encoding="utf-8")

        assert watch.changed() is False

    def test_a_missing_tools_directory_is_not_a_change(self, tmp_path: Path) -> None:
        watch = ToolTreeWatch(tmp_path, min_interval=0.0)

        assert watch.changed() is False

    def test_the_directory_appearing_later_is_a_change(self, tmp_path: Path) -> None:
        watch = ToolTreeWatch(tmp_path, min_interval=0.0)

        _tool(tmp_path, "alpha.py")

        assert watch.changed() is True
        assert watch.changed() is False

    def test_the_directory_going_away_is_a_change(self, watch: ToolTreeWatch, root: Path) -> None:
        import shutil

        shutil.rmtree(root / "config" / "tools")

        assert watch.changed() is True
        assert watch.changed() is False


class TestThrottling:
    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        now = [1000.0]
        monkeypatch.setattr(tool_watch, "time", SimpleNamespace(monotonic=lambda: now[0]))
        return now

    def test_a_change_within_the_interval_is_not_reported_yet(self, root: Path, clock: list[float]) -> None:
        watch = ToolTreeWatch(root, min_interval=2.0)
        _tool(root, "beta.py")

        clock[0] += 1.0

        assert watch.changed() is False

    def test_it_surfaces_once_the_interval_has_passed(self, root: Path, clock: list[float]) -> None:
        watch = ToolTreeWatch(root, min_interval=2.0)
        _tool(root, "beta.py")
        clock[0] += 1.0
        assert watch.changed() is False

        clock[0] += 1.5

        assert watch.changed() is True

    def test_a_throttled_poll_does_not_consume_the_change(self, root: Path, clock: list[float]) -> None:
        watch = ToolTreeWatch(root, min_interval=2.0)
        _tool(root, "beta.py")
        for _ in range(5):
            clock[0] += 0.1
            assert watch.changed() is False

        clock[0] += 10.0

        assert watch.changed() is True
        clock[0] += 10.0
        assert watch.changed() is False


class TestTheResolvedDirectory:
    def test_swapping_config_for_a_group_link_counts_as_a_change(self, root: Path, tmp_path: Path) -> None:
        """换了共享组等于整套工具都换了，哪怕两边文件一模一样。"""
        original = root / "config" / "tools" / "alpha.py"
        group = tmp_path / "group"
        (group / "config" / "tools").mkdir(parents=True)
        twin = group / "config" / "tools" / "alpha.py"
        twin.write_text(_SCRIPT, encoding="utf-8")
        # 连修改时间都对齐：只剩目录指向哪里这一处不同。
        os.utime(twin, ns=(original.stat().st_atime_ns, original.stat().st_mtime_ns))
        watch = ToolTreeWatch(root, min_interval=0.0)
        assert watch.changed() is False

        os.rename(root / "config", tmp_path / "config.archived")
        _make_dir_link(root / "config", group / "config")

        assert watch.changed() is True
        assert watch.changed() is False


class TestTheMcpConfigFile:
    @pytest.fixture
    def mcp_watch(self, tmp_path: Path) -> McpConfigWatch:
        _mcp(tmp_path)
        return McpConfigWatch(tmp_path, min_interval=0.0)

    def test_an_untouched_file_never_reports(self, mcp_watch: McpConfigWatch) -> None:
        assert mcp_watch.changed() is False
        assert mcp_watch.changed() is False

    def test_an_edit_reports_once(self, mcp_watch: McpConfigWatch, tmp_path: Path) -> None:
        _mcp(tmp_path, "clients:\n  fs:\n    command: npx\n    args: [--other]\n")

        assert mcp_watch.changed() is True
        assert mcp_watch.changed() is False

    def test_a_same_size_save_in_the_same_tick_is_noticed(self, mcp_watch: McpConfigWatch, tmp_path: Path) -> None:
        path = config_layout.mcp_path(tmp_path)
        before = path.stat()
        replacement = "clients:\n  fs:\n    command: npm\n"
        assert len(replacement) == len(_MCP)

        _mcp(tmp_path, replacement)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        after = path.stat()
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
        assert after.st_ino != before.st_ino
        assert mcp_watch.changed() is True
        assert mcp_watch.changed() is False

    def test_removing_the_file_reports_once(self, mcp_watch: McpConfigWatch, tmp_path: Path) -> None:
        config_layout.mcp_path(tmp_path).unlink()

        assert mcp_watch.changed() is True
        assert mcp_watch.changed() is False

    def test_a_missing_file_is_not_a_change_until_it_appears(self, tmp_path: Path) -> None:
        watch = McpConfigWatch(tmp_path, min_interval=0.0)
        assert watch.changed() is False

        _mcp(tmp_path)

        assert watch.changed() is True
        assert watch.changed() is False

    def test_a_broken_save_is_a_change_like_any_other(self, mcp_watch: McpConfigWatch, tmp_path: Path) -> None:
        # 写坏也得让各 worker 知道：它们自己决定沿用上一份，修好那次同样要报
        _mcp(tmp_path, "clients: [unclosed\n")
        assert mcp_watch.changed() is True

        _mcp(tmp_path)
        assert mcp_watch.changed() is True

    def test_every_version_it_sees_is_read_so_a_later_breakage_is_reported_as_kept(self, tmp_path: Path) -> None:
        """supervisor 自己不调 MCP，诊断却要说清 worker 在用哪份：它得像 worker 一样读过每一版能用的。"""
        root = tmp_path / "ws"
        watch = McpConfigWatch(root, min_interval=0.0)
        _mcp(root)
        assert watch.changed() is True

        _mcp(root, "clients: [unclosed\n")
        assert watch.changed() is True

        assert mcp_runtime.config_error(root).startswith(f"{config_layout.MCP_FILE} 用不了，沿用上一份能用的")

    def test_swapping_config_for_a_group_link_counts_even_when_the_file_is_the_same(self, tmp_path: Path) -> None:
        """换了共享组就是换了一份配置。两边是同一个文件的硬链接时 file_stamp 一模一样，只剩解析到哪里不同。"""
        root = tmp_path / "ws"
        original = _mcp(root)
        group = tmp_path / "group"
        (group / "config").mkdir(parents=True)
        try:
            os.link(original, group / "config" / "mcp.yaml")
        except OSError as exc:
            pytest.skip(f"这个文件系统建不了硬链接：{exc}")
        watch = McpConfigWatch(root, min_interval=0.0)

        os.rename(root / "config", tmp_path / "config.archived")
        _make_dir_link(root / "config", group / "config")

        assert config_layout.file_stamp(config_layout.mcp_path(root)) == config_layout.file_stamp(
            tmp_path / "config.archived" / "mcp.yaml"
        )
        assert watch.changed() is True
        assert watch.changed() is False


# ---------------------------------------------------------------------------
#  GET /v1/tools/reload-seq、POST /v1/tools/reload
# ---------------------------------------------------------------------------

@pytest.fixture
def api(root: Path, monkeypatch: pytest.MonkeyPatch):
    # 真的两个 watch，只是把扫描间隔压到 0：engine 的轮询频率不是这里要测的东西。
    monkeypatch.setattr(supervisor_api, "ToolTreeWatch", functools.partial(ToolTreeWatch, min_interval=0.0))
    monkeypatch.setattr(supervisor_api, "McpConfigWatch", functools.partial(McpConfigWatch, min_interval=0.0))
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=root,
            eventlog=EventLog(root / "data" / "events.jsonl", run_id="run-tool-watch"),
            policy=PolicyEngine(workspace_root=root),
        ),
        process_manager=None,
        config_store=ConfigStore(path=root / "config" / "models.yaml", workspace_root=root),
    )

    def call(method: str, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=_AUTH if headers is None else headers, **kwargs)

        return asyncio.run(go())

    return call


def _seqs(api: Any) -> ReloadSeqs:
    response = api("GET", "/v1/tools/reload-seq")
    assert response.status_code == 200, response.text
    seqs = ReloadSeqs.from_payload(response.json())
    assert seqs is not None, response.text
    return seqs


def _seq(api: Any) -> int:
    return _seqs(api).scripts


class TestTheReloadSeqEndpoint:
    def test_polling_an_untouched_tree_keeps_the_seq(self, api: Any) -> None:
        first = _seq(api)

        assert _seq(api) == first
        assert _seq(api) == first

    def test_a_new_tool_bumps_the_seq_once(self, api: Any, root: Path) -> None:
        first = _seq(api)

        _tool(root, "beta.py")

        bumped = _seq(api)
        assert bumped > first
        assert _seq(api) == bumped

    def test_an_edit_bumps_the_seq(self, api: Any, root: Path) -> None:
        first = _seq(api)

        _tool(root, "alpha.py", "SPEC = {'name': 'alpha', 'description': '改过'}\n")

        assert _seq(api) > first

    def test_a_removal_bumps_the_seq(self, api: Any, root: Path) -> None:
        first = _seq(api)

        (root / "config" / "tools" / "alpha.py").unlink()

        assert _seq(api) > first

    def test_a_same_size_save_in_the_same_tick_bumps_the_seq(self, api: Any, root: Path) -> None:
        # 控制台连着存两次工具、第二次只改了一个字符：engine 靠序号变了才知道要重载。
        path = root / "config" / "tools" / "alpha.py"
        before = path.stat()
        first = _seq(api)

        config_layout.write_config_text(path, "SPEC = {'name': 'beta_'}\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert _seq(api) > first

    def test_a_compiled_cache_does_not_bump_the_seq(self, api: Any, root: Path) -> None:
        first = _seq(api)

        _tool(root, "__pycache__/alpha.cpython-312.py")

        assert _seq(api) == first


class TestTheMcpSequences:
    def test_the_answer_carries_this_runs_id_and_every_counter(self, api: Any) -> None:
        # run_id 换了 engine 才知道 supervisor 重启过、序号从头数了
        assert _seqs(api) == ReloadSeqs(run_id="run-tool-watch", scripts=0, mcp_config=0, mcp_reload=0)

    def test_an_mcp_config_edit_bumps_only_its_own_counter_once(self, api: Any, root: Path) -> None:
        first = _seqs(api)

        _mcp(root)

        bumped = _seqs(api)
        assert bumped.mcp_config > first.mcp_config
        assert (bumped.scripts, bumped.mcp_reload) == (first.scripts, first.mcp_reload)
        assert _seqs(api) == bumped

    def test_a_script_edit_leaves_the_mcp_counters_alone(self, api: Any, root: Path) -> None:
        first = _seqs(api)

        _tool(root, "beta.py")

        bumped = _seqs(api)
        assert bumped.scripts > first.scripts
        assert (bumped.mcp_config, bumped.mcp_reload) == (first.mcp_config, first.mcp_reload)

    @pytest.mark.parametrize("body", [None, {}, {"mcp": False}])
    def test_a_reload_request_without_mcp_bumps_only_the_scripts(self, api: Any, body: Any) -> None:
        first = _seqs(api)

        response = api("POST", "/v1/tools/reload", json=body)

        assert response.status_code == 200, response.text
        answered = ReloadSeqs.from_payload(response.json())
        assert response.json()["ok"] is True
        assert answered == ReloadSeqs(
            run_id=first.run_id, scripts=first.scripts + 1, mcp_config=first.mcp_config, mcp_reload=first.mcp_reload,
        )
        assert _seqs(api) == answered

    def test_a_reload_request_with_mcp_also_bumps_the_explicit_mcp_counter(self, api: Any) -> None:
        first = _seqs(api)

        response = api("POST", "/v1/tools/reload", json={"mcp": True})

        assert response.status_code == 200, response.text
        assert ReloadSeqs.from_payload(response.json()) == ReloadSeqs(
            run_id=first.run_id, scripts=first.scripts + 1, mcp_config=first.mcp_config,
            mcp_reload=first.mcp_reload + 1,
        )

    def test_a_reload_request_without_the_admin_token_bumps_nothing(self, api: Any) -> None:
        first = _seqs(api)

        response = api("POST", "/v1/tools/reload", json={"mcp": True}, headers={})

        assert response.status_code == 401
        assert _seqs(api) == first

    def test_an_unreadable_mcp_switch_is_refused_instead_of_guessed(self, api: Any) -> None:
        first = _seqs(api)

        response = api("POST", "/v1/tools/reload", json={"mcp": "sometimes"})

        assert response.status_code == 422
        assert _seqs(api) == first
