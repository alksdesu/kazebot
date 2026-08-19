"""管理令牌的取值、落盘与日志可见性。

这个令牌能改 policy、编辑节点、重启引擎，所以三件事都要钉死：
它从哪来、写到哪、什么情况下才允许出现在日志里。
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import admin_api  # noqa: E402

_ENV = "CLONOTH_ADMIN_TOKEN"
_CONSOLE = "http://127.0.0.1:8765/web/"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    # 令牌缓存在模块全局里，不清掉的话第一个测试的结果会漏给后面所有测试。
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.delenv(_ENV, raising=False)


def _token_path(ws: Path) -> Path:
    return ws / "data" / ".admin_token"


def _written(ws: Path) -> str:
    return _token_path(ws).read_text(encoding="utf-8")


class TestPriority:
    def test_the_environment_variable_wins(self, tmp_path: Path, monkeypatch) -> None:
        _token_path(tmp_path).parent.mkdir(parents=True)
        _token_path(tmp_path).write_text("from-file", encoding="utf-8")
        monkeypatch.setenv(_ENV, "from-env")

        assert admin_api.init_admin_token(tmp_path) == "from-env"

    def test_the_file_is_rewritten_to_match_the_environment(self, tmp_path: Path, monkeypatch) -> None:
        # 适配器只读文件，不读环境变量。两边不一致就是一串无法解释的 401。
        _token_path(tmp_path).parent.mkdir(parents=True)
        _token_path(tmp_path).write_text("stale", encoding="utf-8")
        monkeypatch.setenv(_ENV, "from-env")

        admin_api.init_admin_token(tmp_path)

        assert _written(tmp_path) == "from-env"

    def test_an_existing_file_is_reused(self, tmp_path: Path) -> None:
        _token_path(tmp_path).parent.mkdir(parents=True)
        _token_path(tmp_path).write_text("already-here", encoding="utf-8")

        assert admin_api.init_admin_token(tmp_path) == "already-here"

    def test_a_restart_keeps_the_same_token(self, tmp_path: Path, monkeypatch) -> None:
        # 这条是整个改动的理由：令牌每次重启都换，等于浏览器和适配器每次都要重新配。
        first = admin_api.init_admin_token(tmp_path)

        monkeypatch.setattr(admin_api, "_admin_token", "")
        second = admin_api.init_admin_token(tmp_path)

        assert first == second

    def test_a_fresh_workspace_generates_one(self, tmp_path: Path) -> None:
        token = admin_api.init_admin_token(tmp_path)

        assert len(token) >= 24
        assert _written(tmp_path) == token


class TestTheFileIsNotTrusted:
    @pytest.mark.parametrize("content", ["", "   ", chr(10), " " + chr(9) + chr(10) + " "])
    def test_a_blank_file_is_not_a_token(self, tmp_path: Path, content: str) -> None:
        # 空串当令牌就是把 admin 面敞开：compare_digest("", "") 为真。
        _token_path(tmp_path).parent.mkdir(parents=True)
        _token_path(tmp_path).write_text(content, encoding="utf-8")

        token = admin_api.init_admin_token(tmp_path)

        assert token.strip() == token
        assert len(token) >= 24

    def test_surrounding_whitespace_is_stripped(self, tmp_path: Path) -> None:
        # 手工编辑过的文件常带一个尾换行，它不该变成令牌的一部分。
        _token_path(tmp_path).parent.mkdir(parents=True)
        _token_path(tmp_path).write_text("  padded  " + chr(10), encoding="utf-8")

        assert admin_api.init_admin_token(tmp_path) == "padded"


class TestTheTokenStaysOutOfLogs:
    def test_a_generated_token_is_offered_as_a_link(self, tmp_path: Path, capsys) -> None:
        token = admin_api.init_admin_token(tmp_path, console_url=_CONSOLE)

        assert _CONSOLE + "?token=" + token in capsys.readouterr().out

    def test_no_link_when_the_console_is_not_built(self, tmp_path: Path, capsys) -> None:
        # 前端没 build 时 /web/ 根本没挂载，给一条打不开的链接只会误导。
        token = admin_api.init_admin_token(tmp_path, console_url="")

        assert token not in capsys.readouterr().out

    def test_a_reused_token_is_never_printed(self, tmp_path: Path, capsys) -> None:
        # 只有「刚生成、你还不知道它」的令牌值得打印一次。已经落盘的再打就是白送。
        _token_path(tmp_path).parent.mkdir(parents=True)
        _token_path(tmp_path).write_text("already-here", encoding="utf-8")

        admin_api.init_admin_token(tmp_path, console_url=_CONSOLE)

        assert "already-here" not in capsys.readouterr().out

    def test_an_environment_token_is_never_printed(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv(_ENV, "secret-from-env")

        admin_api.init_admin_token(tmp_path, console_url=_CONSOLE)

        assert "secret-from-env" not in capsys.readouterr().out

    def test_the_path_is_always_reported(self, tmp_path: Path, capsys) -> None:
        # 不打印内容时，至少要让人知道去哪儿取。
        admin_api.init_admin_token(tmp_path)

        assert str(_token_path(tmp_path)) in capsys.readouterr().out


class TestOnDisk:
    def test_what_init_returns_is_what_consumers_read(self, tmp_path: Path) -> None:
        assert admin_api.init_admin_token(tmp_path) == _written(tmp_path)

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        # 适配器每次请求都扫这个目录读令牌，残留的 .tmp 迟早被谁读成令牌。
        admin_api.init_admin_token(tmp_path)

        assert [p.name for p in (tmp_path / "data").iterdir()] == [".admin_token"]

    def test_the_permission_bits_are_always_applied(self, tmp_path: Path, monkeypatch) -> None:
        # Windows 测不出 POSIX 权限位，这里退一步，只确认收权那一步没被删掉。
        seen: list[int] = []
        real = os.chmod
        monkeypatch.setattr(
            admin_api.os, "chmod",
            lambda path, mode: (seen.append(mode), real(path, mode))[1],
        )

        admin_api.init_admin_token(tmp_path)

        assert seen == [0o600]

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
    def test_the_file_is_not_readable_by_other_users(self, tmp_path: Path) -> None:
        admin_api.init_admin_token(tmp_path)

        mode = stat.S_IMODE(_token_path(tmp_path).stat().st_mode)
        assert mode == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
    def test_an_inherited_world_readable_file_is_tightened(self, tmp_path: Path) -> None:
        # 这个改动之前落盘用的是默认 umask，老部署留下的多半是 644。
        path = _token_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("inherited", encoding="utf-8")
        path.chmod(0o644)

        admin_api.init_admin_token(tmp_path)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestWhenTheDiskSaysNo:
    """目录占位、磁盘满、只读挂载 —— 写不进去不该拖垮启动。"""

    @pytest.fixture()
    def blocked(self, tmp_path: Path) -> Path:
        # 目录占住了文件名，读和写都会失败。
        _token_path(tmp_path).mkdir(parents=True)
        return tmp_path

    def test_startup_still_gets_a_token(self, blocked: Path) -> None:
        assert len(admin_api.init_admin_token(blocked)) >= 24

    def test_the_console_link_is_still_offered(self, blocked: Path, capsys) -> None:
        # 落盘失败只连累适配器，控制台照样能进，链接不该跟着消失。
        token = admin_api.init_admin_token(blocked, console_url=_CONSOLE)

        assert _CONSOLE + "?token=" + token in capsys.readouterr().out

    def test_it_does_not_claim_to_have_written_the_file(self, blocked: Path, capsys) -> None:
        admin_api.init_admin_token(blocked)
        out = capsys.readouterr().out

        assert "已生成并写入" not in out
        assert "401" in out

    def test_no_temporary_file_is_left_behind(self, blocked: Path) -> None:
        admin_api.init_admin_token(blocked)

        assert list((blocked / "data").glob("*.tmp")) == []


class TestTheWriteIsAtomic:
    def test_the_token_is_never_written_in_place(self) -> None:
        # 撕裂读在单进程里测不出来，落到适配器身上只是一次没头没尾的 401。
        # 退一步钉住写法：内容先进 tmp，再整体换上去。
        source = Path(admin_api.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _persist_token"):source.index("def init_admin_token")]

        assert "os.replace(tmp, path)" in body
        assert "path.write_text" not in body


class TestTheFallbackGetter:
    def test_it_never_returns_an_empty_string(self) -> None:
        # 没走 init 的调用方（测试、独立引用）拿到空串就等于不设防。
        assert len(admin_api.get_admin_token()) >= 24

    def test_it_prefers_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv(_ENV, "env-only")

        assert admin_api.get_admin_token() == "env-only"

    def test_it_is_stable_within_a_process(self) -> None:
        assert admin_api.get_admin_token() == admin_api.get_admin_token()
