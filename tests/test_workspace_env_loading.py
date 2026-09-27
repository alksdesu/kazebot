"""定工作区、灌 .env：和 bot 读同一个工作区、同一份 .env，路径覆盖和平台密钥才对得上。

离线工具（offline_workspace）只补缺，常驻进程（process_workspace）拿工作区那份盖掉继承来的值。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
from workspace import resolve_workspace_root  # noqa: E402

_NAME = "CLONOTH_TEST_WORKSPACE_ENV"
_OTHER = "CLONOTH_TEST_WORKSPACE_ENV_OTHER"
# supervisor 在 argparse 的默认值里读它：串了就是整个实例挂到别人的端口上。
_PORT = "CLONOTH_PORT"
_WORKSPACE_ENVS = ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT")


@pytest.fixture(autouse=True)
def _forget_loaded_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # load_dotenv 直接写 os.environ：先经 monkeypatch 登记一次，测完才会被撤掉；开发机上设着的工作区也先清掉。
    for name in (_NAME, _OTHER, _PORT, *_WORKSPACE_ENVS):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 真的代码目录 .env 里是开发机的密钥，灌进来会串到后面的用例。
    root = tmp_path / "code"
    root.mkdir()
    monkeypatch.setattr(clonoth_runtime, "_CODE_ROOT", root.resolve())
    monkeypatch.setattr(clonoth_runtime, "_CODE_ENV_FILE", root / ".env")
    return root


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)
    return here


def _dir(parent: Path, name: str, env: str | None = None) -> Path:
    root = parent / name
    root.mkdir()
    if env is not None:
        (root / ".env").write_text(env, encoding="utf-8")
    return root


class TestLoadWorkspaceEnv:
    def test_the_workspace_env_file_reaches_the_environment(self, tmp_path: Path, code_root: Path) -> None:
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n")

        assert clonoth_runtime.load_workspace_env(root) == root / ".env"
        assert os.environ[_NAME] == "from-workspace"

    def test_the_process_environment_is_not_overridden(
        self, tmp_path: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # systemd Environment= 和 docker -e 是部署时的显式覆盖，.env 不能把它盖回去。
        monkeypatch.setenv(_NAME, "from-process")
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n")

        clonoth_runtime.load_workspace_env(root)

        assert os.environ[_NAME] == "from-process"

    def test_the_workspace_file_wins_over_the_code_directory_one(self, tmp_path: Path, code_root: Path) -> None:
        # 多开时几个号共用一份代码，密钥和路径只能靠各自工作区那份 .env 区分。
        (code_root / ".env").write_text(f"{_NAME}=from-code\n", encoding="utf-8")
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n")

        clonoth_runtime.load_workspace_env(root)

        assert os.environ[_NAME] == "from-workspace"

    def test_without_override_it_only_fills_what_the_process_lacks(
        self, tmp_path: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_NAME, "from-process")
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n{_OTHER}=only-in-workspace\n")

        clonoth_runtime.load_workspace_env(root, override=False)

        assert os.environ[_NAME] == "from-process"
        assert os.environ[_OTHER] == "only-in-workspace"

    def test_override_lets_the_workspace_file_win_over_the_process(
        self, tmp_path: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # supervisor 以退出码 75 自己重启时环境还停在服务启动那一刻，控制台改过的密钥只能靠这里盖回来。
        monkeypatch.setenv(_NAME, "from-process")
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n")

        assert clonoth_runtime.load_workspace_env(root, override=True) == root / ".env"
        assert os.environ[_NAME] == "from-workspace"

    def test_override_does_not_reach_the_code_directory_fallback(
        self, tmp_path: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 代码目录那份是几个实例共用的，拿它盖掉本实例继承来的值就换成了主实例的端口和密钥。
        monkeypatch.setenv(_NAME, "from-process")
        (code_root / ".env").write_text(f"{_NAME}=from-code\n{_OTHER}=only-in-code\n", encoding="utf-8")
        root = _dir(tmp_path, "workspace")

        assert clonoth_runtime.load_workspace_env(root, override=True) == code_root / ".env"
        assert os.environ[_NAME] == "from-process"
        assert os.environ[_OTHER] == "only-in-code"

    def test_the_code_directory_file_is_the_fallback(self, tmp_path: Path, code_root: Path) -> None:
        (code_root / ".env").write_text(f"{_NAME}=from-code\n", encoding="utf-8")
        root = _dir(tmp_path, "workspace")

        assert clonoth_runtime.load_workspace_env(root) == code_root / ".env"
        assert os.environ[_NAME] == "from-code"

    def test_nothing_to_load_is_not_fatal(self, tmp_path: Path, code_root: Path) -> None:
        root = _dir(tmp_path, "workspace")

        assert clonoth_runtime.load_workspace_env(root) is None
        assert _NAME not in os.environ


class TestPinningTheWorkspaceRoot:
    """灌完 .env 把工作区变量钉回这个根：抄来的 .env 带着原实例的根，不钉就是这个进程用这里、它拉起的子进程认那边。"""

    def test_the_file_it_just_loaded_does_not_get_to_move_the_root(
        self, tmp_path: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        other = _dir(tmp_path, "other")
        root = _dir(tmp_path, "workspace", f"CLONOTH_WORKSPACE={other.as_posix()}\n")
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(root))

        clonoth_runtime.load_workspace_env(root, override=True)

        assert os.environ["CLONOTH_WORKSPACE"] == str(root.resolve())
        assert resolve_workspace_root(code_root) == root.resolve()

    def test_the_code_directory_fallback_pins_the_workspace_not_itself(
        self, tmp_path: Path, code_root: Path,
    ) -> None:
        (code_root / ".env").write_text(f"{_NAME}=from-code\n", encoding="utf-8")
        root = _dir(tmp_path, "workspace")

        assert clonoth_runtime.load_workspace_env(root) == code_root / ".env"
        assert os.environ["CLONOTH_WORKSPACE"] == str(root.resolve())

    def test_it_pins_even_when_there_is_nothing_to_load(self, tmp_path: Path, code_root: Path) -> None:
        root = _dir(tmp_path, "workspace")

        assert clonoth_runtime.load_workspace_env(root) is None
        assert os.environ["CLONOTH_WORKSPACE"] == str(root.resolve())

    def test_a_relative_root_is_pinned_as_an_absolute_path(
        self, tmp_path: Path, code_root: Path, cwd: Path,
    ) -> None:
        # 子进程的 cwd 是工作区，相对路径钉过去会在那边解析到别的目录。
        root = _dir(cwd, "workspace", f"{_NAME}=from-workspace\n")

        clonoth_runtime.load_workspace_env("workspace")

        assert os.environ[_NAME] == "from-workspace"
        assert os.environ["CLONOTH_WORKSPACE"] == str(root.resolve())

    def test_an_alias_that_is_already_set_follows_the_same_root(
        self, tmp_path: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 两个名字指到两个目录时，附件、记忆、admin token 会静默落到两个地方。
        monkeypatch.setenv("ONEBOT_WORKSPACE_ROOT", str(tmp_path / "elsewhere"))
        root = _dir(tmp_path, "workspace")

        clonoth_runtime.load_workspace_env(root)

        assert os.environ["CLONOTH_WORKSPACE"] == str(root.resolve())
        assert os.environ["ONEBOT_WORKSPACE_ROOT"] == str(root.resolve())

    def test_an_alias_nobody_set_is_not_created(self, tmp_path: Path, code_root: Path) -> None:
        root = _dir(tmp_path, "workspace")

        clonoth_runtime.load_workspace_env(root)

        assert os.environ["CLONOTH_WORKSPACE"] == str(root.resolve())
        assert "ONEBOT_WORKSPACE_ROOT" not in os.environ


class TestOfflineWorkspace:
    def test_an_explicit_workspace_wins_over_the_environment(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path / "elsewhere"))
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n")

        assert clonoth_runtime.offline_workspace(str(root)) == root.resolve()
        assert os.environ[_NAME] == "from-workspace"

    def test_an_explicit_workspace_is_pinned_over_the_one_in_the_environment(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # --workspace 指到哪，这趟迁移读写的就都是哪：env 里那个别的根不能再被后面的代码认到。
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(_dir(tmp_path, "elsewhere")))
        root = _dir(tmp_path, "workspace")

        assert clonoth_runtime.offline_workspace(str(root)) == root.resolve()
        assert os.environ["CLONOTH_WORKSPACE"] == str(root.resolve())
        assert resolve_workspace_root(code_root) == root.resolve()

    def test_a_workspace_set_in_the_environment_skips_the_cwd_env_file(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 在代码目录里迁别的实例：cwd 那份是主实例的 .env，灌进来就拿主实例的密钥去算别人的会话键。
        (cwd / ".env").write_text(f"{_NAME}=from-cwd\n", encoding="utf-8")
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n")
        monkeypatch.setenv("ONEBOT_WORKSPACE_ROOT", str(root))

        assert clonoth_runtime.offline_workspace() == root.resolve()
        assert os.environ[_NAME] == "from-workspace"

    def test_the_cwd_env_file_names_the_workspace_like_bot_py(
        self, tmp_path: Path, code_root: Path, cwd: Path,
    ) -> None:
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n{_OTHER}=only-in-workspace\n")
        (cwd / ".env").write_text(f"CLONOTH_WORKSPACE={root}\n{_NAME}=from-cwd\n", encoding="utf-8")

        assert clonoth_runtime.offline_workspace() == root.resolve()
        # bot 从这个 cwd 起读的就是这份；工作区那份只补它没写的。
        assert os.environ[_NAME] == "from-cwd"
        assert os.environ[_OTHER] == "only-in-workspace"

    def test_the_code_directory_env_file_names_the_workspace_without_a_cwd_one(
        self, tmp_path: Path, code_root: Path, cwd: Path,
    ) -> None:
        root = _dir(tmp_path, "workspace")
        (code_root / ".env").write_text(f"CLONOTH_WORKSPACE={root}\n", encoding="utf-8")

        assert clonoth_runtime.offline_workspace() == root.resolve()

    @pytest.mark.parametrize("blank", [None, "", "  "])
    def test_nothing_set_falls_back_to_the_code_directory(
        self, code_root: Path, cwd: Path, blank: str | None,
    ) -> None:
        assert clonoth_runtime.offline_workspace(blank) == code_root.resolve()

    def test_it_leaves_the_running_process_environment_alone(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 对照 process_workspace：离线工具跟服务跑在同一个进程组外，改掉端口和密钥只会让它自己连错地方。
        monkeypatch.setenv(_PORT, "8765")
        monkeypatch.setenv(_NAME, "from-process")
        root = _dir(tmp_path, "workspace", f"{_PORT}=8801\n{_NAME}=from-workspace\n")
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(root))

        assert clonoth_runtime.offline_workspace() == root.resolve()
        assert os.environ[_PORT] == "8765"
        assert os.environ[_NAME] == "from-process"


class TestProcessWorkspace:
    """常驻进程启动：工作区那份 .env 必须盖过 launcher 冻结下来的环境，代码目录那份一个字都不能进来。"""

    def test_the_instance_env_file_beats_the_inherited_environment(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 多开时代码目录那份是主实例的：读进来这个实例就挂到主实例的端口、拿主实例的密钥。
        (code_root / ".env").write_text(
            f"{_PORT}=8765\n{_NAME}=from-code\n{_OTHER}=only-in-code\n", encoding="utf-8",
        )
        root = _dir(tmp_path, "workspace", f"{_PORT}=8801\n{_NAME}=from-workspace\n")
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(root))
        # launcher 的环境停在服务启动那一刻，控制台改过的值还是旧的。
        monkeypatch.setenv(_PORT, "8765")
        monkeypatch.setenv(_NAME, "from-process")

        assert clonoth_runtime.process_workspace() == root.resolve()
        assert os.environ[_PORT] == "8801"
        assert os.environ[_NAME] == "from-workspace"
        # 工作区自己有 .env 就不回落，代码目录独有的键也不该冒出来。
        assert _OTHER not in os.environ

    def test_it_honours_the_other_workspace_variable(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n")
        monkeypatch.setenv("ONEBOT_WORKSPACE_ROOT", str(root))
        monkeypatch.setenv(_NAME, "from-process")

        assert clonoth_runtime.process_workspace() == root.resolve()
        assert os.environ[_NAME] == "from-workspace"

    def test_the_cwd_env_file_names_the_workspace_and_then_loses_to_it(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 没设工作区变量时和 bot.py 一样从 cwd 那份认，但认完之后当家的是工作区自己那份。
        root = _dir(tmp_path, "workspace", f"{_PORT}=8801\n{_NAME}=from-workspace\n{_OTHER}=only-in-workspace\n")
        (cwd / ".env").write_text(f"CLONOTH_WORKSPACE={root}\n{_PORT}=8765\n{_NAME}=from-cwd\n", encoding="utf-8")
        monkeypatch.setenv(_NAME, "from-process")

        assert clonoth_runtime.process_workspace() == root.resolve()
        assert os.environ[_PORT] == "8801"
        assert os.environ[_NAME] == "from-workspace"
        assert os.environ[_OTHER] == "only-in-workspace"

    def test_the_settled_root_survives_the_file_it_loaded(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 多开时 .env 常是从主实例整份抄来的，里面那行 CLONOTH_WORKSPACE 指的是主实例：
        # 不钉回来，supervisor 的 data/ 在这里，它拉起的 engine 和 bot 却认到那边。
        other = _dir(tmp_path, "other")
        root = _dir(tmp_path, "workspace", f"CLONOTH_WORKSPACE={other.as_posix()}\n")
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(root))

        assert clonoth_runtime.process_workspace() == root.resolve()
        assert os.environ["CLONOTH_WORKSPACE"] == str(root.resolve())
        # 子进程按 env 重新解析，认的必须还是这一个根。
        assert resolve_workspace_root(code_root) == root.resolve()

    def test_without_the_pin_the_children_would_go_to_the_other_root(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 钉之前就是这个样子，上面那条不是空断言。
        monkeypatch.setattr(clonoth_runtime, "pin_workspace_env", lambda _root: None)
        other = _dir(tmp_path, "other")
        root = _dir(tmp_path, "workspace", f"CLONOTH_WORKSPACE={other.as_posix()}\n")
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(root))

        assert clonoth_runtime.process_workspace() == root.resolve()
        assert resolve_workspace_root(code_root) == other.resolve()

    def test_a_workspace_without_an_env_file_keeps_the_inherited_values(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 回落到共用的代码目录那份时只补缺：systemd Environment= 给的端口不能被别人的 .env 顶掉。
        (code_root / ".env").write_text(f"{_PORT}=8765\n{_OTHER}=only-in-code\n", encoding="utf-8")
        root = _dir(tmp_path, "workspace")
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(root))
        monkeypatch.setenv(_PORT, "8801")

        assert clonoth_runtime.process_workspace() == root.resolve()
        assert os.environ[_PORT] == "8801"
        assert os.environ[_OTHER] == "only-in-code"
