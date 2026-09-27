"""宿主清理的部署面。

NTQQ/NapCat 的缓存归 root，只有这条以 root 跑的链路碰得到 —— 单元写歪或者脚本漏装一步，
缓存就再也没人清，而每个实例的清理日志看着一切正常。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = REPO_ROOT / "deploy" / "systemd"
HOST_SERVICE = SYSTEMD / "kazebot-host-cleanup.service"
HOST_TIMER = SYSTEMD / "kazebot-host-cleanup.timer"
CLEANUP_SERVICE = SYSTEMD / "kazebot-cleanup@.service"
BOOTSTRAP = REPO_ROOT / "deploy" / "bootstrap.sh"
INSTALLER = REPO_ROOT / "deploy" / "install_provision.sh"
DEPLOY = REPO_ROOT / "deploy" / "deploy.sh"

LIB = "/usr/local/lib/kazebot"


def _directives(path: Path) -> list[str]:
    """单元文件的正文行。禁用词写在正文里是漏洞，写在解释为什么不用它的注释里不是。"""
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _script_code(path: Path) -> list[str]:
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _index(lines: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(lines) if needle in line)


def _function_body(lines: list[str], name: str) -> list[str]:
    start = _index(lines, f"{name}() {{")
    end = next(i for i, line in enumerate(lines[start:], start) if line == "}")
    return lines[start:end]


# ── 宿主清理单元 ──

def test_the_host_cleanup_runs_once_and_exits() -> None:
    assert "Type=oneshot" in _directives(HOST_SERVICE)


def test_the_host_cleanup_runs_the_root_owned_copy_with_the_system_python() -> None:
    assert f"ExecStart=/usr/bin/python3 -I {LIB}/qq_cache.py" in _directives(HOST_SERVICE)


def test_the_host_cleanup_never_touches_the_agent_writable_tree() -> None:
    # /opt/kazebot 和它的 .venv 归 kazebot，拿那边的脚本或解释器以 root 跑就是提权。
    body = "\n".join(_directives(HOST_SERVICE))

    assert ".venv" not in body
    assert "/opt/kazebot" not in body


def test_the_host_cleanup_reads_no_workspace_env_file() -> None:
    # .env 在工作区里，agent 写得动，EnvironmentFile 等于让它给 root 进程塞变量。
    assert not any(line.startswith("EnvironmentFile") for line in _directives(HOST_SERVICE))


def test_the_host_cleanup_stays_root() -> None:
    # 降成 kazebot 就删不动容器里那些 root 的缓存，这条单元也就没有存在意义了。
    assert not any(line.startswith("User=") for line in _directives(HOST_SERVICE))


def test_the_timer_points_at_the_host_cleanup_service() -> None:
    assert "Unit=kazebot-host-cleanup.service" in _directives(HOST_TIMER)


def test_the_timer_is_installed_where_systemctl_enable_looks() -> None:
    assert "WantedBy=timers.target" in _directives(HOST_TIMER)


def test_the_timer_catches_up_after_the_machine_was_off() -> None:
    # 没有 Persistent，关机跨过那一次就直接跳过，缓存多攒一整天。
    assert "Persistent=true" in _directives(HOST_TIMER)


# ── 实例侧只清自己的工作区 ──

def test_the_per_instance_cleanup_limits_itself_to_the_workspace() -> None:
    execstart = next(line for line in _directives(CLEANUP_SERVICE) if line.startswith("ExecStart="))

    assert execstart.endswith("-m engine.data_cleanup --scope workspace")


@pytest.mark.parametrize("unit", ["kazebot.service", "kazebot@.service"], ids=lambda name: name)
def test_the_supervisor_units_narrow_the_scheduled_cleanup(unit: str) -> None:
    # engine 自己按 cron 拉起的那次没有命令行，只认这个环境变量。
    assert "Environment=CLONOTH_MAINTENANCE_SCOPE=workspace" in _directives(SYSTEMD / unit)


# ── bootstrap.sh ──

def test_bootstrap_installs_the_root_side_copies_as_root() -> None:
    lines = _script_code(BOOTSTRAP)

    assert f"LIB={LIB}" in lines
    assert 'install -m 0755 -o root -g root "$REPO/deploy/deploy.sh" "$LIB/deploy.sh"' in lines
    assert 'install -m 0644 -o root -g root "$REPO/adapters/onebot/qq_cache.py" "$LIB/qq_cache.py"' in lines


def test_bootstrap_lays_down_the_root_side_before_the_units_that_run_it() -> None:
    # 单元先 enable --now，脚本还没到位，第一次触发就是 203/EXEC。
    lines = _script_code(BOOTSTRAP)
    main = _index(lines, "main() {")
    root_side = next(i for i, line in enumerate(lines[main:], main) if line == "install_root_side")
    units = next(i for i, line in enumerate(lines[main:], main) if line == "install_units")

    assert root_side < units


def test_bootstrap_installs_and_enables_the_host_cleanup_timer() -> None:
    body = _function_body(_script_code(BOOTSTRAP), "install_units")

    assert '"$REPO/deploy/systemd/kazebot-host-cleanup.service" \\' in body
    assert '"$REPO/deploy/systemd/kazebot-host-cleanup.timer" \\' in body
    assert any(line.startswith("systemctl enable --now kazebot-host-cleanup.timer") for line in body)


# ── install_provision.sh ──

def test_the_installer_puts_the_cleanup_script_next_to_the_other_root_side_copies() -> None:
    lines = _script_code(INSTALLER)

    assert f"LIB={LIB}" in lines
    assert 'install -m 0644 -o root -g root "$REPO/adapters/onebot/qq_cache.py" "$LIB/qq_cache.py"' in lines


def test_the_installer_installs_the_host_cleanup_units_where_systemd_reads_them() -> None:
    # 落进 $LIB/units 那一批只是给建实例时按名字取用的模板，systemd 不读那个目录。
    lines = _script_code(INSTALLER)
    templates = _index(lines, '"$LIB/units/"')
    service = _index(lines, '"$REPO/deploy/systemd/kazebot-host-cleanup.service"')
    timer = _index(lines, '"$REPO/deploy/systemd/kazebot-host-cleanup.timer"')

    assert templates < service < timer
    assert lines[timer + 1] == "/etc/systemd/system/"


def test_the_installer_enables_the_timer_only_after_a_daemon_reload() -> None:
    # 没 reload 就 enable，systemd 找不到刚落盘的单元。
    lines = _script_code(INSTALLER)

    assert _index(lines, "systemctl daemon-reload") < _index(lines, "systemctl enable --now kazebot-host-cleanup.timer")


def test_the_installer_drop_in_narrows_the_main_instance_cleanup() -> None:
    # 多开之后宿主清理接管了容器缓存，主实例再扫一遍只会撞权限。
    lines = _script_code(INSTALLER)
    dropin = _index(lines, "/etc/systemd/system/kazebot.service.d/10-instances.conf")
    scope = _index(lines, "Environment=CLONOTH_MAINTENANCE_SCOPE=workspace")
    stickers = _index(lines, "/etc/systemd/system/kazebot-qq.service.d")

    assert dropin < scope < stickers


# ── deploy.sh ──

def test_deploy_refreshes_the_host_cleanup_copy_after_syncing_itself() -> None:
    # 自己还是旧版就 exec 走了，那之后的同步这一趟根本跑不到。
    lines = _script_code(DEPLOY)
    self_sync = _index(lines, 'exec "$SELF" "$@"')
    refresh = _index(lines, 'install -m 0644 -o root -g root "$ROOT/adapters/onebot/qq_cache.py" "$HOST_CLEANUP"')

    assert self_sync < refresh


def test_deploy_only_refreshes_a_host_cleanup_that_is_already_installed() -> None:
    """没装宿主清理的机器上凭空长出一份 root 脚本，等于替它做了没提过的决定。"""
    lines = _script_code(DEPLOY)

    assert f"HOST_CLEANUP={LIB}/qq_cache.py" in lines
    guard = next(line for line in lines if line.startswith('if [[ -f "$HOST_CLEANUP" ]]'))
    assert '! cmp -s "$ROOT/adapters/onebot/qq_cache.py" "$HOST_CLEANUP"' in guard


# ── 语法 ──

@pytest.mark.parametrize("script", [BOOTSTRAP, INSTALLER, DEPLOY], ids=lambda p: p.name)
def test_the_deploy_scripts_parse(script: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("没有 bash，跳过语法检查")

    done = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
