"""root 侧的 NapCat 账号动作执行器：按实例名找对目录和容器，只认带对令牌的请求，不跟着符号链接走。

docker 换成假的，其余都落在真实文件上 —— 它的全部输入输出就是那几个文件。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from adapters.onebot import account_ops  # noqa: E402

RUNNER = REPO_ROOT / "deploy" / "napcat_account_runner.py"

_TOKEN = "admin-token-for-tests"
_INSTANCE = "koki"
_UIN = "10000001"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_napcat_account_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Host:
    """一台假宿主：主实例和一个多开实例各有工作区与 NapCat 目录，docker 调用只记不跑。"""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.runner = _load()
        self.docker: list[list[str]] = []
        self.docker_error: subprocess.CalledProcessError | None = None
        self.main_data = root / "kazebot" / "data"
        self.instance_data = root / "kazebot-data" / _INSTANCE / "data"
        self.main_config = root / "napcat" / "config" / "webui.json"
        self.instance_config = root / f"napcat-{_INSTANCE}" / "config" / "webui.json"
        for data in (self.main_data, self.instance_data):
            data.mkdir(parents=True)
            (data / ".admin_token").write_text(_TOKEN + "\n", encoding="utf-8")
        for config in (self.main_config, self.instance_config):
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"token": "webui", "autoLoginAccount": "20000002"}), encoding="utf-8")
        monkeypatch.setattr(self.runner, "MAIN_WORKSPACE", root / "kazebot")
        monkeypatch.setattr(self.runner, "DATA_ROOT", root / "kazebot-data")
        monkeypatch.setattr(self.runner, "NAPCAT_HOME", str(root / "napcat"))
        monkeypatch.setattr(self.runner.subprocess, "run", self._run)

    def _run(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        self.docker.append(list(command))
        if self.docker_error is not None:
            raise self.docker_error
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def ask(self, data: Path, **request: Any) -> None:
        payload = {"nonce": "n-1", "token": _TOKEN, **request}
        (data / "napcat-account.request").write_text(json.dumps(payload), encoding="utf-8")

    def run(self, *instance: str) -> int:
        return self.runner.main(["napcat_account_runner.py", *instance])

    @staticmethod
    def result(data: Path) -> dict[str, Any]:
        return json.loads((data / "napcat-account.result").read_text(encoding="utf-8"))

    @staticmethod
    def auto_login(config: Path) -> str:
        return json.loads(config.read_text(encoding="utf-8"))["autoLoginAccount"]


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    return _Host(tmp_path.resolve(), monkeypatch)


class TestTargets:
    def test_no_instance_name_is_the_main_instance(self) -> None:
        target = _load().target_for("")

        assert (str(target.data_dir), str(target.webui_config), target.container) == (
            "/opt/kazebot/data", "/opt/napcat/config/webui.json", "napcat",
        )

    def test_an_instance_gets_its_own_workspace_config_and_container(self) -> None:
        target = _load().target_for("koki")

        assert (str(target.data_dir), str(target.webui_config), target.container) == (
            "/opt/kazebot-data/koki/data", "/opt/napcat-koki/config/webui.json", "napcat-koki",
        )

    @pytest.mark.parametrize("name", ["../etc", "a", "Koki", "ko ki", "ko/ki", "1234", "stickers", "shared", "koki\n"])
    def test_a_name_that_could_leave_its_directory_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError):
            _load().target_for(name)

    def test_the_name_shape_matches_the_provisioning_script(self) -> None:
        script = (REPO_ROOT / "deploy" / "provision_instance.sh").read_text(encoding="utf-8")

        assert _load().INSTANCE_RE.pattern in script

    def test_the_file_names_are_the_ones_the_supervisor_writes_and_reads(self) -> None:
        runner = _load()

        assert (runner.REQUEST_NAME, runner.RESULT_NAME) == (account_ops._REQUEST_NAME, account_ops._RESULT_NAME)


class TestActions:
    def test_an_instance_request_restarts_that_instances_container(self, host: _Host) -> None:
        host.ask(host.instance_data, action="restart")

        assert host.run(_INSTANCE) == 0
        assert host.docker == [["docker", "restart", f"napcat-{_INSTANCE}"]]
        assert host.result(host.instance_data) == {"nonce": "n-1", "ok": True, "message": "ok"}
        assert not (host.instance_data / "napcat-account.request").exists()

    def test_the_main_instance_restarts_the_plain_container(self, host: _Host) -> None:
        host.ask(host.main_data, action="restart")

        assert host.run() == 0
        assert host.docker == [["docker", "restart", "napcat"]]
        assert host.result(host.main_data)["ok"] is True

    def test_auto_login_is_written_to_that_instances_config_only(self, host: _Host) -> None:
        host.ask(host.instance_data, action="set-auto", uin=_UIN)

        assert host.run(_INSTANCE) == 0
        assert host.auto_login(host.instance_config) == _UIN
        assert host.auto_login(host.main_config) == "20000002"
        assert json.loads(host.instance_config.read_text(encoding="utf-8"))["token"] == "webui"

    def test_clearing_auto_login_leaves_it_empty(self, host: _Host) -> None:
        host.ask(host.instance_data, action="clear-auto")

        assert host.run(_INSTANCE) == 0
        assert host.auto_login(host.instance_config) == ""

    def test_a_request_in_another_workspace_is_not_picked_up(self, host: _Host) -> None:
        host.ask(host.main_data, action="restart")

        assert host.run(_INSTANCE) == 0
        assert host.docker == []
        assert (host.main_data / "napcat-account.request").exists()


class TestRefusals:
    def test_a_wrong_token_changes_nothing(self, host: _Host) -> None:
        host.ask(host.instance_data, action="clear-auto", token="guess")

        assert host.run(_INSTANCE) == 0
        assert host.result(host.instance_data) == {"nonce": "n-1", "ok": False, "message": "令牌校验失败"}
        assert host.auto_login(host.instance_config) == "20000002"

    def test_another_instances_token_is_not_accepted(self, host: _Host) -> None:
        (host.main_data / ".admin_token").write_text("main-only", encoding="utf-8")
        host.ask(host.instance_data, action="restart", token="main-only")

        assert host.run(_INSTANCE) == 0
        assert host.result(host.instance_data)["message"] == "令牌校验失败"
        assert host.docker == []

    def test_a_missing_token_file_refuses_everything(self, host: _Host) -> None:
        (host.instance_data / ".admin_token").unlink()
        host.ask(host.instance_data, action="restart", token="")

        assert host.run(_INSTANCE) == 0
        assert host.result(host.instance_data)["ok"] is False
        assert host.docker == []

    @pytest.mark.parametrize("uin", ["", "abc", "1234", "1" * 13, "10000001; reboot", "10000001\n"])
    def test_a_malformed_account_is_not_written(self, host: _Host, uin: str) -> None:
        host.ask(host.instance_data, action="set-auto", uin=uin)

        assert host.run(_INSTANCE) == 0
        assert host.result(host.instance_data)["message"] == "QQ 号格式不对"
        assert host.auto_login(host.instance_config) == "20000002"

    def test_an_unknown_action_is_named_back(self, host: _Host) -> None:
        host.ask(host.instance_data, action="remove")

        assert host.run(_INSTANCE) == 0
        assert host.result(host.instance_data) == {"nonce": "n-1", "ok": False, "message": "未知动作: remove"}

    def test_unreadable_json_is_answered_and_cleared(self, host: _Host) -> None:
        (host.instance_data / "napcat-account.request").write_text("{not json", encoding="utf-8")

        assert host.run(_INSTANCE) == 0
        assert host.result(host.instance_data) == {"nonce": "", "ok": False, "message": "请求不是合法 JSON"}
        assert not (host.instance_data / "napcat-account.request").exists()

    def test_a_failed_restart_passes_on_what_docker_said(self, host: _Host) -> None:
        host.docker_error = subprocess.CalledProcessError(1, ["docker"], stderr="No such container".encode())
        host.ask(host.instance_data, action="restart")

        assert host.run(_INSTANCE) == 0
        assert host.result(host.instance_data) == {"nonce": "n-1", "ok": False, "message": "No such container"}

    def test_an_invalid_instance_name_exits_without_touching_anything(self, host: _Host) -> None:
        host.ask(host.instance_data, action="restart")

        assert host.run("../" + _INSTANCE) == 2
        assert host.docker == []
        assert (host.instance_data / "napcat-account.request").exists()

    def test_nothing_to_do_is_not_an_error(self, host: _Host) -> None:
        assert host.run(_INSTANCE) == 0
        assert not (host.instance_data / "napcat-account.result").exists()


class TestSymlinks:
    def test_a_linked_request_is_removed_unread(self, host: _Host, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps({"nonce": "n-1", "token": _TOKEN, "action": "restart"}), encoding="utf-8")
        request = host.instance_data / "napcat-account.request"
        request.symlink_to(elsewhere)

        assert host.run(_INSTANCE) == 0
        assert host.docker == []
        assert not request.is_symlink() and elsewhere.exists()
        assert not (host.instance_data / "napcat-account.result").exists()

    def test_the_result_does_not_follow_a_planted_link(self, host: _Host, tmp_path: Path) -> None:
        victim = tmp_path / "victim.conf"
        victim.write_text("untouched", encoding="utf-8")
        result = host.instance_data / "napcat-account.result"
        result.symlink_to(victim)
        host.ask(host.instance_data, action="clear-auto")

        assert host.run(_INSTANCE) == 0
        assert victim.read_text(encoding="utf-8") == "untouched"
        assert not result.is_symlink()
        assert host.result(host.instance_data)["ok"] is True

    def test_a_linked_token_is_not_read(self, host: _Host, tmp_path: Path) -> None:
        secret = tmp_path / "root-only"
        secret.write_text("root-secret", encoding="utf-8")
        token = host.instance_data / ".admin_token"
        token.unlink()
        token.symlink_to(secret)
        host.ask(host.instance_data, action="restart", token="root-secret")

        assert host.run(_INSTANCE) == 0
        assert host.result(host.instance_data)["message"] == "令牌校验失败"
        assert host.docker == []

    @pytest.mark.parametrize("linked", ["data", "workspace"])
    def test_a_linked_workspace_is_left_alone(self, host: _Host, tmp_path: Path, linked: str) -> None:
        workspace = host.instance_data.parent
        real = tmp_path / "real"
        if linked == "data":
            host.instance_data.rename(real)
            host.instance_data.symlink_to(real, target_is_directory=True)
        else:
            workspace.rename(real)
            workspace.symlink_to(real, target_is_directory=True)
        host.ask(host.instance_data, action="restart")

        assert host.run(_INSTANCE) == 1
        assert host.docker == []
        assert (host.instance_data / "napcat-account.request").exists()
