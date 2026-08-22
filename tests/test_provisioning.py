"""从控制台建号。

这条链路会以 root 身份跑脚本，而 agent 能以 kazebot 身份任意执行命令 —— 所以校验
放宽一格就是提权，下面的边界用例都是安全回归而不是易用性回归。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from supervisor import provisioning
from supervisor.instances import (
    MAX_INDEX,
    allocate_index,
    instances_file,
    load_instances,
    ports_for,
    prefix_for,
    remove_instance,
    save_instance,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "provision_instance.sh"
INSTALLER = REPO_ROOT / "deploy" / "install_provision.sh"
POLKIT = REPO_ROOT / "deploy" / "polkit" / "49-kazebot-provision.rules"


def _script_code(path: Path) -> str:
    """剥掉注释行。禁用词在正文里是漏洞，在解释为什么不用它的注释里不是。"""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _symlink_allowed() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        probe = Path(raw)
        try:
            (probe / "link").symlink_to(probe, target_is_directory=True)
        except OSError:
            return False
        return True


# Windows 默认不给非管理员建软链，而部署目标是 Linux。
needs_symlink = pytest.mark.skipif(
    not _symlink_allowed(), reason="本机不允许建符号链接",
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "kazebot-data"
    root.mkdir()
    monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(root / "instances.yaml"))
    monkeypatch.delenv("CLONOTH_URL_PREFIX", raising=False)
    return tmp_path / "workspace"


# ── uin 校验 ──

@pytest.mark.parametrize("bad", [
    "", "   ", "abc", "1234", "0123456789", "-1", "1e9",
    "12345; rm -rf /", "12345 67890", "12345\n67890", "../../etc/passwd",
    "12345$(id)", "1234567890123", "١٢٣٤٥",
])
def test_bad_uin_is_refused(bad: str) -> None:
    with pytest.raises(provisioning.ProvisionError):
        provisioning.validate_uin(bad)


@pytest.mark.parametrize("good", ["12345", "1000000001", "12345678901"])
def test_plausible_uin_passes(good: str) -> None:
    assert provisioning.validate_uin(good) == good


def test_uin_regex_matches_the_one_in_polkit_and_shell() -> None:
    # 三处各写一遍，宽的那一处说了算。放宽任意一处都要连带改另外两处。
    shape = "[1-9][0-9]{4,10}"
    assert shape in SCRIPT.read_text(encoding="utf-8")
    assert shape in POLKIT.read_text(encoding="utf-8")
    assert shape in provisioning._UIN_RE.pattern


# ── 端口与序号 ──

def test_index_zero_keeps_single_instance_ports() -> None:
    # 序号 0 必须逐字等于改造前的单实例部署，否则升级会换掉现役端口。
    assert ports_for(0) == {"supervisor": 8765, "bot": 8080, "bridge": 8769, "napcat": 6099}


def test_ports_do_not_collide_between_indexes() -> None:
    seen: set[int] = set()
    for idx in range(MAX_INDEX + 1):
        for port in ports_for(idx).values():
            assert port not in seen, f"序号 {idx} 撞上了已分配端口 {port}"
            seen.add(port)


def test_index_zero_owns_the_site_root() -> None:
    assert prefix_for(0, "1000000001") == ""
    assert prefix_for(2, "1000000001") == "/i/1000000001"


def test_allocate_skips_taken_indexes() -> None:
    rows = [{"uin": "1", "idx": 0, "path": "", "label": "a"},
            {"uin": "2", "idx": 1, "path": "/i/2", "label": "b"}]
    assert allocate_index(rows, probe=lambda _port: False) == 2


def test_allocate_skips_live_ports_even_when_roster_is_silent() -> None:
    # 清单可能被手改漏记。端口实测是最后一道防线，撞上就换一个序号。
    busy = set(ports_for(0).values()) | set(ports_for(1).values())
    assert allocate_index([], probe=lambda port: port in busy) == 2


def test_allocate_gives_up_instead_of_wrapping() -> None:
    with pytest.raises(ValueError):
        allocate_index([], probe=lambda _port: True)


# ── 清单读写 ──

def test_save_then_load_roundtrip(workspace: Path) -> None:
    save_instance(workspace, uin="1000000001", label="主号", idx=0)
    save_instance(workspace, uin="1000000002", label="小号", idx=1)
    rows = load_instances(workspace)
    assert [r["uin"] for r in rows] == ["1000000001", "1000000002"]
    assert rows[0]["path"] == ""
    assert rows[1]["path"] == "/i/1000000002"


def test_saving_the_same_uin_twice_rewrites_instead_of_duplicating(workspace: Path) -> None:
    save_instance(workspace, uin="1000000001", label="旧", idx=0)
    rows = save_instance(workspace, uin="1000000001", label="新", idx=0)
    assert len(rows) == 1
    assert rows[0]["label"] == "新"


def test_remove_drops_only_the_named_one(workspace: Path) -> None:
    save_instance(workspace, uin="1000000001", label="a", idx=0)
    save_instance(workspace, uin="1000000002", label="b", idx=1)
    rows = remove_instance(workspace, "1000000002")
    assert [r["uin"] for r in rows] == ["1000000001"]


def test_legacy_roster_without_idx_still_loads(workspace: Path) -> None:
    # 改造前写下的清单没有 idx。根实例能推成 0，带前缀的推不出来就标 -1 交给端口探测。
    instances_file(workspace).write_text(
        "instances:\n"
        "  - uin: '1000000001'\n    label: 主号\n    path: ''\n"
        "  - uin: '1000000002'\n    label: 小号\n    path: i/1000000002\n",
        encoding="utf-8",
    )
    rows = load_instances(workspace)
    assert rows[0]["idx"] == 0
    assert rows[1]["idx"] == -1
    assert rows[1]["path"] == "/i/1000000002"


# ── 计划与脚手架 ──

def test_plan_refuses_a_uin_already_on_the_roster(workspace: Path) -> None:
    save_instance(workspace, uin="1000000001", label="主号", idx=0)
    with pytest.raises(provisioning.ProvisionError, match="已经在清单里"):
        provisioning.plan_instance(workspace, uin="1000000001", label="")


def test_plan_refuses_when_the_workspace_dir_is_already_there(workspace: Path) -> None:
    (provisioning.data_root(workspace) / "1000000002").mkdir(parents=True)
    with pytest.raises(provisioning.ProvisionError, match="已存在"):
        provisioning.plan_instance(workspace, uin="1000000002", label="")


@needs_symlink
def test_scaffold_links_code_dirs_and_writes_env(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    plan = provisioning.plan_instance(workspace, uin="1000000002", label="小号")
    provisioning.scaffold(plan, workspace)

    for name in ("adapters", "engine", "plugins", "tools"):
        assert (plan.workspace / name).is_symlink()
    env = (plan.workspace / ".env").read_text(encoding="utf-8")
    assert "CLONOTH_PORT=8775" in env
    assert "CLONOTH_URL_PREFIX=i/1000000002" in env
    assert [r["uin"] for r in load_instances(workspace)] == ["1000000002"]


def test_secondary_instance_listens_beyond_loopback_with_a_token(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # bridge 网络下容器过来是外部地址，只听回环等于反向 WS 永远连不上；开放监听就得配 token。
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    plan = provisioning.plan_instance(workspace, uin="1000000002", label="")
    body = provisioning._env_body(plan)
    assert "HOST=0.0.0.0" in body
    assert "ONEBOT_ACCESS_TOKEN=" in body
    assert len(body.split("ONEBOT_ACCESS_TOKEN=")[1].split("\n")[0]) >= 32


def test_primary_instance_stays_on_loopback(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 0)
    plan = provisioning.plan_instance(workspace, uin="1000000001", label="")
    body = provisioning._env_body(plan)
    assert "HOST=127.0.0.1" in body
    # 序号 0 的 NapCat 跑 host 网络，加 token 反而会和现役配置对不上。
    assert "ONEBOT_ACCESS_TOKEN" not in body


# ── 删除守卫 ──

def test_cannot_remove_the_instance_you_are_looking_at(workspace: Path) -> None:
    save_instance(workspace, uin="1000000002", label="小号", idx=1)
    with pytest.raises(provisioning.ProvisionError, match="正在用"):
        provisioning.guard_removable(workspace, "1000000002", current_prefix="/i/1000000002")


def test_cannot_remove_the_primary_instance(workspace: Path) -> None:
    save_instance(workspace, uin="1000000001", label="主号", idx=0)
    with pytest.raises(provisioning.ProvisionError, match="主实例"):
        provisioning.guard_removable(workspace, "1000000001", current_prefix="/i/999")


def test_cannot_remove_a_uin_that_is_not_on_the_roster(workspace: Path) -> None:
    with pytest.raises(provisioning.ProvisionError, match="不在清单"):
        provisioning.guard_removable(workspace, "1000000002", current_prefix="")


def test_removable_instance_passes(workspace: Path) -> None:
    save_instance(workspace, uin="1000000002", label="小号", idx=1)
    row = provisioning.guard_removable(workspace, "1000000002", current_prefix="")
    assert row["uin"] == "1000000002"


# ── 进度 ──

def test_progress_reads_success_marker(workspace: Path) -> None:
    provisioning.log_path(workspace, "1000000002").parent.mkdir(parents=True, exist_ok=True)
    provisioning.log_path(workspace, "1000000002").write_text("拉起容器\nDONE ok\n", encoding="utf-8")
    state = provisioning.progress(workspace, "1000000002")
    assert state["finished"] and state["ok"]


def test_progress_surfaces_the_failure_reason(workspace: Path) -> None:
    provisioning.log_path(workspace, "1000000002").parent.mkdir(parents=True, exist_ok=True)
    provisioning.log_path(workspace, "1000000002").write_text(
        "拉起容器\nDONE fail: 容器 90 秒没写出 webui.json\n", encoding="utf-8",
    )
    state = provisioning.progress(workspace, "1000000002")
    assert state["finished"] and not state["ok"]
    assert "webui.json" in state["detail"]


def test_progress_on_a_missing_log_is_not_finished(workspace: Path) -> None:
    state = provisioning.progress(workspace, "1000000002")
    assert state["lines"] == [] and not state["finished"]


# ── root 侧脚本的形状 ──
# 这些不是风格检查。每一条都对应一个能把 kazebot 变成 root 的具体写法。

def test_root_script_never_sources_the_agent_writable_env() -> None:
    body = _script_code(SCRIPT)
    for forbidden in ("source ", '. "$WS/.env"', "eval "):
        assert forbidden not in body, f"{forbidden} 会让 .env 里的内容变成 root 执行的代码"


def test_root_script_does_not_use_the_agent_writable_interpreter() -> None:
    body = _script_code(SCRIPT)
    # /opt/kazebot/.venv 是 kazebot 可写的，用它当 root 解释器等于交出机器。
    assert ".venv" not in body
    assert "PY=/usr/bin/python3" in body


def test_root_script_has_no_recursive_delete() -> None:
    body = _script_code(SCRIPT)
    assert "rm -rf" not in body and "rm -fr" not in body
    # 删号只归档，靠 mv 而不是 rm。
    assert 'mv "$WS" "$WS.deleted-' in body


def test_root_script_validates_uin_before_anything_else() -> None:
    lines = [l.strip() for l in _script_code(SCRIPT).splitlines() if l.strip()]
    guard = next(i for i, l in enumerate(lines) if "^[1-9][0-9]{4,10}$" in l)
    for risky in ("docker ", "systemctl ", "mv ", "install "):
        hits = [i for i, l in enumerate(lines) if l.startswith(risky)]
        assert all(i > guard for i in hits), f"{risky} 出现在 uin 校验之前"


def test_units_are_installed_outside_the_agent_writable_tree() -> None:
    installer = _script_code(INSTALLER)
    assert "/usr/local/lib/kazebot" in installer
    script = _script_code(SCRIPT)
    assert "UNITS=/usr/local/lib/kazebot/units" in script
    # 从 /opt/kazebot/deploy 直接装单元，agent 改一行就能让 root 跑它的东西。
    assert "$REPO/deploy/systemd" not in script


def test_polkit_rule_only_grants_start_on_the_two_templates() -> None:
    body = POLKIT.read_text(encoding="utf-8")
    assert 'subject.user !== "kazebot"' in body
    assert 'action.lookup("verb") === "start"' in body
    assert "kazebot-(provision|deprovision)@" in body
    # 授权只能是那一个 action id，多一个就是多一条提权路径。
    assert body.count("polkit.Result.YES") == 1
