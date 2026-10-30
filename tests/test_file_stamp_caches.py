"""共享配置文件变没变，全按同一个指纹判：config_layout.file_stamp。

保存都是同目录临时文件替换，每次换 inode。共享组里别的实例在同一个时钟刻度里写进等长内容时，
修改时间和大小都认不出来 —— 这里每个用例都把 mtime 拨回改动之前，只留 inode 这一处不同。
Windows 的系统时钟约 15 毫秒跳一次，连着保存两次本来就会拿到同一个 mtime。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
import engine.node as node_module  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
import toolbox.mcp_runtime as mcp_runtime  # noqa: E402
from adapters._shared.live_config import BOOL, LiveConfig, LiveKey, int_in  # noqa: E402
from engine.conversation_store import (  # noqa: E402
    ConversationChangedError,
    ConversationStore,
    Message,
)
from engine.node import canonical_node_id  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402

_TOKEN = "test-file-stamp-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _write(path: Path, text: str) -> Path:
    # 按字节写：Windows 上 write_text 会把 LF 换成 CRLF，等长替换就对不上了。
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _rewrite(path: Path, text: str) -> Path:
    """换内容并把 mtime 拨回去。长度可以不同，用在只关心「换过了」的地方。"""
    before = path.stat()
    config_layout.write_config_text(path, text)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    return path


def _same_tick_replace(path: Path, text: str) -> Path:
    """按生产里的保存路径换掉内容，再把 mtime 拨回去：只剩 inode 认得出来。"""
    before = path.stat()
    assert len(text.encode("utf-8")) == before.st_size, "换上的内容得和原来一样长"
    config_layout.write_config_text(path, text)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    assert after.st_ino != before.st_ino
    return path


# ---------------------------------------------------------------------------
#  指纹本身
# ---------------------------------------------------------------------------

class TestFileStamp:
    def test_it_is_time_size_and_inode(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.yaml", "k: 1\n")
        info = path.stat()

        assert config_layout.file_stamp(path) == (info.st_mtime_ns, info.st_size, info.st_ino)

    def test_a_missing_file_has_no_stamp(self, tmp_path: Path) -> None:
        assert config_layout.file_stamp(tmp_path / "nope.yaml") is None

    def test_reading_it_twice_without_touching_the_file_is_stable(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.yaml", "k: 1\n")

        assert config_layout.file_stamp(path) == config_layout.file_stamp(path)

    def test_a_same_size_replacement_in_the_same_tick_differs(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.yaml", "k: 1\n")
        before = config_layout.file_stamp(path)

        _same_tick_replace(path, "k: 2\n")

        after = config_layout.file_stamp(path)
        assert after != before
        # 前两项一模一样，认出来靠的只有第三项。
        assert after[:2] == before[:2]


# ---------------------------------------------------------------------------
#  策略
# ---------------------------------------------------------------------------

_POLICY_AUTO = "version: 1\nextra_roots: []\nread_file:\n  default: auto\n  rules: []\n"
_POLICY_DENY = "version: 1\nextra_roots: []\nread_file:\n  default: deny\n  rules: []\n"


class TestThePolicyEngine:
    def test_a_same_tick_rewrite_by_another_instance_takes_effect(self, tmp_path: Path) -> None:
        path = _write(config_layout.policy_path(tmp_path), _POLICY_AUTO)
        engine = PolicyEngine(workspace_root=tmp_path, policy_path=path)
        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.auto

        _same_tick_replace(path, _POLICY_DENY)

        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.deny

    def test_it_goes_back_the_other_way_too(self, tmp_path: Path) -> None:
        path = _write(config_layout.policy_path(tmp_path), _POLICY_DENY)
        engine = PolicyEngine(workspace_root=tmp_path, policy_path=path)
        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.deny

        _same_tick_replace(path, _POLICY_AUTO)

        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.auto

    def test_an_untouched_file_is_not_reparsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import supervisor.policy as policy_module

        path = _write(config_layout.policy_path(tmp_path), _POLICY_AUTO)
        engine = PolicyEngine(workspace_root=tmp_path, policy_path=path)
        engine.evaluate_read_file(path="notes.txt")
        counted, calls = _counting(policy_module._load_policy_bytes)
        monkeypatch.setattr(policy_module, "_load_policy_bytes", counted)

        # 每一次工具调用都判一遍，指纹没变就不该再解析一次 yaml。
        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.auto
        assert calls == [0]

        _same_tick_replace(path, _POLICY_DENY)

        assert engine.evaluate_read_file(path="notes.txt").safety_level == SafetyLevel.deny
        assert calls == [1]


_POLICY_ROOTS = "version: 1\nextra_roots: ['{root}']\nread_file:\n  default: auto\n  rules: []\n"


@pytest.fixture
def engine_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clonoth_runtime, "_POLICY_CONFIG", clonoth_runtime._POLICY_CONFIG.fresh())


def _policy_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage() for record in caplog.records
        if record.name == clonoth_runtime._log.name and config_layout.POLICY_FILE in record.getMessage()
    ]


@pytest.mark.usefixtures("engine_policy")
class TestTheEngineCopyOfThePolicy:
    """engine 和工具读 policy.yaml 是为了 extra_roots：收哪一版、坏了用哪份都得和 PolicyEngine 一样。

    以前读坏就按空算：写坏期间审批还认 extra_roots，文件工具却把那里当成外部路径，多要一次审批，项目说明也找不过去。
    """

    def _seed(self, workspace: Path, root: str = "/srv/shared") -> Path:
        return _write(config_layout.policy_path(workspace), _POLICY_ROOTS.format(root=root))

    def test_a_broken_rewrite_keeps_the_last_extra_roots(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        path = self._seed(tmp_path)
        assert clonoth_runtime.load_policy_config(tmp_path)["extra_roots"] == ["/srv/shared"]

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            _rewrite(path, "version: 1\nextra_roots: [/srv/sk-SECRET-root\n")
            for _ in range(3):
                data = clonoth_runtime.load_policy_config(tmp_path)

        assert data["extra_roots"] == ["/srv/shared"]
        [warning] = _policy_warnings(caplog)
        assert "沿用上一份能用的 extra_roots" in warning
        assert "YAML 解析失败：第 " in warning
        assert "sk-SECRET-root" not in warning

    def test_a_file_the_policy_engine_rejects_is_rejected_here_too(self, tmp_path: Path) -> None:
        # 解析得了、结构不对：PolicyEngine 整份不收，这里照收的话两边的 extra_roots 就分家了。
        path = self._seed(tmp_path)
        engine = PolicyEngine(workspace_root=tmp_path, policy_path=path)
        clonoth_runtime.load_policy_config(tmp_path)

        _rewrite(path, "version: 1\nextra_roots: ['/srv/elsewhere']\nread_file:\n  default: maybe\n  rules: []\n")
        ours = clonoth_runtime.load_policy_config(tmp_path)

        assert "用不了" in engine.load_error
        assert ours["extra_roots"] == ["/srv/shared"]
        assert clonoth_runtime.parse_extra_roots(tmp_path, ours["extra_roots"]) == engine.extra_roots()

    def test_a_file_broken_from_the_start_is_the_built_in_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from supervisor.policy import default_policy

        _write(config_layout.policy_path(tmp_path), "extra_roots: [oops\n")
        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            data = clonoth_runtime.load_policy_config(tmp_path)

        assert data["extra_roots"] == default_policy()["extra_roots"]
        [warning] = _policy_warnings(caplog)
        assert "按内置默认策略的 extra_roots 算" in warning
        assert "上一份" not in warning

    @pytest.mark.parametrize("make", ["directory", "not-utf8"])
    def test_an_unreadable_file_is_named_without_its_path_or_the_decoder_wording(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, make: str,
    ) -> None:
        path = config_layout.policy_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if make == "directory":
            path.mkdir()
        else:
            path.write_bytes(b"version: 1\n# \xff\n")

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            clonoth_runtime.load_policy_config(tmp_path)

        [warning] = _policy_warnings(caplog)
        assert "用不了，文件工具和项目说明按内置默认策略的 extra_roots 算：读不了：" in warning
        assert str(tmp_path) not in warning
        assert "codec" not in warning
        if make == "not-utf8":
            assert warning.endswith("不是 UTF-8 编码（第 14 个字节）")

    def test_an_empty_file_counts_as_unusable_like_in_the_policy_engine(self, tmp_path: Path) -> None:
        path = self._seed(tmp_path)
        clonoth_runtime.load_policy_config(tmp_path)

        _rewrite(path, "")

        assert clonoth_runtime.load_policy_config(tmp_path)["extra_roots"] == ["/srv/shared"]

    def test_a_deleted_file_keeps_the_last_extra_roots(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        path = self._seed(tmp_path)
        clonoth_runtime.load_policy_config(tmp_path)

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            path.unlink()
            first = clonoth_runtime.load_policy_config(tmp_path)
            second = clonoth_runtime.load_policy_config(tmp_path)

        assert first["extra_roots"] == second["extra_roots"] == ["/srv/shared"]
        [warning] = _policy_warnings(caplog)
        assert "不见了" in warning

    def test_a_file_that_was_never_there_is_the_built_in_default_quietly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Supervisor 首启时照内置默认建出这份文件，engine 这边就按同一份算，不用为此告警。
        from supervisor.policy import default_policy

        with caplog.at_level(logging.WARNING, logger=clonoth_runtime._log.name):
            data = clonoth_runtime.load_policy_config(tmp_path)

        assert data == default_policy()
        assert _policy_warnings(caplog) == []

    def test_fixing_the_file_takes_effect_again(self, tmp_path: Path) -> None:
        path = self._seed(tmp_path)
        clonoth_runtime.load_policy_config(tmp_path)
        _rewrite(path, "extra_roots: [oops\n")
        clonoth_runtime.load_policy_config(tmp_path)

        _rewrite(path, _POLICY_ROOTS.format(root="/srv/moved"))

        assert clonoth_runtime.load_policy_config(tmp_path)["extra_roots"] == ["/srv/moved"]

    def test_callers_get_their_own_copy(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        clonoth_runtime.load_policy_config(tmp_path)["extra_roots"].append("/tampered")

        assert clonoth_runtime.load_policy_config(tmp_path)["extra_roots"] == ["/srv/shared"]

    def test_an_untouched_file_is_not_reparsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import supervisor.policy as policy_module

        path = self._seed(tmp_path, "/srv/aaaaaa")
        assert clonoth_runtime.load_policy_config(tmp_path)["extra_roots"] == ["/srv/aaaaaa"]
        counted, calls = _counting(policy_module.parse_policy_text)
        monkeypatch.setattr(policy_module, "parse_policy_text", counted)

        # 文件工具每解析一个路径都要问一次，指纹没变就不该再解析一次 yaml。
        assert clonoth_runtime.load_policy_config(tmp_path)["extra_roots"] == ["/srv/aaaaaa"]
        assert calls == [0]

        _same_tick_replace(path, _POLICY_ROOTS.format(root="/srv/bbbbbb"))

        assert clonoth_runtime.load_policy_config(tmp_path)["extra_roots"] == ["/srv/bbbbbb"]
        assert calls == [1]

    def test_the_file_tools_keep_the_extra_root_inside_while_the_file_is_broken(self, tmp_path: Path) -> None:
        from toolbox._common import resolve_and_classify

        workspace = tmp_path / "ws"
        extra = tmp_path / "shared"
        extra.mkdir()
        path = self._seed(workspace, extra.as_posix())
        assert resolve_and_classify(workspace, str(extra / "notes.txt"))[1] is False

        _rewrite(path, "version: 1\nextra_roots: [oops\n")

        assert resolve_and_classify(workspace, str(extra / "notes.txt"))[1] is False


def _counting(target: Any) -> tuple[Any, list[int]]:
    """数解析函数被调了几次。不用抛异常的替身：有几处会吞掉解析异常，断言会静默失效。"""
    calls = [0]

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return target(*args, **kwargs)

    return wrapper, calls


# ---------------------------------------------------------------------------
#  runtime 的两层
# ---------------------------------------------------------------------------

@pytest.fixture
def runtime_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时代码目录 + 工作区，真仓库的 defaults/runtime.yaml 一个字节都不碰。"""
    monkeypatch.setattr(config_layout, "CODE_ROOT", tmp_path / "code")
    monkeypatch.setattr(clonoth_runtime, "_CACHE", {})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


class TestTheRuntimeCache:
    def test_a_same_tick_rewrite_of_the_bundled_layer_is_noticed(self, runtime_workspace: Path) -> None:
        default = _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")
        assert clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]["max_steps"] == 40

        _same_tick_replace(default, "engine:\n  max_steps: 41\n")

        assert clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]["max_steps"] == 41

    def test_a_same_tick_rewrite_of_the_instance_layer_is_noticed(self, runtime_workspace: Path) -> None:
        own = _write(config_layout.runtime_path(runtime_workspace), "engine:\n  max_steps: 50\n")
        assert clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]["max_steps"] == 50

        _same_tick_replace(own, "engine:\n  max_steps: 51\n")

        assert clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]["max_steps"] == 51

    def test_each_layer_is_watched_on_its_own(self, runtime_workspace: Path) -> None:
        default = _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")
        own = _write(config_layout.runtime_path(runtime_workspace), "engine:\n  retry:\n    max_retries: 5\n")
        merged = clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]
        assert (merged["max_steps"], merged["retry"]["max_retries"]) == (40, 5)

        _same_tick_replace(default, "engine:\n  max_steps: 41\n")
        merged = clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]
        assert (merged["max_steps"], merged["retry"]["max_retries"]) == (41, 5)

        _same_tick_replace(own, "engine:\n  retry:\n    max_retries: 6\n")

        merged = clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]
        assert (merged["max_steps"], merged["retry"]["max_retries"]) == (41, 6)

    def test_an_untouched_pair_comes_from_the_cache(
        self, runtime_workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")
        own = _write(config_layout.runtime_path(runtime_workspace), "engine:\n  max_steps: 50\n")
        assert clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]["max_steps"] == 50
        counted, calls = _counting(clonoth_runtime._read_runtime_layer)
        monkeypatch.setattr(clonoth_runtime, "_read_runtime_layer", counted)

        assert clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]["max_steps"] == 50
        assert calls == [0]

        _same_tick_replace(own, "engine:\n  max_steps: 51\n")

        assert clonoth_runtime.load_runtime_config(runtime_workspace)["engine"]["max_steps"] == 51
        # 每层按自己的指纹缓存：只有改了的实例那份重读，仓库默认那份不动。
        assert calls == [1]


# ---------------------------------------------------------------------------
#  节点别名
# ---------------------------------------------------------------------------

@pytest.fixture
def nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 仓库自带的 defaults/nodes 排在实例目录后面，不挡住就会混进这个假工作区的节点表。
    monkeypatch.setattr(config_layout, "CODE_ROOT", tmp_path)
    monkeypatch.setattr(node_module, "_ALIAS_CACHE", {})
    (tmp_path / "config" / "nodes").mkdir(parents=True)
    (tmp_path / "engine" / "system_nodes").mkdir(parents=True)
    return tmp_path


def _node_yaml(aliases: str) -> str:
    return f"type: ai\nid: chat.vision\naliases:\n- {aliases}\n"


class TestTheNodeAliasCache:
    def test_a_same_tick_rewrite_moves_the_old_id(self, nodes: Path) -> None:
        path = _write(nodes / "config" / "nodes" / "chat.vision.yaml", _node_yaml("qq.vision"))
        assert canonical_node_id(nodes, "qq.vision") == "chat.vision"

        _same_tick_replace(path, _node_yaml("qq.visio2"))

        assert canonical_node_id(nodes, "qq.vision") == "qq.vision"
        assert canonical_node_id(nodes, "qq.visio2") == "chat.vision"

    def test_an_alias_added_in_the_same_tick_is_picked_up(self, nodes: Path) -> None:
        path = _write(nodes / "config" / "nodes" / "chat.vision.yaml", _node_yaml("qq.visio2"))
        assert canonical_node_id(nodes, "qq.vision") == "qq.vision"

        _same_tick_replace(path, _node_yaml("qq.vision"))

        assert canonical_node_id(nodes, "qq.vision") == "chat.vision"

    def test_an_untouched_file_comes_from_the_cache(
        self, nodes: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = _write(nodes / "config" / "nodes" / "chat.vision.yaml", _node_yaml("qq.vision"))
        assert canonical_node_id(nodes, "qq.vision") == "chat.vision"
        counted, calls = _counting(node_module._parse_aliases)
        monkeypatch.setattr(node_module, "_parse_aliases", counted)

        # 每一次派发都查一遍别名，指纹没变就不该再读一次 yaml。
        assert canonical_node_id(nodes, "qq.vision") == "chat.vision"
        assert calls == [0]

        _same_tick_replace(path, _node_yaml("qq.visio2"))

        assert canonical_node_id(nodes, "qq.vision") == "qq.vision"
        assert calls[0] >= 1


# ---------------------------------------------------------------------------
#  LiveConfig
# ---------------------------------------------------------------------------

_LIVE_KEYS = (
    LiveKey("enabled", "feature.enabled", BOOL, False),
    LiveKey("limit", "feature.limit", int_in(min_value=1, max_value=10), 3),
)
# 两份等长的文档。
_LIVE_A = "feature:\n  enabled: true\n  limit: 4\n"
_LIVE_B = "feature:\n  enabled: true\n  limit: 7\n"
# bot 和 supervisor 之间的跨进程协议：两边按字典相等比「已生效」，多一个键就永远不相等。
_EXPORTED_KEYS = {"exists", "mtime_ns", "size"}


def _live(path: Path) -> LiveConfig:
    return LiveConfig(_LIVE_KEYS, path=lambda: path)


class TestLiveConfig:
    def test_a_same_tick_rewrite_is_reparsed(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "qq.yaml", _LIVE_A)
        config = _live(path)
        assert config.current()["limit"] == 4

        _same_tick_replace(path, _LIVE_B)

        assert config.current()["limit"] == 7

    def test_the_exported_shape_is_unchanged(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "qq.yaml", _LIVE_A)
        info = path.stat()

        state = _live(path).file_state()

        assert set(state["file"]) == _EXPORTED_KEYS
        assert set(state["loaded"]) == _EXPORTED_KEYS
        assert state["file"] == {"exists": True, "mtime_ns": info.st_mtime_ns, "size": info.st_size}
        assert state["loaded"] == state["file"]

    def test_a_missing_file_still_exports_zeroes(self, tmp_path: Path) -> None:
        state = _live(tmp_path / "nope.yaml").file_state()

        assert state["file"] == {"exists": False, "mtime_ns": 0, "size": 0}
        assert state["loaded"] == {"exists": False, "mtime_ns": 0, "size": 0}

    def test_the_exported_shape_survives_a_same_tick_rewrite(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "qq.yaml", _LIVE_A)
        config = _live(path)
        config.file_state()

        _same_tick_replace(path, _LIVE_B)

        state = config.file_state()
        assert set(state["file"]) == set(state["loaded"]) == _EXPORTED_KEYS
        # 两份文件同刻等长，导出的两项数字自然相同；判「已生效」靠的是它们相等。
        assert state["loaded"] == state["file"]
        assert config.current()["limit"] == 7

    def test_a_broken_file_still_reports_the_version_in_use(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "qq.yaml", _LIVE_A)
        config = _live(path)
        assert config.current()["limit"] == 4

        _rewrite(path, "feature:\n  enabled: [oops\n")

        state = config.file_state()
        assert state["parse_error"]
        # 磁盘上那份和实际生效那份不是一个版本，控制台据此显示「没生效」。
        assert state["loaded"] != state["file"]
        assert config.current()["limit"] == 4

    def test_an_untouched_file_is_not_reparsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import adapters._shared.live_config as live_module

        path = _write(tmp_path / "qq.yaml", _LIVE_A)
        config = _live(path)
        assert config.current()["limit"] == 4
        # 数调用次数，不用抛异常的替身：解析失败会被吞掉、留用上一份，断言就静默失效了。
        counted, calls = _counting(live_module.load_yaml)
        monkeypatch.setattr(live_module, "load_yaml", counted)

        assert config.current()["limit"] == 4
        assert calls == [0]

        _same_tick_replace(path, _LIVE_B)

        assert config.current()["limit"] == 7
        assert calls == [1]


# ---------------------------------------------------------------------------
#  PUT /v1/features/config/raw 之后紧接着 GET state
# ---------------------------------------------------------------------------

_FEATURES_A = "queue:\n  workers: 4\n"
_FEATURES_B = "queue:\n  workers: 5\n"


@pytest.fixture
def features_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-file-stamp"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


def _put_features(call: Any, content: str) -> httpx.Response:
    response = call("PUT", "/v1/features/config/raw", json={"content": content})
    assert response.status_code == 200, response.text
    return response


def _features_state(call: Any) -> dict[str, Any]:
    response = call("GET", "/v1/features/config/state")
    assert response.status_code == 200, response.text
    return response.json()


class TestTheFeatureConfigEndpoint:
    def test_a_save_shows_up_on_the_very_next_state_read(self, features_api: Any) -> None:
        _put_features(features_api, _FEATURES_A)
        assert _features_state(features_api)["state"]["values"]["queue_workers"] == 4

        _put_features(features_api, _FEATURES_B)

        assert _features_state(features_api)["state"]["values"]["queue_workers"] == 5

    def test_a_same_tick_save_shows_up_too(self, features_api: Any, tmp_path: Path) -> None:
        """写完不再手动作废缓存了：整份写回都是换文件落盘，inode 必变。"""
        _put_features(features_api, _FEATURES_A)
        assert _features_state(features_api)["state"]["values"]["queue_workers"] == 4
        before = (tmp_path / "config" / "features.yaml").stat()

        _put_features(features_api, _FEATURES_B)
        os.utime(tmp_path / "config" / "features.yaml", ns=(before.st_atime_ns, before.st_mtime_ns))

        assert _features_state(features_api)["state"]["values"]["queue_workers"] == 5

    def test_the_applied_judgement_is_unchanged(self, features_api: Any) -> None:
        _put_features(features_api, _FEATURES_B)

        body = _features_state(features_api)

        assert body["applied"] is True
        assert body["state"]["loaded"] == body["file"]
        assert set(body["file"]) == _EXPORTED_KEYS

    def test_a_broken_file_is_not_applied(self, features_api: Any, tmp_path: Path) -> None:
        _put_features(features_api, _FEATURES_A)
        path = tmp_path / "config" / "features.yaml"

        _rewrite(path, "queue:\n  workers: [4\n")

        body = _features_state(features_api)
        assert body["applied"] is False
        assert body["state"]["parse_error"]
        assert body["state"]["loaded"] != body["file"]
        assert body["state"]["values"]["queue_workers"] == 4


# ---------------------------------------------------------------------------
#  对话存储
# ---------------------------------------------------------------------------

def _message(message_id: str) -> Message:
    return Message(id=message_id, role="user", content="正文")


def _ids(store: ConversationStore) -> list[str]:
    return [message.id for message in store.load("s")]


class TestTheConversationStore:
    def test_a_same_tick_replacement_is_a_conflict(self, tmp_path: Path) -> None:
        """supervisor 的压缩和 worker 的 L2 snip 改同一个文件，认漏了就把摘要整段盖回去。"""
        reader = ConversationStore(tmp_path)
        writer = ConversationStore(tmp_path)
        reader.append("s", _message("old-1"))
        assert _ids(reader) == ["old-1"]
        path = tmp_path / "s.jsonl"
        before = path.stat()

        writer.replace_all("s", [_message("new-1")])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)

        with pytest.raises(ConversationChangedError):
            reader.replace_all("s", [_message("mine")])
        # 冲突后缓存作废，重读拿到的是别人写进去的那份。
        assert _ids(reader) == ["new-1"]

    def test_a_file_created_after_an_empty_load_is_a_conflict(self, tmp_path: Path) -> None:
        # 哨兵是 (0,0,0) 不是 None：加载时文件不在、之后被别的进程建出来，照样要拦。
        reader = ConversationStore(tmp_path)
        assert _ids(reader) == []

        ConversationStore(tmp_path).append("s", _message("theirs"))

        with pytest.raises(ConversationChangedError):
            reader.replace_all("s", [_message("mine")])
        assert _ids(reader) == ["theirs"]

    def test_replacing_from_a_fresh_read_goes_through(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path)
        store.append("s", _message("old-1"))
        store.load("s")

        store.replace_all("s", [_message("new-1")])

        assert _ids(ConversationStore(tmp_path)) == ["new-1"]

    def test_a_session_nobody_loaded_can_be_replaced(self, tmp_path: Path) -> None:
        # 没读过就没有要保护的读改写窗口。
        ConversationStore(tmp_path).append("s", _message("old-1"))

        ConversationStore(tmp_path).replace_all("s", [_message("new-1")])

        assert _ids(ConversationStore(tmp_path)) == ["new-1"]

    def test_load_still_serves_the_cache_when_nothing_moved(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path)
        store.append("s", _message("old-1"))
        first = store.load("s")

        assert store.load("s") is first

    def test_append_drops_an_outdated_cache_after_a_same_tick_write(self, tmp_path: Path) -> None:
        reader = ConversationStore(tmp_path)
        writer = ConversationStore(tmp_path)
        reader.append("s", _message("old-1"))
        assert _ids(reader) == ["old-1"]
        path = tmp_path / "s.jsonl"
        before = path.stat()

        writer.replace_all("s", [_message("new-1")])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        reader.append("s", _message("mine-1"))

        assert _ids(reader) == ["new-1", "mine-1"]

    def test_a_reload_after_a_same_tick_write_sees_the_new_content(self, tmp_path: Path) -> None:
        reader = ConversationStore(tmp_path)
        writer = ConversationStore(tmp_path)
        reader.append("s", _message("old-1"))
        assert _ids(reader) == ["old-1"]
        path = tmp_path / "s.jsonl"
        before = path.stat()

        writer.replace_all("s", [_message("new-1")])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert _ids(reader) == ["new-1"]


# ---------------------------------------------------------------------------
#  config/mcp.yaml：调用期读的那份
# ---------------------------------------------------------------------------

class TestTheMcpConfig:
    _GOOD = "clients:\n  aaa:\n    command: x\n"

    def test_a_same_tick_rewrite_is_picked_up(self, tmp_path: Path) -> None:
        path = _write(config_layout.mcp_path(tmp_path), self._GOOD)
        assert [client["id"] for client in mcp_runtime.list_clients(tmp_path)] == ["aaa"]

        _same_tick_replace(path, "clients:\n  bbb:\n    command: x\n")

        assert [client["id"] for client in mcp_runtime.list_clients(tmp_path)] == ["bbb"]

    def test_a_same_tick_breakage_is_noticed_and_the_last_good_one_stays(self, tmp_path: Path) -> None:
        path = _write(config_layout.mcp_path(tmp_path), self._GOOD)
        assert mcp_runtime.config_error(tmp_path) == ""

        _same_tick_replace(path, "clients:\n  aaa:\n    command: [\n")

        assert mcp_runtime.config_error(tmp_path).startswith(f"{config_layout.MCP_FILE} 用不了，沿用上一份能用的")
        assert [client["id"] for client in mcp_runtime.list_clients(tmp_path)] == ["aaa"]


# ---------------------------------------------------------------------------
#  同一个指纹被这几处共用
# ---------------------------------------------------------------------------

class TestEveryCacheUsesTheSameStamp:
    @pytest.mark.parametrize("module", [
        "supervisor.config_store",
        "adapters._shared.user_profiles",
        "supervisor.tool_watch",
        "toolbox.mcp_runtime",
        "supervisor.policy",
        "engine.node",
        "engine.conversation_store",
        "adapters._shared.live_config",
    ])
    def test_each_of_them_reaches_config_layout(self, module: str) -> None:
        # 各自再抄一份实现，就会再出现一处只看修改时间和大小的缓存。
        import importlib

        assert importlib.import_module(module).config_layout is config_layout

    def test_nobody_keeps_a_private_stamp_helper(self) -> None:
        assert not hasattr(ConfigStore, "_file_stamp")
        assert not hasattr(clonoth_runtime, "_mtime")
        assert not hasattr(PolicyEngine, "_cached_mtime")

    def test_the_conversation_store_sentinel_is_a_triple(self, tmp_path: Path) -> None:
        # replace_all 里 `expected_stamp is not None` 的判断照旧靠它：文件不在也要记下一个非 None 的戳。
        store = ConversationStore(tmp_path)

        assert store._file_stamp(tmp_path / "nope.jsonl") == (0, 0, 0)

    def test_the_live_config_sentinel_is_a_triple(self) -> None:
        from adapters._shared.live_config import _MISSING_STAMP

        assert _MISSING_STAMP == (0, 0, 0)


class TestTheToolSideYamlReader:
    """工具那边只吃标准库导不了 config_layout，指纹是照抄的一份：两份得一直是同一个东西。"""

    def test_its_stamp_is_the_config_layout_one(self, tmp_path: Path) -> None:
        from tools import _channel

        path = _write(tmp_path / "a.yaml", "k: 1\n")

        assert _channel._file_stamp(path) == config_layout.file_stamp(path)
        assert _channel._file_stamp(tmp_path / "nope.yaml") is None
        assert config_layout.file_stamp(tmp_path / "nope.yaml") is None

    def test_a_same_tick_rewrite_is_reparsed(self, tmp_path: Path) -> None:
        from tools import _channel

        path = _write(tmp_path / "config" / "models.yaml", "version: 1\nopenai:\n  model: m-aaa\n")
        assert _channel.read_yaml_document(path, label="config/models.yaml").data["openai"]["model"] == "m-aaa"

        _same_tick_replace(path, "version: 1\nopenai:\n  model: m-bbb\n")

        assert _channel.read_yaml_document(path, label="config/models.yaml").data["openai"]["model"] == "m-bbb"

    def test_a_same_tick_breakage_falls_back_to_the_last_good_reading(self, tmp_path: Path) -> None:
        from tools import _channel

        path = _write(tmp_path / "config" / "models.yaml", "version: 1\nopenai:\n  model: m-aaa\n")
        _channel.read_yaml_document(path, label="config/models.yaml")

        _same_tick_replace(path, "version: 1\nopenai:\n  model: [m-aa\n")
        document = _channel.read_yaml_document(path, label="config/models.yaml")

        assert document.data["openai"]["model"] == "m-aaa"
        assert document.stale.startswith("config/models.yaml 读不了")
        assert document.problem == ""


class TestTheProfileFileAndConfigStoreShareIt:
    def test_the_config_store_notices_a_same_tick_rewrite(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "config" / "models.yaml", "version: 1\nprovider: openai\nopenai:\n  model: m-aaa\n")
        store = ConfigStore(path=path, workspace_root=tmp_path)
        assert store.get_openai_secret().model == "m-aaa"

        _same_tick_replace(path, "version: 1\nprovider: openai\nopenai:\n  model: m-bbb\n")

        assert store.get_openai_secret().model == "m-bbb"

    def test_the_profile_file_notices_a_same_tick_rewrite(self, tmp_path: Path) -> None:
        from adapters._shared.user_profiles import ProfileFile

        path = _write(tmp_path / "people.yaml", 'users:\n  "10001":\n    display_name: 张三\n')
        source = ProfileFile(path)
        assert source.load() == {"10001": {"display_name": "张三"}}
        assert source.changed() is False

        _same_tick_replace(path, 'users:\n  "10001":\n    display_name: 李四\n')

        assert source.changed() is True
        assert source.load() == {"10001": {"display_name": "李四"}}
