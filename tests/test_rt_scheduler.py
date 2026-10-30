"""定时任务：脚本在自己的线程和进程组里跑，带不走 supervisor 的密钥，超时连子孙一起杀，拖不住别的定时任务。

schedules.yaml 的锁和写入跟 config/ 下其余配置走同一套；控制台原文编辑器保存前先过一遍调度线程读得懂的结构。
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _lock_holder import WAIT_SEC, LockHolder  # noqa: E402
from supervisor import scheduler  # noqa: E402
from supervisor.scheduler import (  # noqa: E402
    SchedulerThread,
    check_schedules_text,
    load_schedules,
    schedules_lock,
    schedules_path,
    update_schedules,
)

_SETTLE_SEC = 60.0


class _State:
    """调度线程碰得到的那几样：开会话、记事件、发 tick。"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.eventlog = self
        self._lock = threading.RLock()
        self.hook_registry = SimpleNamespace(fire=lambda *_args, **_kwargs: None)

    def get_or_create_session(self, *, channel: str, conversation_key: str) -> str:
        return "sess"

    def append(self, *, session_id: str, component: str, type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"seq": len(self.payloads), "payload": payload}

    def record_inbound_message_event(self, evt: dict[str, Any]) -> None:
        return None

    def _build_supervisor_hook_ctx(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    def texts(self) -> dict[str, str]:
        return {payload["schedule_id"]: payload["text"] for payload in self.payloads}


@pytest.fixture
def state() -> _State:
    return _State()


@pytest.fixture
def runner(tmp_path: Path, state: _State) -> SchedulerThread:
    return SchedulerThread(state=state, workspace_root=tmp_path)  # type: ignore[arg-type]


def _command(*args: str) -> str:
    """当前解释器跑一段脚本的命令行，按本机 shell 的规矩转义：路径里可能有空格。"""
    argv = [sys.executable, *args]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _seed(tmp_path: Path, *items: dict[str, Any]) -> Path:
    path = schedules_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schedules": list(items)}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _settle(runner: SchedulerThread) -> None:
    deadline = time.monotonic() + _SETTLE_SEC
    while time.monotonic() < deadline:
        with runner._scripts_lock:
            if not runner._running_scripts:
                return
        time.sleep(0.05)
    raise AssertionError("脚本线程没有收尾")


def _alive(pid: int) -> bool:
    """进程还在：僵尸不算，孤儿被没人收尸的 1 号进程领走时就是僵尸。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


# ---------------------------------------------------------------------------
#  脚本的环境、超时和进程树
# ---------------------------------------------------------------------------

class TestScriptProcesses:
    def test_a_script_does_not_inherit_model_keys_or_the_admin_token(
        self, tmp_path: Path, runner: SchedulerThread, state: _State, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 命令是写在文件里的，跑在 supervisor 的身份下：管理令牌一漏，它就能自己批自己的审批。
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "admin-should-not-leak")
        monkeypatch.setenv("RT_SCHEDULER_PLAIN", "kept")
        probe = _script(tmp_path, "probe.py", (
            "import json, os\n"
            "names = ('OPENAI_API_KEY', 'CLONOTH_ADMIN_TOKEN', 'RT_SCHEDULER_PLAIN')\n"
            "print(json.dumps({'text': ','.join(os.environ.get(name, '-') for name in names)}))\n"
        ))

        assert runner._fire_script({"command": _command(str(probe)), "conversation_key": "scheduler:probe"}, "probe")

        assert state.texts() == {"probe": "-,-,kept"}

    def test_a_timed_out_script_is_killed_together_with_its_children(
        self, tmp_path: Path, runner: SchedulerThread,
    ) -> None:
        # 只杀 shell 的话，孙进程攥着输出管道，communicate 永远等不到头，这个线程就挂死了。
        pid_file = tmp_path / "child.pid"
        tree = _script(tmp_path, "tree.py", (
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
            "time.sleep(120)\n"
        ))

        started = time.monotonic()
        assert runner._run_command("tree", _command(str(tree), str(pid_file)), 2.0) is None
        assert time.monotonic() - started < 30

        if os.name == "posix" and pid_file.is_file():
            child = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 10
            while _alive(child) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not _alive(child)

    def test_a_script_that_exits_non_zero_injects_nothing(
        self, tmp_path: Path, runner: SchedulerThread, state: _State,
    ) -> None:
        failing = _script(tmp_path, "fail.py", "import sys\nprint('{\"text\": \"x\"}')\nsys.exit(3)\n")

        assert runner._fire_script({"command": _command(str(failing))}, "fail") is False
        assert state.payloads == []

    def test_json_that_is_not_an_object_is_refused_without_crashing(
        self, tmp_path: Path, runner: SchedulerThread, state: _State,
    ) -> None:
        listing = _script(tmp_path, "list.py", "print('[1, 2, 3]')\n")

        assert runner._fire_script({"command": _command(str(listing))}, "list") is False
        assert state.payloads == []

    def test_output_that_is_not_utf8_does_not_kill_the_run(
        self, tmp_path: Path, runner: SchedulerThread, state: _State,
    ) -> None:
        noisy = _script(tmp_path, "noisy.py", (
            "import sys\n"
            "sys.stderr.buffer.write(b'\\xff\\xfe broken\\n')\n"
            "print('{\"text\": \"fine\"}')\n"
        ))

        assert runner._fire_script({"command": _command(str(noisy))}, "noisy") is True
        assert state.texts() == {"noisy": "fine"}

    @pytest.mark.parametrize(("raw", "expected"), [
        (None, 30.0), ("", 30.0), ("abc", 30.0), (0, 30.0), (-5, 30.0),
        (float("inf"), 30.0), (float("nan"), 30.0), ([1], 30.0),
        ("45", 45.0), (12.5, 12.5), (0.2, 1.0), (10**9, 3600.0),
    ])
    def test_the_timeout_is_read_defensively_and_clamped(self, raw: Any, expected: float) -> None:
        # 以前 int("abc") 直接抛，这条定时任务从此一次都跑不成；超时写得再大也不能永远占着一个线程。
        assert scheduler._script_timeout(raw) == expected


# ---------------------------------------------------------------------------
#  一个慢脚本拖不住别的定时任务
# ---------------------------------------------------------------------------

def _waiting_script(tmp_path: Path, flag: Path, started: Path) -> Path:
    return _script(tmp_path, "wait.py", (
        "import json, pathlib, sys, time\n"
        "flag, started = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
        "with started.open('a', encoding='utf-8') as log:\n"
        "    log.write('started\\n')\n"
        "deadline = time.monotonic() + 50\n"
        "while not flag.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        "print(json.dumps({'text': 'slow done'}))\n"
    ))


class TestTheTickIsNeverBlocked:
    def test_a_slow_script_does_not_hold_up_a_message_due_the_same_minute(
        self, tmp_path: Path, runner: SchedulerThread, state: _State,
    ) -> None:
        flag, started = tmp_path / "release", tmp_path / "started.log"
        slow = _waiting_script(tmp_path, flag, started)
        _seed(
            tmp_path,
            {"id": "slow", "type": "script", "cron": "* * * * *", "timeout": 55,
             "command": _command(str(slow), str(flag), str(started))},
            {"id": "ping", "cron": "* * * * *", "text": "hello"},
        )
        try:
            begun = time.monotonic()
            runner._tick()
            # 以前脚本在调度线程里同步跑，这一轮要等它跑完，过了那一分钟的 cron 就再也不响。
            assert time.monotonic() - begun < 10
            assert state.texts().get("ping") == "hello"
        finally:
            flag.write_text("go", encoding="utf-8")
            _settle(runner)
        assert state.texts().get("slow") == "slow done"

    def test_the_same_script_is_not_started_again_while_it_still_runs(
        self, tmp_path: Path, runner: SchedulerThread,
    ) -> None:
        flag, started = tmp_path / "release", tmp_path / "started.log"
        slow = _waiting_script(tmp_path, flag, started)
        entry = {"id": "slow", "type": "script", "timeout": 55, "command": _command(str(slow), str(flag), str(started))}
        try:
            runner._start_script(entry, "slow", False)
            deadline = time.monotonic() + 30
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            runner._start_script(entry, "slow", False)
        finally:
            flag.write_text("go", encoding="utf-8")
            _settle(runner)

        assert started.read_text(encoding="utf-8").splitlines() == ["started"]

    def test_a_one_shot_script_is_removed_once_it_has_delivered(
        self, tmp_path: Path, runner: SchedulerThread, state: _State,
    ) -> None:
        once = _script(tmp_path, "once.py", "print('{\"text\": \"reminder\"}')\n")
        entry = {"id": "once", "type": "script", "once": True, "command": _command(str(once))}
        _seed(tmp_path, entry, {"id": "other", "cron": "0 0 1 1 *"})

        runner._start_script(entry, "once", True)
        _settle(runner)

        assert state.texts() == {"once": "reminder"}
        assert [item["id"] for item in load_schedules(tmp_path)] == ["other"]

    def test_a_one_shot_script_that_failed_waits_for_the_next_match(
        self, tmp_path: Path, runner: SchedulerThread,
    ) -> None:
        failing = _script(tmp_path, "fail.py", "import sys\nsys.exit(1)\n")
        entry = {"id": "once", "type": "script", "once": True, "command": _command(str(failing))}
        _seed(tmp_path, entry)

        runner._start_script(entry, "once", True)
        _settle(runner)

        assert [item["id"] for item in load_schedules(tmp_path)] == ["once"]

    def test_switches_written_as_text_are_read_as_switches(
        self, tmp_path: Path, runner: SchedulerThread, state: _State,
    ) -> None:
        # 手写的 "false" 以前被 bool() 读成真：关掉的任务照响，一次性的标成 "no" 也照删。
        _seed(
            tmp_path,
            {"id": "off", "cron": "* * * * *", "enabled": "false", "text": "should not fire"},
            {"id": "keep", "cron": "* * * * *", "once": "no", "text": "fires and stays"},
        )

        runner._tick()

        assert state.texts() == {"keep": "fires and stays"}
        assert [item["id"] for item in load_schedules(tmp_path)] == ["off", "keep"]


class TestTheTickClock:
    @pytest.mark.parametrize("second", [0.0, 0.5, 1.0, 30.25, 58.9, 59.999])
    def test_every_tick_lands_just_after_a_minute_boundary(self, second: float) -> None:
        # 固定睡 60 秒的话每轮自己的耗时会累积，迟早整整跳过一分钟。
        now = 1_700_000_040.0 + second
        delay = scheduler._until_next_minute(now)

        assert 1.0 <= delay <= 61.0
        assert (now + delay) % 60 == pytest.approx(scheduler._TICK_OFFSET_SEC, abs=1e-6)


# ---------------------------------------------------------------------------
#  原文编辑器的校验
# ---------------------------------------------------------------------------

class TestTheEditorCheck:
    @pytest.mark.parametrize("text", [
        "schedules: [unclosed\n",
        "- just\n- a list\n",
        "schedules: nope\n",
        "schedules: {id: a}\n",
    ])
    def test_a_file_the_scheduler_cannot_read_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError):
            check_schedules_text(text)

    @pytest.mark.parametrize(("text", "reason"), [
        (
            "schedules:\n  - id: a\n    command: run --token sk-SECRET-sched: x\n",
            "YAML 解析失败：第 3 行第 41 列：mapping values are not allowed here",
        ),
        ("schedules:\n  - id: a\n    cron: !!int sk-SECRET-sched\n", "YAML 解析失败：第 3 行第 11 列：值不是合法的 int"),
    ], ids=["broken-line", "bad-value"])
    def test_a_refusal_names_the_place_but_not_the_line(self, text: str, reason: str) -> None:
        # 条目里写着要跑的命令和里面的令牌；原因原样显示在控制台，接口层再包一层往上抛。
        with pytest.raises(ValueError) as caught:
            check_schedules_text(text)

        assert str(caught.value) == reason
        for link in (caught.value.__cause__, caught.value.__context__):
            assert link is not None and "sk-SECRET" not in str(link)

    @pytest.mark.parametrize("text", ["", "schedules: []\n", "schedules:\n", "{}\n"])
    def test_an_empty_list_is_fine(self, text: str) -> None:
        assert check_schedules_text(text) == []

    def test_entries_the_scheduler_would_skip_are_reported(self) -> None:
        text = yaml.safe_dump({"schedules": [
            {"id": "ok", "cron": "*/5 * * * *", "text": "hi"},
            "a bare string",
            {"cron": "* * * * *"},
            {"id": "ok", "cron": "* * * * *"},
            {"id": "short", "cron": "* * * *"},
            {"id": "script", "cron": "* * * * *", "type": "script"},
        ]}, allow_unicode=True)

        warnings = check_schedules_text(text)

        assert len(warnings) == 5
        assert any("第 2 条" in line for line in warnings)
        assert any("第 3 条" in line and "id" in line for line in warnings)
        assert any("ok" in line and "重复" in line for line in warnings)
        assert any("short" in line and "cron" in line for line in warnings)
        assert any("script" in line and "command" in line for line in warnings)

    def test_cron_terms_that_never_fire_are_reported(self) -> None:
        text = yaml.safe_dump({"schedules": [
            {"id": "dead", "cron": "0-59/5 9 * * *", "text": "hi"},
            {"id": "partly", "cron": "7,0-59/5 9 * * *", "text": "hi"},
            {"id": "fine", "cron": "*/5 9 * * 1-5", "text": "hi"},
        ]})

        assert check_schedules_text(text) == [
            "dead 的 cron 分钟段的 0-59/5 永远命中不了，整条不会触发",
            "partly 的 cron 分钟段的 0-59/5 永远命中不了",
        ]


# ---------------------------------------------------------------------------
#  永远不会触发的 cron
# ---------------------------------------------------------------------------

def _scheduler_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


class TestACronThatNeverFires:
    def test_it_is_logged_once_until_its_cron_changes(
        self, tmp_path: Path, runner: SchedulerThread, caplog: pytest.LogCaptureFixture,
    ) -> None:
        fine = {"id": "fine", "cron": "0 9 * * *", "text": "hi"}
        _seed(tmp_path, {"id": "dead", "cron": "0-59/5 9 * * *", "text": "hi"}, fine)

        with caplog.at_level(logging.WARNING, logger="supervisor.scheduler"):
            runner._tick()
            runner._tick()
            _seed(tmp_path, {"id": "dead", "cron": "0 9 * *", "text": "hi"}, fine)
            runner._tick()

        assert _scheduler_warnings(caplog) == [
            "[scheduler] 定时任务 dead 的 cron「0-59/5 9 * * *」分钟段的 0-59/5 永远命中不了，整条不会触发",
            "[scheduler] 定时任务 dead 的 cron「0 9 * *」不是「分 时 日 月 周」五段，不会触发",
        ]

    def test_a_schedule_without_a_cron_is_logged_too(
        self, tmp_path: Path, runner: SchedulerThread, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seed(tmp_path, {"id": "blank", "text": "hi"})

        with caplog.at_level(logging.WARNING, logger="supervisor.scheduler"):
            runner._tick()

        assert _scheduler_warnings(caplog) == ["[scheduler] 定时任务 blank 的 cron「」不是「分 时 日 月 周」五段，不会触发"]

    def test_a_disabled_schedule_is_not_logged(
        self, tmp_path: Path, runner: SchedulerThread, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seed(tmp_path, {"id": "off", "cron": "0-59/5 9 * * *", "text": "hi", "enabled": False})

        with caplog.at_level(logging.WARNING, logger="supervisor.scheduler"):
            runner._tick()

        assert _scheduler_warnings(caplog) == []


# ---------------------------------------------------------------------------
#  锁和写入
# ---------------------------------------------------------------------------

class TestTheSharedLock:
    def test_the_lock_file_is_the_one_config_layout_names(self, tmp_path: Path) -> None:
        # 跨进程互斥：另起一个进程拿住 config_layout 给的那把锁，这边的读改写就得等。
        path = _seed(tmp_path, {"id": "a"})
        holder = LockHolder(config_layout.lock_path(path))
        done = threading.Event()

        def edit() -> None:
            update_schedules(tmp_path, lambda items: items + [{"id": "b"}])
            done.set()

        worker = threading.Thread(target=edit)
        try:
            worker.start()
            assert not done.wait(0.5)
        finally:
            holder.release()
        worker.join(WAIT_SEC)
        assert done.is_set()
        assert [item["id"] for item in load_schedules(tmp_path)] == ["a", "b"]
        assert not path.with_name(f"{path.name}.lock").exists()

    def test_schedules_lock_and_update_schedules_queue_on_one_lock(self, tmp_path: Path) -> None:
        _seed(tmp_path, {"id": "a"})
        done = threading.Event()

        def edit() -> None:
            update_schedules(tmp_path, lambda items: items + [{"id": "b"}])
            done.set()

        with schedules_lock(tmp_path):
            worker = threading.Thread(target=edit)
            worker.start()
            assert not done.wait(0.5)
        worker.join(WAIT_SEC)
        assert done.is_set()

    def test_a_save_keeps_the_existing_newlines_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        path = schedules_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"schedules:\r\n- id: a\r\n")

        scheduler.save_schedules(tmp_path, [{"id": "a"}, {"id": "b"}])

        data = path.read_bytes()
        assert data.count(b"\n") == data.count(b"\r\n") > 0
        assert [item["id"] for item in load_schedules(tmp_path)] == ["a", "b"]
        assert not [entry.name for entry in path.parent.iterdir() if entry.name.endswith(".tmp")]


class TestAnUnreadableFile:
    def test_it_is_logged_once_per_version(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        # 调度线程每分钟都读一次，坏文件不去重的话日志每分钟刷一条；修好之前一条定时任务都不会响，又必须说一声。
        path = schedules_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("schedules: [unclosed\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="supervisor.scheduler"):
            assert load_schedules(tmp_path) == []
            assert load_schedules(tmp_path) == []
            path.write_text("schedules: nope, still broken\n", encoding="utf-8")
            assert load_schedules(tmp_path) == []

        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 2

    def test_the_log_and_a_writer_get_the_place_but_not_the_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # 条目里写着要跑的命令和里面的令牌：调度线程每分钟记一次日志，改它的工具把原因交给模型。
        path = schedules_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("schedules:\n  - id: a\n    command: run --token sk-SECRET-sched: x\n", encoding="utf-8")
        reason = "第 3 行第 41 列：mapping values are not allowed here"

        with caplog.at_level(logging.WARNING, logger="supervisor.scheduler"):
            assert load_schedules(tmp_path) == []
        with pytest.raises(scheduler.ScheduleFileUnreadable) as caught:
            scheduler.read_schedules(tmp_path)

        assert reason in caplog.text
        assert "sk-SECRET" not in caplog.text
        assert str(caught.value).endswith(f"读不懂：{reason}")
        for link in (caught.value, caught.value.__cause__, caught.value.__context__):
            assert link is not None and "sk-SECRET" not in str(link)

    @pytest.mark.parametrize(("body", "said"), [
        (b"schedules: [unclosed\n", "data/schedules.yaml 读不懂：第 "),
        (b"schedules:\n  - id: a\n# \xff\n", "data/schedules.yaml 读不懂：不是 UTF-8 编码（第 "),
        (b"- id: a\n", "data/schedules.yaml 顶层要写成映射"),
        (b"schedules: nope\n", "data/schedules.yaml 里的 schedules 要写成列表"),
    ])
    def test_the_reason_names_the_file_by_its_place_in_the_workspace(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, body: bytes, said: str,
    ) -> None:
        # 原因经定时任务工具交给模型、每分钟进一次日志：绝对路径和解码器原话都不往外带
        path = schedules_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(body)

        with caplog.at_level(logging.WARNING, logger="supervisor.scheduler"):
            assert load_schedules(tmp_path) == []
        with pytest.raises(scheduler.ScheduleFileUnreadable) as caught:
            scheduler.read_schedules(tmp_path)

        assert str(caught.value).startswith(said)
        assert str(tmp_path) not in str(caught.value)
        assert "codec" not in str(caught.value)
        assert said in caplog.text
        assert str(tmp_path) not in caplog.text
