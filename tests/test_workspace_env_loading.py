"""离线工具定工作区、灌 .env：和 bot 读同一个工作区、同一份 .env，路径覆盖和平台密钥才对得上。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402

_NAME = "CLONOTH_TEST_WORKSPACE_ENV"
_OTHER = "CLONOTH_TEST_WORKSPACE_ENV_OTHER"
_WORKSPACE_ENVS = ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT")


@pytest.fixture(autouse=True)
def _forget_loaded_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # load_dotenv 直接写 os.environ：先经 monkeypatch 登记一次，测完才会被撤掉；开发机上设着的工作区也先清掉。
    for name in (_NAME, _OTHER, *_WORKSPACE_ENVS):
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

    def test_the_code_directory_file_is_the_fallback(self, tmp_path: Path, code_root: Path) -> None:
        (code_root / ".env").write_text(f"{_NAME}=from-code\n", encoding="utf-8")
        root = _dir(tmp_path, "workspace")

        assert clonoth_runtime.load_workspace_env(root) == code_root / ".env"
        assert os.environ[_NAME] == "from-code"

    def test_nothing_to_load_is_not_fatal(self, tmp_path: Path, code_root: Path) -> None:
        root = _dir(tmp_path, "workspace")

        assert clonoth_runtime.load_workspace_env(root) is None
        assert _NAME not in os.environ


class TestOfflineWorkspace:
    def test_an_explicit_workspace_wins_over_the_environment(
        self, tmp_path: Path, code_root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path / "elsewhere"))
        root = _dir(tmp_path, "workspace", f"{_NAME}=from-workspace\n")

        assert clonoth_runtime.offline_workspace(str(root)) == root.resolve()
        assert os.environ[_NAME] == "from-workspace"

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
