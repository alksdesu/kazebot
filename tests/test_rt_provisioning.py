"""建删实例：清单读不懂就停下，删到一半能接着删，没装 cloudflared 也建得起来；几处进程认同一个数据根。

部署脚本按 tests/test_provisioning.py 的路子静态检查，内嵌的那几段 bash/Python 抠出来真跑一遍。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from supervisor import instances, provisioning  # noqa: E402
from supervisor.instances import save_instance  # noqa: E402

SCRIPT = _ROOT / "deploy" / "provision_instance.sh"
BOOTSTRAP = _ROOT / "deploy" / "bootstrap.sh"
INSTALLER = _ROOT / "deploy" / "install_provision.sh"
SYSTEMD = _ROOT / "deploy" / "systemd"
_BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(_BASH is None, reason="没有 bash")


def _code(path: Path) -> list[str]:
    """去掉注释和空行的正文：禁用词写在解释为什么不用它的注释里不算。"""
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _function(path: Path, name: str) -> str:
    """脚本里一个 shell 函数的完整定义，顶格的右花括号收尾。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _heredoc(path: Path, marker: str, contains: str) -> str:
    body, blocks = "", []
    for line in path.read_text(encoding="utf-8").splitlines():
        if body and line.strip() == marker:
            blocks.append(body)
            body = ""
        elif body:
            body += line + "\n"
        elif line.rstrip().endswith(f"<<'{marker}'"):
            body = "\n"
    return next(block for block in blocks if contains in block)


def _directive(unit: str, prefix: str) -> list[str]:
    return [line for line in _code(SYSTEMD / unit) if line.startswith(prefix)]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "kazebot-data"
    root.mkdir()
    monkeypatch.setenv(config_layout.INSTANCES_FILE_ENV, str(root / "instances.yaml"))
    monkeypatch.delenv("CLONOTH_URL_PREFIX", raising=False)
    monkeypatch.setattr(instances, "port_in_use", lambda *_args, **_kwargs: False)
    return tmp_path / "workspace"


def _break_roster(workspace: Path) -> Path:
    roster = instances.instances_file(workspace)
    roster.write_text("instances: [ this is: not: valid\n", encoding="utf-8")
    return roster


# ---------------------------------------------------------------------------
#  进度
# ---------------------------------------------------------------------------

class TestProgress:
    def _log(self, workspace: Path, text: str) -> None:
        path = provisioning.log_path(workspace, "koki")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_retried_removal_is_not_read_as_the_old_failure(self, workspace: Path) -> None:
        # 重试删除时新进度接在上一次的 DONE fail 后面：往回找 DONE 的话，控制台一点重试就显示「又失败了」。
        self._log(workspace, "[10:00:00] 停服务\nDONE fail: 第 180 行执行失败\n[10:05:00] 停服务\n")

        state = provisioning.progress(workspace, "koki")

        assert state["finished"] is False
        assert (state["state"], state["action"]) == ("running", "remove")

    def test_a_success_after_a_retry_is_the_result(self, workspace: Path) -> None:
        self._log(workspace, "DONE fail: 第 1 行执行失败\n[10:05:00] 停服务\nDONE ok\n")

        state = provisioning.progress(workspace, "koki")

        assert state["finished"] and state["ok"]

    def test_a_trailing_blank_line_does_not_hide_the_result(self, workspace: Path) -> None:
        self._log(workspace, "拉起容器\nDONE fail: 容器没起来\n\n")

        state = provisioning.progress(workspace, "koki")

        assert state["finished"] and not state["ok"] and state["detail"] == "容器没起来"


# ---------------------------------------------------------------------------
#  清单读不懂就停下
# ---------------------------------------------------------------------------

class TestABrokenRoster:
    def test_planning_stops_instead_of_handing_out_a_used_index(self, workspace: Path) -> None:
        # 当成没有别的实例的话，分到的序号可能正是一个在跑的实例的端口。
        roster = _break_roster(workspace)

        with pytest.raises(provisioning.RosterUnreadable) as caught:
            provisioning.plan_instance(workspace, instance_id="koki", label="")

        _says_where_it_broke(caught.value, roster, "。先在服务器上修好它")

    def test_removing_stops_with_a_reason_instead_of_not_on_the_roster(self, workspace: Path) -> None:
        roster = _break_roster(workspace)

        with pytest.raises(provisioning.RosterUnreadable) as caught:
            provisioning.guard_removable(workspace, "koki", current_prefix="")

        _says_where_it_broke(caught.value, roster, "。先在服务器上修好它")

    def test_scaffolding_does_not_overwrite_it(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provisioning, "allocate_index", lambda *_args, **_kwargs: 1)
        monkeypatch.setattr(provisioning, "_LINKED", ())
        monkeypatch.setattr(provisioning, "link_shared_stickers", lambda *_args: True)
        plan = provisioning.plan_instance(workspace, instance_id="koki", label="")
        roster = _break_roster(workspace)
        before = roster.read_bytes()

        with pytest.raises(provisioning.RosterUnreadable) as caught:
            provisioning.scaffold(plan, workspace)

        assert roster.read_bytes() == before
        _says_where_it_broke(caught.value, roster, "。没有登记 koki")

    def test_forgetting_an_instance_says_where_the_roster_broke(self, workspace: Path) -> None:
        roster = _break_roster(workspace)
        before = roster.read_bytes()

        with pytest.raises(provisioning.RosterUnreadable) as caught:
            provisioning.forget(workspace, "koki")

        assert roster.read_bytes() == before
        _says_where_it_broke(caught.value, roster, "。先在服务器上修好它")


def _says_where_it_broke(error: Exception, roster: Path, then: str) -> None:
    """管理员要看得出是哪份文件、坏在哪一行，不能只有一句读不懂；文件里写的东西不能跟着出来。"""
    message = str(error)
    assert message.startswith(f"实例清单 {roster} 读不懂：第 1 行"), message
    assert message.endswith(then), message
    assert "this is" not in message


class TestWhoMayCreate:
    def test_an_instance_that_cannot_write_the_data_root_is_sent_to_the_main_console(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 多开实例的沙箱里数据根是只读挂载：往下走只会在建目录时撞上 EROFS，报成一个 500。
        data_root = provisioning.data_root(workspace)
        real_access = provisioning.os.access
        monkeypatch.setattr(
            provisioning.os, "access",
            lambda path, mode, **kw: False if Path(path) == data_root else real_access(path, mode, **kw),
        )

        with pytest.raises(provisioning.ProvisionError, match="主实例"):
            provisioning.plan_instance(workspace, instance_id="koki", label="")

    def test_the_id_rules_are_the_ones_the_roster_uses(self) -> None:
        assert provisioning.INSTANCE_ID_PATTERN is instances.INSTANCE_ID_PATTERN
        assert provisioning.RESERVED_IDS is instances.RESERVED_IDS
        for name in ("koki", "10001", "stickers", "Koki", "../x", ""):
            try:
                provisioning.validate_instance_id(name)
                accepted = True
            except provisioning.ProvisionError:
                accepted = False
            assert accepted is instances.is_instance_id(name), name


class TestTheInstanceEnv:
    @pytest.fixture
    def plan(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> provisioning.Plan:
        monkeypatch.setattr(provisioning, "allocate_index", lambda *_args, **_kwargs: 1)
        monkeypatch.setattr(provisioning, "_LINKED", ())
        monkeypatch.setattr(provisioning, "link_shared_stickers", lambda *_args: True)
        return provisioning.plan_instance(workspace, instance_id="koki", label="")

    def test_it_is_created_owner_only_from_the_first_byte(
        self, workspace: Path, plan: provisioning.Plan, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 先按 umask 建再 chmod 的话，中间那一瞬同机别的用户读得到里面的 OneBot 令牌。
        modes: dict[str, Any] = {}
        real = provisioning.config_layout.write_config_text

        def spy(path: Path, content: str, **kwargs: Any) -> str:
            modes[Path(path).name] = kwargs.get("mode")
            return real(path, content, **kwargs)

        monkeypatch.setattr(provisioning.config_layout, "write_config_text", spy)

        provisioning.scaffold(plan, workspace)

        assert modes[".env"] == 0o600
        assert "CLONOTH_PORT=" in (plan.workspace / ".env").read_text(encoding="utf-8")
        if os.name == "posix":
            assert (plan.workspace / ".env").stat().st_mode & 0o777 == 0o600

    def test_an_existing_env_is_left_alone(self, workspace: Path, plan: provisioning.Plan) -> None:
        # 重试建实例时 root 侧可能已经往里回填过 WebUI 令牌。
        env = plan.workspace / ".env"
        env.parent.mkdir(parents=True)
        env.write_text("NAPCAT_WEBUI_TOKEN=filled-in\n", encoding="utf-8")

        provisioning.scaffold(plan, workspace)

        assert env.read_text(encoding="utf-8") == "NAPCAT_WEBUI_TOKEN=filled-in\n"


# ---------------------------------------------------------------------------
#  root 侧建删实例脚本
# ---------------------------------------------------------------------------

class TestTheRemovalScript:
    def test_removal_no_longer_requires_the_workspace(self) -> None:
        # 上次已经归档了工作区、停在摘清单那一步的话，再删一次得能接着做完。
        body = _function(SCRIPT, "do_remove")

        assert '[ -d "$WS" ] || fail' not in body
        assert 'if [ -d "$WS" ]; then' in body

    def test_the_roster_is_edited_last(self) -> None:
        # 前面哪一步失败，控制台里那条都还在，才能再点一次删除。
        lines = [line.strip() for line in _function(SCRIPT, "do_remove").splitlines()]
        archive = next(i for i, line in enumerate(lines) if line.startswith('mv "$WS"'))
        cloudflared = next(i for i, line in enumerate(lines) if line.startswith("cf_edit remove"))
        roster = next(i for i, line in enumerate(lines) if line.startswith("flock "))

        assert cloudflared < archive < roster

    def test_cloudflared_is_optional(self) -> None:
        body = _function(SCRIPT, "cf_edit")

        assert 'if [ ! -f "$CF_CONF" ]; then' in body
        assert "return 0" in body

    def test_errors_inside_functions_still_write_a_result(self) -> None:
        # 不加 -E 的话 trap 不跟进 do_create/do_remove，日志里没有 DONE，控制台一直显示「进行中」。
        code = _code(SCRIPT)

        assert "set -E" in code
        assert "trap 'on_error $LINENO' ERR" in code

    @needs_bash
    def test_the_script_parses(self) -> None:
        done = subprocess.run([_BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)

        assert done.returncode == 0, done.stderr


def _bash_with_script_helpers(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """脚本里的 say/fail/on_error 加上 trap，后面接 body，在临时日志上真跑一遍。"""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("say() {")
    end = text.index("trap 'on_error $LINENO' ERR") + len("trap 'on_error $LINENO' ERR")
    log = tmp_path / "provision.log"
    program = "set -euo pipefail\n" + f"LOG='{log.as_posix()}'\n" + text[start:end] + "\n" + body
    runner = tmp_path / "run.sh"
    runner.write_text(program, encoding="utf-8", newline="\n")
    done = subprocess.run([_BASH, str(runner)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    return done, log


@needs_bash
class TestTheScriptHelpersRun:
    def test_a_failure_inside_a_function_writes_one_done_line(self, tmp_path: Path) -> None:
        done, log = _bash_with_script_helpers(tmp_path, "step() {\n  false\n}\nstep\necho unreachable\n")

        lines = log.read_text(encoding="utf-8").splitlines()
        assert done.returncode != 0 and "unreachable" not in done.stdout
        assert [line for line in lines if line.startswith("DONE")] == lines[-1:]
        assert lines[-1].startswith("DONE fail: ")

    def test_a_specific_reason_from_a_substitution_is_not_buried(self, tmp_path: Path) -> None:
        done, log = _bash_with_script_helpers(tmp_path, 'step() {\n  local value\n  value="$(fail "WebUI token missing")"\n}\nstep\n')

        lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("DONE")]
        assert done.returncode != 0
        assert lines == ["DONE fail: WebUI token missing"]

    def test_cloudflared_is_skipped_without_its_config(self, tmp_path: Path) -> None:
        body = (
            f"CF_CONF='{(tmp_path / 'missing.yml').as_posix()}'\nCF_EDIT=edit.py\nPY=echo\nINSTANCE=koki\n"
            + _function(SCRIPT, "cf_edit")
            + 'cf_edit add --config "$CF_CONF" --instance "$INSTANCE" --port 8775\necho finished\n'
        )

        done, log = _bash_with_script_helpers(tmp_path, body)

        assert done.returncode == 0, done.stderr
        assert "finished" in done.stdout
        assert "跳过 cloudflared" in log.read_text(encoding="utf-8")

    def test_cloudflared_is_edited_when_its_config_exists(self, tmp_path: Path) -> None:
        conf = tmp_path / "config.yml"
        conf.write_text("ingress: []\n", encoding="utf-8")
        body = (
            f"CF_CONF='{conf.as_posix()}'\nCF_EDIT=edit.py\nPY=echo\nINSTANCE=koki\n"
            + _function(SCRIPT, "cf_edit")
            + 'cf_edit remove --config "$CF_CONF" --instance "$INSTANCE"\n'
        )

        done, log = _bash_with_script_helpers(tmp_path, body)

        assert done.returncode == 0, done.stderr
        assert "edit.py remove --config" in log.read_text(encoding="utf-8")


class TestTheRosterEdit:
    """root 侧摘清单的那段 Python，在临时清单上真跑一遍。"""

    def _run(self, roster: Path, target: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _heredoc(SCRIPT, "PY", "kept"), str(roster), target],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )

    def test_the_old_list_form_is_understood(self, tmp_path: Path) -> None:
        # supervisor 认顶层就是列表的老写法，root 侧不认的话这个实例永远摘不掉。
        roster = tmp_path / "instances.yaml"
        roster.write_text("- {id: rong, path: ''}\n- {id: koki, path: /i/koki}\n", encoding="utf-8")

        done = self._run(roster, "koki")

        assert done.returncode == 0, done.stderr
        assert [row["id"] for row in yaml.safe_load(roster.read_text(encoding="utf-8"))["instances"]] == ["rong"]

    @pytest.mark.parametrize("body", ["instances: {koki: 1}\n", "instances: nope\n"])
    def test_a_roster_it_cannot_read_fails_loudly_and_is_left_alone(self, tmp_path: Path, body: str) -> None:
        roster = tmp_path / "instances.yaml"
        roster.write_text(body, encoding="utf-8")

        done = self._run(roster, "koki")

        assert done.returncode != 0
        assert roster.read_text(encoding="utf-8") == body

    @pytest.mark.parametrize("body", ["", "{}\n", "instances:\n", "instances:\n- {id: rong}\n"])
    def test_nothing_to_remove_leaves_the_file_byte_for_byte(self, tmp_path: Path, body: str) -> None:
        roster = tmp_path / "instances.yaml"
        roster.write_text(body, encoding="utf-8")

        done = self._run(roster, "koki")

        assert done.returncode == 0, done.stderr
        assert roster.read_text(encoding="utf-8") == body


# ---------------------------------------------------------------------------
#  装机脚本和单元
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_the_root_side_gets_the_system_pyyaml(self) -> None:
        # 建删实例的脚本和 cloudflared_ingress.py 以 root 跑系统解释器，不碰 kazebot 可写的 .venv。
        assert any(line.startswith("python3 python3-venv python3-pip python3-yaml") for line in _code(BOOTSTRAP))

    def test_python_311_is_the_floor(self) -> None:
        code = "\n".join(_code(BOOTSTRAP))

        assert "MIN_PYTHON='(3, 11)'" in code
        assert "(3, 10)" not in code
        assert 'as_owner "$PYTHON" -m venv "$REPO/.venv"' in code

    @needs_bash
    def test_the_script_parses(self) -> None:
        done = subprocess.run([_BASH, "-n", str(BOOTSTRAP)], capture_output=True, text=True, timeout=60)

        assert done.returncode == 0, done.stderr


class TestTheInstaller:
    def test_it_installs_pyyaml_for_the_system_python_when_missing(self) -> None:
        code = "\n".join(_code(INSTALLER))

        assert "/usr/bin/python3 -c 'import yaml'" in code
        assert "apt-get install -y -qq python3-yaml" in code

    def test_the_main_qq_adapter_gets_the_instances_file(self) -> None:
        # bot 和 supervisor 按同一个数据根认受信链接：少了它，共享表情包库在 bot 眼里就在工作区外面。
        code = _code(INSTALLER)
        dropin = next(i for i, line in enumerate(code) if "kazebot-qq.service.d/10-instances.conf" in line)

        assert code[dropin + 2] == "Environment=CLONOTH_INSTANCES_FILE=$DATA_ROOT/instances.yaml"


class TestTheUnits:
    @pytest.mark.parametrize("unit", ["kazebot-qq@.service", "kazebot-cleanup@.service"])
    def test_every_process_of_an_instance_shares_one_data_root(self, unit: str) -> None:
        expected = _directive("kazebot@.service", "Environment=CLONOTH_INSTANCES_FILE=")

        assert expected and _directive(unit, "Environment=CLONOTH_INSTANCES_FILE=") == expected

    def test_the_legacy_cleanup_unit_is_not_root_and_is_sandboxed(self) -> None:
        directives = _code(SYSTEMD / "clonoth-data-cleanup.service")

        assert not [line for line in directives if "clonoth.service" in line]
        assert any(line.startswith("User=") and line != "User=root" for line in directives)
        for wanted in ("ProtectSystem=strict", "ReadWritePaths=/opt/Clonoth/data", "NoNewPrivileges=yes", "PrivateTmp=yes"):
            assert wanted in directives
        assert any(line.startswith("ExecStart=") and line.endswith("--scope workspace") for line in directives)
