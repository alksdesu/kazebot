"""supervisor 进程的日志出口。

没有 handler 的 logger 落到 lastResort：不带时间和来源地打到 stderr，INFO 整条丢。
而终端归前台 TUI —— 排障时能翻的只剩几行散落的 print。
"""
from __future__ import annotations

import ast
import logging
import re
import sys
import threading
import warnings
from pathlib import Path
from typing import Any

import pytest
import uvicorn

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import logging_setup  # noqa: E402
from supervisor.logging_setup import (  # noqa: E402
    CHATTY_LIBRARIES,
    DEFAULT_BACKUP_COUNT,
    DEFAULT_MAX_BYTES,
    KeyValueFormatter,
    LOG_FORMAT,
    SupervisorLogHandler,
    backup_name,
    configure_supervisor_logging,
)

_MAIN = _ROOT / "supervisor" / "main.py"
_SUPERVISOR = _ROOT / "supervisor"
_SKIP_DIRS = {"__pycache__", "node_modules"}

# 装 handler 之后被改动过的每一处全局状态，用例之间不还原就会互相串。
_WATCHED_LOGGERS = (
    *CHATTY_LIBRARIES, "py.warnings", "uvicorn.error", "uvicorn.access", "uvicorn.asgi",
)

# supervisor 里允许留下的 print：都是首启时给运维看的，那会儿控制台的日志页还打不开。
# 首登链接带着令牌，更是只能走终端 —— 进了日志文件就是谁能读日志谁就能登控制台。
_PRINT_ALLOWED = {
    "supervisor/admin_api.py": ("[admin]", "{console_url}?token="),
    "supervisor/api.py": ("[web] 前端地址",),
}


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch: pytest.MonkeyPatch):
    """configure 改的是整个进程：根 logger、warnings、两个 excepthook 和第三方库的级别。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_root_level = root.level
    saved = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            list(logging.getLogger(name).handlers),
        )
        for name in _WATCHED_LOGGERS
    }
    saved_thread_hook = threading.excepthook
    saved_sys_hook = sys.excepthook
    for name in (logging_setup.MAX_BYTES_ENV, logging_setup.BACKUP_COUNT_ENV):
        monkeypatch.delenv(name, raising=False)

    yield

    logging.captureWarnings(False)
    threading.excepthook = saved_thread_hook
    sys.excepthook = saved_sys_hook
    if logging_setup._installed is not None:
        logging_setup._installed.close()
        logging_setup._installed = None
    root.handlers[:] = saved_handlers
    root.setLevel(saved_root_level)
    for name, (level, propagate, handlers) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.handlers[:] = handlers


def _configure(tmp_path: Path, level: str | int = "info") -> int:
    return configure_supervisor_logging(tmp_path / "supervisor.log", level)


def _read(tmp_path: Path, name: str = "supervisor.log") -> str:
    path = tmp_path / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _names(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir())


def _own_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, SupervisorLogHandler)]


def _foreign_handlers() -> list[logging.Handler]:
    """pytest 自己往根上挂的那几个不算，它们只写内存。"""
    return [
        h for h in logging.getLogger().handlers
        if not isinstance(h, SupervisorLogHandler)
        and not type(h).__module__.startswith("_pytest")
    ]


class TestEverythingLandsInTheFile:
    def test_a_first_party_record_carries_time_level_and_source(self, tmp_path: Path) -> None:
        _configure(tmp_path)

        logging.getLogger("supervisor.x").info("起来了")

        line = _read(tmp_path).strip()
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO supervisor\.x: 起来了", line,
        ), line

    def test_a_logger_nobody_registered_lands_there_too(self, tmp_path: Path) -> None:
        # 装在根上而不是逐个 logger 配：外部插件随手 getLogger 出来的名字也得进同一份文件。
        _configure(tmp_path)

        logging.getLogger("clonoth_external_plugin_demo").info("插件说话了")

        assert "INFO clonoth_external_plugin_demo: 插件说话了" in _read(tmp_path)

    def test_nothing_reaches_the_terminal(self, tmp_path: Path, capfd: pytest.CaptureFixture) -> None:
        # lastResort 会把 WARNING 以上裸打到 stderr，而终端归前台 TUI。
        _configure(tmp_path)
        capfd.readouterr()

        logging.getLogger("supervisor.x").info("信息")
        logging.getLogger("supervisor.x").error("出错了")

        captured = capfd.readouterr()
        assert (captured.out, captured.err) == ("", "")
        assert _foreign_handlers() == []
        assert "出错了" in _read(tmp_path)


class TestExtraFields:
    """路由、会话几处把任务号会话号都放在 extra 里，只印消息的话一行只剩几个英文单词。"""

    def _emit(self, tmp_path: Path, **extra: Any) -> str:
        _configure(tmp_path)
        logging.getLogger("supervisor.x").info("干完了", extra=extra)
        return _read(tmp_path).strip()

    def test_plain_values_are_appended_as_pairs(self, tmp_path: Path) -> None:
        assert self._emit(tmp_path, task="t-1", node="chat").endswith("干完了 task=t-1 node=chat")

    def test_the_insertion_order_is_kept(self, tmp_path: Path) -> None:
        # 按字典序排会把最该先看到的任务号冲到后面去。
        assert self._emit(tmp_path, zeta=1, alpha=2).endswith("zeta=1 alpha=2")

    @pytest.mark.parametrize(("value", "rendered"), [
        ("plain", "plain"),
        ("", "''"),
        ("两 个 词", "'两 个 词'"),
        ("换\n行", "'换\\n行'"),
        (42, "42"),
        (None, "None"),
        (["a"], "['a']"),
    ])
    def test_only_a_bare_word_goes_in_unquoted(self, tmp_path: Path, value: Any, rendered: str) -> None:
        # 带空白的值不加引号，key=value 就断在半路，grep 的人看到的是另一个字段。
        assert self._emit(tmp_path, field=value).endswith(f"干完了 field={rendered}")

    def test_the_colour_copy_uvicorn_attaches_is_dropped(self, tmp_path: Path) -> None:
        # uvicorn 给彩色终端另备的一份消息，和正文一字不差。
        line = self._emit(tmp_path, color_message="干完了")
        assert line.endswith("干完了")
        assert "color_message" not in line

    def test_private_fields_stay_out(self, tmp_path: Path) -> None:
        line = self._emit(tmp_path, _internal="x", task="t-1")
        assert line.endswith("干完了 task=t-1")
        assert "_internal" not in line

    def test_standard_record_fields_are_not_repeated(self, tmp_path: Path) -> None:
        line = self._emit(tmp_path)
        assert line.endswith("干完了")
        assert "levelname=" not in line and "asctime=" not in line

    def test_the_traceback_still_follows_the_line(self, tmp_path: Path) -> None:
        _configure(tmp_path)
        try:
            raise RuntimeError("炸了")
        except RuntimeError:
            logging.getLogger("supervisor.x").exception("处理失败", extra={"task": "t-1"})

        lines = _read(tmp_path).splitlines()
        assert lines[0].endswith("处理失败 task=t-1")
        assert lines[1] == "Traceback (most recent call last):"
        assert lines[-1] == "RuntimeError: 炸了"

    def test_the_formatter_is_the_one_on_the_handler(self, tmp_path: Path) -> None:
        _configure(tmp_path)
        formatter = logging_setup._installed.formatter
        assert isinstance(formatter, KeyValueFormatter)
        assert formatter._style._fmt == LOG_FORMAT


class TestLevels:
    def test_debug_lets_debug_records_through(self, tmp_path: Path) -> None:
        assert _configure(tmp_path, "debug") == logging.DEBUG

        logging.getLogger("supervisor.x").debug("细节")

        assert "DEBUG supervisor.x: 细节" in _read(tmp_path)

    def test_warning_holds_info_back(self, tmp_path: Path) -> None:
        assert _configure(tmp_path, "warning") == logging.WARNING

        logging.getLogger("supervisor.x").info("信息")
        logging.getLogger("supervisor.x").warning("警告")

        text = _read(tmp_path)
        assert "信息" not in text
        assert "警告" in text

    def test_trace_is_recognised_like_uvicorn_does(self, tmp_path: Path) -> None:
        # uvicorn 的 --log-level 认 trace=5，两边用同一张表才不会一个认一个不认。
        assert _configure(tmp_path, "trace") == 5

        logging.getLogger("supervisor.x").debug("细节")

        assert logging.getLogger().level == 5
        assert "细节" in _read(tmp_path)

    def test_the_name_is_case_and_space_insensitive(self, tmp_path: Path) -> None:
        assert _configure(tmp_path, " DEBUG ") == logging.DEBUG

    def test_an_int_is_taken_as_is(self, tmp_path: Path) -> None:
        assert _configure(tmp_path, 25) == 25
        assert logging.getLogger().level == 25

    def test_an_unknown_name_falls_back_to_info_and_says_so(self, tmp_path: Path) -> None:
        # 静默退回 INFO 的话，调了 --log-level verbose 的人只会以为 debug 没东西可打。
        assert _configure(tmp_path, "verbose") == logging.INFO

        text = _read(tmp_path)
        assert "WARNING supervisor.logging_setup" in text
        assert "'verbose'" in text
        assert "INFO" in text

    def test_the_complaint_is_written_after_the_handler_is_in_place(self, tmp_path: Path) -> None:
        # 先抱怨再装 handler，这条 WARNING 就只会落到 lastResort 上。
        _configure(tmp_path, "verbose")
        assert "认不出" in _read(tmp_path)


class TestChattyLibraries:
    def test_per_request_info_is_held_back_at_info(self, tmp_path: Path) -> None:
        # httpx 每发一个请求记一条 INFO，engine 轮询几个端点就把文件刷满了。
        _configure(tmp_path, "info")

        for name in CHATTY_LIBRARIES:
            logging.getLogger(name).info(f"{name} 的请求日志")

        assert _read(tmp_path) == ""

    def test_their_warnings_still_get_through(self, tmp_path: Path) -> None:
        _configure(tmp_path, "info")

        for name in CHATTY_LIBRARIES:
            logging.getLogger(name).warning(f"{name} 连不上")

        text = _read(tmp_path)
        assert all(f"{name} 连不上" in text for name in CHATTY_LIBRARIES)

    def test_debug_opens_them_back_up(self, tmp_path: Path) -> None:
        # 调到 debug 就是为了看它们，压着不放等于这个开关没用。
        _configure(tmp_path, "debug")

        for name in CHATTY_LIBRARIES:
            logging.getLogger(name).info(f"{name} 的请求日志")

        text = _read(tmp_path)
        assert all(f"{name} 的请求日志" in text for name in CHATTY_LIBRARIES)
        assert all(logging.getLogger(name).level == logging.NOTSET for name in CHATTY_LIBRARIES)

    def test_a_later_reconfigure_lifts_the_cap(self, tmp_path: Path) -> None:
        # 先 info 后 debug：上一轮压下去的级别不主动清就一直留着。
        _configure(tmp_path, "info")
        _configure(tmp_path, "debug")

        assert all(logging.getLogger(name).level == logging.NOTSET for name in CHATTY_LIBRARIES)


class TestRotation:
    def test_it_rolls_over_and_caps_the_backups(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(logging_setup.MAX_BYTES_ENV, "300")
        monkeypatch.setenv(logging_setup.BACKUP_COUNT_ENV, "2")
        _configure(tmp_path)

        for i in range(60):
            logging.getLogger("supervisor.x").info("填充 %03d %s", i, "x" * 60)

        assert _names(tmp_path) == ["supervisor.1.log", "supervisor.2.log", "supervisor.log"]

    def test_the_backups_keep_the_log_suffix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 控制台的日志页和定期清理都只认 .log 结尾，supervisor.log.1 两边都看不见。
        monkeypatch.setenv(logging_setup.MAX_BYTES_ENV, "300")
        monkeypatch.setenv(logging_setup.BACKUP_COUNT_ENV, "2")
        _configure(tmp_path)

        for i in range(60):
            logging.getLogger("supervisor.x").info("填充 %03d %s", i, "x" * 60)

        assert not (tmp_path / "supervisor.log.1").exists()

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "10MB", "0", "-1"])
    def test_an_unusable_env_value_falls_back_to_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str,
    ) -> None:
        # 0 或负数会让每条记录都触发一次轮转，备份编号一秒之内就全滚没了。
        monkeypatch.setenv(logging_setup.MAX_BYTES_ENV, raw)
        monkeypatch.setenv(logging_setup.BACKUP_COUNT_ENV, raw)
        _configure(tmp_path)

        handler = logging_setup._installed
        assert (handler.maxBytes, handler.backupCount) == (DEFAULT_MAX_BYTES, DEFAULT_BACKUP_COUNT)

    def test_the_env_values_reach_the_handler(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(logging_setup.MAX_BYTES_ENV, "2048")
        monkeypatch.setenv(logging_setup.BACKUP_COUNT_ENV, "7")
        _configure(tmp_path)

        handler = logging_setup._installed
        assert (handler.maxBytes, handler.backupCount) == (2048, 7)

    def test_the_file_is_utf8(self, tmp_path: Path) -> None:
        # 中文日志在 GBK 控制台下写会抛 UnicodeEncodeError，整条记录连同后面的都没了。
        _configure(tmp_path)
        logging.getLogger("supervisor.x").info("引擎已就绪 ✅")

        assert "引擎已就绪 ✅" in (tmp_path / "supervisor.log").read_text(encoding="utf-8")


class TestBackupName:
    @pytest.mark.parametrize(("given", "wanted"), [
        ("supervisor.log.1", "supervisor.1.log"),
        ("supervisor.log.12", "supervisor.12.log"),
    ])
    def test_the_index_moves_before_the_suffix(self, given: str, wanted: str) -> None:
        assert Path(backup_name(given)).name == wanted

    def test_the_directory_is_left_alone(self, tmp_path: Path) -> None:
        got = backup_name(str(tmp_path / "logs" / "supervisor.log.1"))
        assert Path(got) == tmp_path / "logs" / "supervisor.1.log"

    def test_the_console_log_page_can_see_the_backups(self, tmp_path: Path) -> None:
        # 改名的唯一理由：日志页只 glob *.log，supervisor.log.1 在那边是隐形的。
        from supervisor.log_access import LogReader

        (tmp_path / "supervisor.1.log").write_text("上一轮\n", encoding="utf-8")
        (tmp_path / "supervisor.log.1").write_text("上一轮\n", encoding="utf-8")

        names = {row.name for row in LogReader(tmp_path).list()}

        assert "supervisor.1.log" in names
        assert "supervisor.log.1" not in names

    def test_a_backup_can_be_tailed(self, tmp_path: Path) -> None:
        from supervisor.log_access import LogReader

        (tmp_path / "supervisor.1.log").write_text("上一轮的最后一行\n", encoding="utf-8")

        text, _truncated = LogReader(tmp_path).tail("supervisor.1.log")

        assert "上一轮的最后一行" in text


class TestReconfiguring:
    def test_only_one_handler_is_left_on_the_root(self, tmp_path: Path) -> None:
        # 叠一层就是每条记录写两遍，而轮转按大小算，两个 handler 会互相抢着改名。
        _configure(tmp_path)
        first = logging_setup._installed

        _configure(tmp_path)
        second = logging_setup._installed

        assert first is not second
        assert _own_handlers() == [second]

    def test_the_previous_handler_is_closed(self, tmp_path: Path) -> None:
        # 摘掉不关就一直占着文件句柄，Windows 上轮转改名会被这个句柄挡住。
        _configure(tmp_path)
        first = logging_setup._installed

        _configure(tmp_path)

        assert first.stream is None

    def test_a_record_is_written_once(self, tmp_path: Path) -> None:
        _configure(tmp_path)
        _configure(tmp_path)

        logging.getLogger("supervisor.x").info("只此一条")

        assert _read(tmp_path).count("只此一条") == 1


class TestWarnings:
    def test_a_python_warning_lands_in_the_file(self, tmp_path: Path) -> None:
        # DeprecationWarning 默认走 stderr，被 TUI 吞掉就等于升级前没人看到过。
        _configure(tmp_path)

        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn("这个接口要下线了", DeprecationWarning, stacklevel=1)

        text = _read(tmp_path)
        assert "py.warnings" in text
        assert "这个接口要下线了" in text


class TestUncaughtExceptions:
    def test_a_thread_crash_is_recorded_with_its_name(
        self, tmp_path: Path, capfd: pytest.CaptureFixture,
    ) -> None:
        # 看门狗和调度线程死在这里，默认钩子只往 stderr 打，进程看着还活着。
        _configure(tmp_path)
        capfd.readouterr()

        def _boom() -> None:
            raise RuntimeError("线程里炸了")

        thread = threading.Thread(target=_boom, name="test-worker")
        thread.start()
        thread.join()

        text = _read(tmp_path)
        assert "ERROR supervisor.uncaught" in text
        assert "test-worker" in text
        assert "RuntimeError: 线程里炸了" in text
        assert "Traceback (most recent call last):" in text
        assert capfd.readouterr().err == ""

    def test_a_thread_calling_sys_exit_is_not_an_error(self, tmp_path: Path) -> None:
        _configure(tmp_path)

        def _quit() -> None:
            raise SystemExit(3)

        thread = threading.Thread(target=_quit, name="test-quitter")
        thread.start()
        thread.join()

        assert "supervisor.uncaught" not in _read(tmp_path)

    def test_a_fatal_exception_is_recorded_before_being_handed_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _configure(tmp_path)
        handed: list[type[BaseException]] = []
        monkeypatch.setattr(sys, "__excepthook__", lambda exc_type, *_rest: handed.append(exc_type))

        sys.excepthook(ValueError, ValueError("主线程炸了"), None)

        text = _read(tmp_path)
        assert "CRITICAL supervisor.uncaught" in text
        assert "ValueError: 主线程炸了" in text
        # 进程要退了，终端和 systemd 日志里也照常留一份。
        assert handed == [ValueError]

    def test_ctrl_c_is_handed_on_without_a_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 人按的 Ctrl-C 不是故障，记成 CRITICAL 会让每次正常停机都像崩溃。
        _configure(tmp_path)
        handed: list[type[BaseException]] = []
        monkeypatch.setattr(sys, "__excepthook__", lambda exc_type, *_rest: handed.append(exc_type))

        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

        assert "supervisor.uncaught" not in _read(tmp_path)
        assert handed == [KeyboardInterrupt]


async def _app(scope, receive, send):  # pragma: no cover - uvicorn.Config 只存不调
    raise AssertionError("这个 app 不该被真的跑起来")


class TestUvicornSharesTheFile:
    def test_its_own_records_land_in_the_same_file(self, tmp_path: Path) -> None:
        # log_config=None 时 uvicorn 不自己装 handler，全靠往根上传。
        level = _configure(tmp_path)
        uvicorn.Config(_app, log_config=None, log_level=level, access_log=False)

        logging.getLogger("uvicorn.error").info("Application startup complete.")

        assert "INFO uvicorn.error: Application startup complete." in _read(tmp_path)

    def test_the_level_it_gets_is_the_one_we_resolved(self, tmp_path: Path) -> None:
        # 传级别名的话 uvicorn 认不出 trace 之外的写法会直接 KeyError，传解析好的 int 两边就一定一致。
        level = _configure(tmp_path, "debug")
        uvicorn.Config(_app, log_config=None, log_level=level, access_log=False)

        logging.getLogger("uvicorn.error").debug("debug 细节")

        assert logging.getLogger("uvicorn.error").level == logging.DEBUG
        assert "debug 细节" in _read(tmp_path)

    def test_the_access_log_stays_out_unless_it_is_asked_for(self, tmp_path: Path) -> None:
        # worker 每秒轮询几个端点，access log 开着的话文件里九成是它。
        level = _configure(tmp_path)
        uvicorn.Config(_app, log_config=None, log_level=level, access_log=False)

        logging.getLogger("uvicorn.access").info('GET /v1/tasks/next HTTP/1.1 200')

        assert "/v1/tasks/next" not in _read(tmp_path)

    def test_the_access_log_lands_in_the_file_when_enabled(self, tmp_path: Path) -> None:
        level = _configure(tmp_path)
        uvicorn.Config(_app, log_config=None, log_level=level, access_log=True)

        logging.getLogger("uvicorn.access").info('GET /v1/tasks/next HTTP/1.1 200')

        assert "/v1/tasks/next" in _read(tmp_path)


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py")
        if not any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts)
    )


def _rel(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _call_name(node) == name]


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


@pytest.fixture(scope="module")
def main_func() -> ast.FunctionDef:
    for node in _parse(_MAIN).body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("supervisor/main.py 里没有 main()")


class TestMainWiresItUpFirst:
    def test_logging_is_configured_before_the_port_check(self, main_func: ast.FunctionDef) -> None:
        # 端口被占用就 sys.exit(98)，配置晚一步这条唯一说明原因的记录就没地方去。
        configured = [call.lineno for call in _calls_named(main_func, "configure_supervisor_logging")]
        bound = [call.lineno for call in _calls_named(main_func, "bind")]

        assert configured, "main() 没有调用 configure_supervisor_logging()"
        assert bound, "main() 里找不到端口检查的 bind()，这条守卫失效了"
        assert min(configured) < min(bound)

    def test_nothing_opens_the_log_file_by_hand(self) -> None:
        # 自己 open 出来的那份不轮转、不带来源，也和 uvicorn 写的不是同一个文件。
        tree = _parse(_MAIN)
        opens = [f"{call.lineno}: {ast.unparse(call)}" for call in _calls_named(tree, "open")]

        assert opens == [], f"日志文件由 configure_supervisor_logging 负责: {opens}"

    def test_the_standard_streams_are_not_redirected(self) -> None:
        # 重定向 sys.stdout 会把 TUI 的画面也一起送进日志文件。
        tree = _parse(_MAIN)
        hits = [
            f"{node.lineno}: {ast.unparse(node)}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
            and target.attr in {"stdout", "stderr"}
            and getattr(target.value, "id", "") == "sys"
        ]

        assert hits == [], f"进程的标准流不该被接走: {hits}"

    def test_uvicorn_is_told_not_to_configure_logging(self, main_func: ast.FunctionDef) -> None:
        # 留着默认 log_config，uvicorn 会给自己的 logger 装 StreamHandler 并关掉往上传。
        calls = _calls_named(main_func, "Config")
        assert len(calls) == 1, "main() 里的 uvicorn.Config 不止一处"

        log_config = _keyword(calls[0], "log_config")
        assert isinstance(log_config, ast.Constant) and log_config.value is None

    def test_uvicorn_gets_the_level_that_was_resolved(self, main_func: ast.FunctionDef) -> None:
        bound = [
            target.id
            for node in ast.walk(main_func)
            if isinstance(node, ast.Assign) and _calls_named(node.value, "configure_supervisor_logging")
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        assert bound, "configure_supervisor_logging() 的返回值没被接住"

        log_level = _keyword(_calls_named(main_func, "Config")[0], "log_level")
        assert isinstance(log_level, ast.Name) and log_level.id in bound

    def test_the_process_manager_is_not_handed_a_log_sink(self, main_func: ast.FunctionDef) -> None:
        # 给了 log_func 就绕开本模块的 logger，watchdog 那几行又回到没有时间和来源的状态。
        calls = _calls_named(main_func, "ProcessManager")
        assert calls, "main() 没有建 ProcessManager，这条守卫失效了"

        assert [call.lineno for call in calls if _keyword(call, "log_func") is not None] == []


class TestNothingElsePrintsToTheTerminal:
    def test_only_the_first_boot_hints_may_print(self) -> None:
        """print 不带时间不带来源，还在前台 TUI 画面上乱插字符。"""
        offenders: list[str] = []
        for path in _python_files(_SUPERVISOR):
            rel = _rel(path)
            allowed = _PRINT_ALLOWED.get(rel, ())
            for call in _calls_named(_parse(path), "print"):
                text = ast.unparse(call)
                if not any(marker in text for marker in allowed):
                    offenders.append(f"{rel}:{call.lineno} {text}")

        assert offenders == [], f"改用 logging，要新增 print 先在 _PRINT_ALLOWED 里登记理由: {offenders}"

    def test_the_whitelisted_files_still_print(self) -> None:
        # 登记项跟着代码一起腐烂：白名单留着而 print 早没了，下一次就白放行一整个文件。
        stale = [
            rel for rel in _PRINT_ALLOWED
            if not _calls_named(_parse(_ROOT / rel), "print")
        ]

        assert stale == [], f"这些文件已经没有 print 了，从 _PRINT_ALLOWED 里删掉: {stale}"

    def test_the_process_manager_logs_instead_of_printing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture,
    ) -> None:
        # 子进程的拉起、停止和退出码是排障第一现场，print 出去只会被前台 TUI 冲掉。
        from supervisor.process_manager import ProcessManager

        monkeypatch.setattr(ProcessManager, "_install_signal_handlers", lambda self: None)
        _configure(tmp_path)
        capfd.readouterr()
        manager = ProcessManager(
            supervisor_url="http://127.0.0.1:8765",
            workspace_root=tmp_path,
            log_dir=tmp_path / "logs",
        )
        # atexit 里的 _cleanup 会在解释器收摊之后再碰一次 logging。
        manager._stopped = True

        manager._log("[process_manager] spawned engine-1 pid=4242")

        assert "INFO supervisor.process_manager: [process_manager] spawned engine-1 pid=4242" in _read(tmp_path)
        assert capfd.readouterr().out == ""
