"""从控制台开一个新实例。

这条链路会以 root 身份跑脚本，而 agent 能以 kazebot 身份任意执行命令 —— 所以校验
放宽一格就是提权，下面的边界用例都是安全回归而不是易用性回归。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import yaml

from adapters.onebot import provisioning as qq_provisioning
from platforms.descriptor import (
    CHANNEL_GROUP,
    HOOK_INSTANCE_ENV,
    ChannelDescriptor,
    InstancePort,
    PlatformDescriptor,
)
from platforms.registry import register_platform, unregister_platform
from supervisor import instances, provisioning
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
NEW_INSTANCE = REPO_ROOT / "deploy" / "new_instance.sh"
CLOUDFLARED = REPO_ROOT / "deploy" / "cloudflared_ingress.py"
POLKIT = REPO_ROOT / "deploy" / "polkit" / "49-kazebot-provision.rules"
INSTALL_DOC = REPO_ROOT / "deploy" / "INSTALL.md"
BOOTSTRAP = REPO_ROOT / "deploy" / "bootstrap.sh"
README = REPO_ROOT / "README.md"
README_EN = REPO_ROOT / "README.en.md"


def _script_code(path: Path) -> str:
    """剥掉注释行。禁用词在正文里是漏洞，在解释为什么不用它的注释里不是。"""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _heredoc(path: Path, marker: str, contains: str) -> str:
    """把脚本里内嵌的那段 Python 抠出来，好在临时数据上真跑一遍。"""
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


@contextmanager
def _extra_platform(platform_id: str, **fields: Any) -> Iterator[PlatformDescriptor]:
    platform = PlatformDescriptor(
        id=platform_id, display_name="别的平台", short_name="X",
        channels=(ChannelDescriptor(f"{platform_id}_group", CHANNEL_GROUP),), **fields,
    )
    register_platform(platform)
    try:
        yield platform
    finally:
        unregister_platform(platform.id)


# ── 实例名校验 ──

@pytest.mark.parametrize("bad", [
    "", "   ", "a", "1234", "0123456", "1abc", "Xiaoyu", "xiao-yu", "xiao yu", "_xiaoyu",
    "x" * 33, "0123456789", "-1", "1e9", "1234567890123",
    "12345; rm -rf /", "12345 67890", "12345\n67890", "../../etc/passwd",
    "12345$(id)", "xiaoyu$(id)", "١٢٣٤٥",
])
def test_a_bad_instance_name_is_refused(bad: str) -> None:
    with pytest.raises(provisioning.ProvisionError):
        provisioning.validate_instance_id(bad)


@pytest.mark.parametrize("good", ["xiaoyu", "bot_2", "ab", "x" * 32, "1000000002", "12345", "12345678901"])
def test_a_plausible_instance_name_passes(good: str) -> None:
    assert provisioning.validate_instance_id(good) == good


def test_a_name_that_is_already_a_shared_directory_is_refused() -> None:
    # 数据根下的共享表情包库就叫这个名字，实例叫它的话工作区和库是同一个目录。
    assert "stickers" in provisioning.RESERVED_IDS
    with pytest.raises(provisioning.ProvisionError, match="stickers"):
        provisioning.validate_instance_id("stickers")


def test_the_instance_name_regex_is_the_same_word_for_word_in_all_six_places() -> None:
    # 六处各写一遍，宽的那一处说了算。放宽任意一处都要连带改另外五处。
    shape = "[a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10}"
    assert shape == provisioning.INSTANCE_ID_PATTERN
    for path in (SCRIPT, POLKIT, CLOUDFLARED, NEW_INSTANCE, INSTALLER):
        assert shape in _script_code(path), f"{path.name} 里的实例名正则和 provisioning.py 对不上"


# ── 登记的平台账号 ──

def test_no_accounts_is_an_instance_nobody_has_logged_in_yet() -> None:
    assert provisioning.normalize_accounts(None) == {}
    assert provisioning.normalize_accounts({}) == {}


def test_accounts_must_be_a_mapping_of_platform_to_account() -> None:
    with pytest.raises(provisioning.ProvisionError, match="平台 id"):
        provisioning.normalize_accounts([("qq", "1000000002")])


def test_a_blank_account_is_skipped_rather_than_refused() -> None:
    # 前端的表单里留空就是「这个平台先不登」。
    assert provisioning.normalize_accounts({"qq": "  "}) == {}
    assert provisioning.normalize_accounts({"qq": None}) == {}


def test_an_account_is_normalised_to_the_platform_id_shape() -> None:
    assert provisioning.normalize_accounts({"qq": "  1000000002 "}) == {"qq": "1000000002"}


@pytest.mark.parametrize("name", ["nowhere", "onebot", "onebot11", "qq_group", ""])
def test_an_unknown_platform_name_is_refused(name: str) -> None:
    # 按 id 认，别名和通道名都不算：清单里的键就是这个 id，认宽了两处会对不上。
    with pytest.raises(provisioning.ProvisionError, match="不认识的平台"):
        provisioning.normalize_accounts({name: "1000000002"})


@pytest.mark.parametrize("account", ["abc", "10000 00002", "1000000002; id", "١٢٣٤٥"])
def test_an_account_that_does_not_fit_the_platform_is_refused(account: str) -> None:
    with pytest.raises(provisioning.ProvisionError, match="账号写法不对"):
        provisioning.normalize_accounts({"qq": account})


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
    assert prefix_for(0, "xiaoyu") == ""
    assert prefix_for(2, "xiaoyu") == "/i/xiaoyu"


def test_allocate_skips_taken_indexes() -> None:
    rows = [{"id": "rong", "idx": 0, "path": "", "label": "a"},
            {"id": "koki", "idx": 1, "path": "/i/koki", "label": "b"}]
    assert allocate_index(rows, probe=lambda _port: False) == 2


def test_allocate_skips_live_ports_even_when_roster_is_silent() -> None:
    # 清单可能被手改漏记。端口实测是最后一道防线，撞上就换一个序号。
    busy = set(ports_for(0).values()) | set(ports_for(1).values())
    assert allocate_index([], probe=lambda port: port in busy) == 2


def test_allocate_gives_up_instead_of_wrapping() -> None:
    with pytest.raises(ValueError):
        allocate_index([], probe=lambda _port: True)


@pytest.fixture()
def quiet_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """端口探测换成确定的「都空着」。本机真占着 8080 的话，分配结果会跟着机器变。"""
    monkeypatch.setattr(instances, "port_in_use", lambda *_a, **_k: False)


@pytest.fixture()
def busy_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instances, "port_in_use", lambda *_a, **_k: True)


def test_plan_turns_a_used_up_range_into_a_refusal(workspace: Path, busy_ports: None) -> None:
    # 序号耗尽是要告诉操作的人的话，不是一个 500。
    with pytest.raises(provisioning.ProvisionError, match="没有空闲序号了"):
        provisioning.plan_instance(workspace, instance_id="koki", label="")


def test_plan_refuses_when_the_roster_already_claims_every_index(workspace: Path, quiet_ports: None) -> None:
    for n in range(MAX_INDEX + 1):
        save_instance(workspace, instance_id=f"inst{n}", label="", idx=n)
    with pytest.raises(provisioning.ProvisionError, match="没有空闲序号了"):
        provisioning.plan_instance(workspace, instance_id="koki", label="")


def test_plan_does_not_dress_up_a_broken_port_table(workspace: Path, quiet_ports: None) -> None:
    # 端口表写错是代码错误，翻成 400 就成了「你的输入有问题」，真正的重名没人看见。
    with _extra_platform("portclash", instance_ports=(InstancePort("bot", 9300, 10),)):
        with pytest.raises(ValueError, match="端口名重复") as caught:
            provisioning.plan_instance(workspace, instance_id="koki", label="")
    assert not isinstance(caught.value, (provisioning.ProvisionError, instances.NoFreeIndex))


# ── 清单读写 ──

def test_save_then_load_roundtrip(workspace: Path) -> None:
    save_instance(workspace, instance_id="rong", label="主号", idx=0)
    save_instance(workspace, instance_id="koki", label="小号", idx=1)
    rows = load_instances(workspace)
    assert [r["id"] for r in rows] == ["rong", "koki"]
    assert rows[0]["path"] == ""
    assert rows[1]["path"] == "/i/koki"


def test_saving_the_same_name_twice_rewrites_instead_of_duplicating(workspace: Path) -> None:
    save_instance(workspace, instance_id="rong", label="旧", idx=0)
    rows = save_instance(workspace, instance_id="rong", label="新", idx=0)
    assert len(rows) == 1
    assert rows[0]["label"] == "新"


def test_remove_drops_only_the_named_one(workspace: Path) -> None:
    save_instance(workspace, instance_id="rong", label="a", idx=0)
    save_instance(workspace, instance_id="koki", label="b", idx=1)
    rows = remove_instance(workspace, "koki")
    assert [r["id"] for r in rows] == ["rong"]


def test_legacy_roster_without_idx_still_loads(workspace: Path) -> None:
    # 改造前写下的清单没有 idx，也没有 id。根实例能推成 0，带前缀的推不出来就标 -1 交给端口探测。
    instances_file(workspace).write_text(
        "instances:\n"
        "  - uin: '1000000001'\n    label: 主号\n    path: ''\n"
        "  - uin: '1000000002'\n    label: 小号\n    path: i/1000000002\n",
        encoding="utf-8",
    )
    rows = load_instances(workspace)
    assert [r["id"] for r in rows] == ["1000000001", "1000000002"]
    assert rows[0]["idx"] == 0
    assert rows[1]["idx"] == -1
    assert rows[1]["path"] == "/i/1000000002"
    assert rows[1]["accounts"] == {"qq": "1000000002"}


# ── 计划与脚手架 ──

def test_plan_refuses_a_name_already_on_the_roster(workspace: Path) -> None:
    save_instance(workspace, instance_id="rong", label="主号", idx=0)
    with pytest.raises(provisioning.ProvisionError, match="已经在清单里"):
        provisioning.plan_instance(workspace, instance_id="rong", label="")


def test_plan_refuses_an_account_another_instance_already_holds(workspace: Path) -> None:
    # 同一个号两边登会互相踢下线，数据也会分成两份。
    save_instance(workspace, instance_id="rong", label="主号", idx=0, accounts={"qq": "1000000001"})
    with pytest.raises(provisioning.ProvisionError, match="已经归实例 主号 管了"):
        provisioning.plan_instance(workspace, instance_id="koki", label="", accounts={"qq": "1000000001"})


@pytest.mark.parametrize("accounts", [
    {"qq": " 1000000001 "},
    {"  QQ  ": "1000000001"},
    {"QQ": "\t1000000001 "},
])
def test_plan_refuses_a_held_account_however_it_was_typed(workspace: Path, accounts: dict[str, str]) -> None:
    # 请求体里的写法拦不住冲突检查：normalize_accounts 和清单查询走的是同一套清洗。
    save_instance(workspace, instance_id="rong", label="主号", idx=0, accounts={"qq": "1000000001"})
    with pytest.raises(provisioning.ProvisionError, match="已经归实例 主号 管了"):
        provisioning.plan_instance(workspace, instance_id="koki", label="", accounts=accounts)


def test_plan_counts_the_instances_hanging_under_a_prefix_too(workspace: Path) -> None:
    # 新实例还没有自己的前缀，谁都不该被当成「那就是我」放过去。
    save_instance(workspace, instance_id="koki", label="小号", idx=1, accounts={"qq": "1000000002"})
    with pytest.raises(provisioning.ProvisionError, match="已经归实例 小号 管了"):
        provisioning.plan_instance(workspace, instance_id="yu", label="", accounts={"qq": "1000000002"})


def test_plan_lets_through_an_account_nobody_holds(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 2)
    save_instance(workspace, instance_id="rong", label="主号", idx=0, accounts={"qq": "1000000001"})
    plan = provisioning.plan_instance(workspace, instance_id="koki", label="", accounts={"qq": "1000000002"})
    assert plan.accounts == {"qq": "1000000002"}


def test_plan_refuses_when_the_workspace_dir_is_already_there(workspace: Path) -> None:
    (provisioning.data_root(workspace) / "koki").mkdir(parents=True)
    with pytest.raises(provisioning.ProvisionError, match="已存在"):
        provisioning.plan_instance(workspace, instance_id="koki", label="")


def test_plan_refuses_when_the_disk_is_nearly_full(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provisioning.shutil, "disk_usage", lambda _path: SimpleNamespace(total=0, used=0, free=1024**3),
    )
    with pytest.raises(provisioning.ProvisionError, match="不够开新实例"):
        provisioning.plan_instance(workspace, instance_id="koki", label="")


def test_a_plan_carries_the_prefix_ports_and_accounts(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    plan = provisioning.plan_instance(
        workspace, instance_id="koki", label=" 小号 ", accounts={"qq": " 1000000002 "},
    )
    assert plan.idx == 1 and plan.label == "小号"
    assert plan.prefix == "/i/koki"
    assert plan.ports == ports_for(1)
    assert plan.accounts == {"qq": "1000000002"}
    assert plan.workspace == provisioning.data_root(workspace) / "koki"


def test_a_nameless_plan_is_labelled_with_its_own_name(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    assert provisioning.plan_instance(workspace, instance_id="koki", label="").label == "koki"


@needs_symlink
def test_scaffold_links_code_dirs_and_writes_env(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    plan = provisioning.plan_instance(
        workspace, instance_id="koki", label="小号", accounts={"qq": "1000000002"},
    )
    provisioning.scaffold(plan, workspace)

    for name in ("adapters", "engine", "plugins", "tools"):
        assert (plan.workspace / name).is_symlink()
    env = (plan.workspace / ".env").read_text(encoding="utf-8")
    assert "CLONOTH_PORT=8775" in env
    assert "CLONOTH_URL_PREFIX=i/koki" in env
    rows = load_instances(workspace)
    assert [r["id"] for r in rows] == ["koki"]
    assert rows[0]["accounts"] == {"qq": "1000000002"}


# ── .env 的内容 ──

def _plan(workspace: Path, monkeypatch: pytest.MonkeyPatch, idx: int, instance_id: str = "koki") -> provisioning.Plan:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: idx)
    return provisioning.plan_instance(workspace, instance_id=instance_id, label="")


def test_secondary_instance_listens_beyond_loopback_with_a_token(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bridge 网络下容器过来是外部地址，只听回环等于反向 WS 永远连不上；开放监听就得配 token。
    body = provisioning._env_body(_plan(workspace, monkeypatch, 1))
    assert "HOST=0.0.0.0" in body
    assert "ONEBOT_ACCESS_TOKEN=" in body
    assert len(body.split("ONEBOT_ACCESS_TOKEN=")[1].split("\n")[0]) >= 32


def test_primary_instance_stays_on_loopback(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = provisioning._env_body(_plan(workspace, monkeypatch, 0, "rong"))
    assert "HOST=127.0.0.1" in body
    # 序号 0 的 NapCat 跑 host 网络，加 token 反而会和现役配置对不上。
    assert "ONEBOT_ACCESS_TOKEN" not in body


def test_the_env_opens_with_the_instance_and_its_supervisor_port(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = provisioning._env_body(_plan(workspace, monkeypatch, 1)).splitlines()
    assert lines[0] == "# 实例 koki (koki)"
    assert lines[1] == "CLONOTH_PORT=8775"


def test_the_qq_lines_carry_this_instance_ports(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = provisioning._env_body(_plan(workspace, monkeypatch, 1))
    assert "PORT=8090" in body
    assert "ONEBOT_FORWARD_BRIDGE_PORT=8779" in body
    assert "NAPCAT_WEBUI_URL=http://127.0.0.1:6199" in body
    # 容器首启才会生成，由 root 侧读出来回填。
    assert "NAPCAT_WEBUI_TOKEN=\n" in body


def test_the_url_prefix_is_the_last_line(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 前面几行是平台自己写的，谁也不该排在挂载前缀之后。
    body = provisioning._env_body(_plan(workspace, monkeypatch, 1))
    assert body.splitlines()[-1] == "CLONOTH_URL_PREFIX=i/koki"


def test_the_root_instance_gets_no_prefix_line(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "CLONOTH_URL_PREFIX" not in provisioning._env_body(_plan(workspace, monkeypatch, 0, "rong"))


def test_a_platform_without_the_hook_writes_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _extra_platform("silent", instance_ports=(InstancePort("hush", 9300, 10),)):
        body = provisioning._env_body(_plan(workspace, monkeypatch, 1))
    assert "9310" not in body
    assert "hush" not in body


def test_a_platform_only_sees_the_ports_it_declared(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 把别的平台的端口递过去，它会照着算出一个不属于自己的监听地址。
    seen: list[dict[str, int]] = []

    def instance_env(index: int, ports: dict[str, int]) -> list[str]:
        seen.append(dict(ports))
        return [f"NOISY_INDEX={index}", f"NOISY_PORT={ports['noisy']}"]

    stub = SimpleNamespace(instance_env=instance_env)
    real = provisioning.platform_hook

    def lookup(platform: PlatformDescriptor, name: str) -> Any:
        return stub if platform.id == "noisy" and name == HOOK_INSTANCE_ENV else real(platform, name)

    monkeypatch.setattr(provisioning, "platform_hook", lookup)
    with _extra_platform(
        "noisy",
        instance_ports=(InstancePort("noisy", 9300, 10),),
        hooks={HOOK_INSTANCE_ENV: "noisy.provisioning"},
    ):
        body = provisioning._env_body(_plan(workspace, monkeypatch, 1))

    assert seen == [{"noisy": 9310}]
    assert "NOISY_INDEX=1" in body and "NOISY_PORT=9310" in body


def test_a_hook_module_that_breaks_on_import_is_not_a_missing_hook(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跳过等于悄悄开出一个缺了这个平台配置的实例，起来才发现连不上。"""
    def lookup(platform: PlatformDescriptor, name: str) -> Any:
        raise KeyError("napcat ports")

    monkeypatch.setattr(provisioning, "platform_hook", lookup)
    plan = _plan(workspace, monkeypatch, 1)

    with pytest.raises(KeyError):
        provisioning._platform_env(plan)


# ── QQ 侧写进 .env 的那几行 ──

def test_the_primary_qq_instance_listens_on_loopback_without_a_token() -> None:
    lines = qq_provisioning.instance_env(0, {"bot": 8080, "bridge": 8769, "napcat": 6099})
    assert lines == [
        "HOST=127.0.0.1",
        "PORT=8080",
        "ONEBOT_FORWARD_BRIDGE_PORT=8769",
        "NAPCAT_WEBUI_URL=http://127.0.0.1:6099",
        "NAPCAT_WEBUI_TOKEN=",
    ]


def test_every_other_qq_instance_opens_up_and_gets_its_own_token() -> None:
    lines = qq_provisioning.instance_env(2, {"bot": 8100, "bridge": 8789, "napcat": 6299})
    assert lines[:5] == [
        "HOST=0.0.0.0",
        "PORT=8100",
        "ONEBOT_FORWARD_BRIDGE_PORT=8789",
        "NAPCAT_WEBUI_URL=http://127.0.0.1:6299",
        "NAPCAT_WEBUI_TOKEN=",
    ]
    token = lines[5].removeprefix("ONEBOT_ACCESS_TOKEN=")
    assert len(lines) == 6 and len(token) == 48 and int(token, 16) >= 0


def test_two_instances_never_share_a_token() -> None:
    ports = {"bot": 8100, "bridge": 8789, "napcat": 6299}
    assert qq_provisioning.instance_env(2, ports)[5] != qq_provisioning.instance_env(2, ports)[5]


# ── 删除守卫 ──

def test_cannot_remove_the_instance_you_are_looking_at(workspace: Path) -> None:
    save_instance(workspace, instance_id="koki", label="小号", idx=1)
    with pytest.raises(provisioning.ProvisionError, match="正在用"):
        provisioning.guard_removable(workspace, "koki", current_prefix="/i/koki")


@pytest.mark.parametrize("prefix", ["i/koki", "/i/koki/", " /i/koki ", "///i/koki///"])
def test_cannot_remove_yourself_however_the_prefix_is_written(workspace: Path, prefix: str) -> None:
    # 差一个斜杠就认不出这是自己，控制台会把自己脚下的实例删掉。
    save_instance(workspace, instance_id="koki", label="小号", idx=1)
    with pytest.raises(provisioning.ProvisionError, match="正在用"):
        provisioning.guard_removable(workspace, "koki", current_prefix=prefix)


def test_another_instance_is_still_removable_from_here(workspace: Path) -> None:
    save_instance(workspace, instance_id="koki", label="小号", idx=1)
    save_instance(workspace, instance_id="yu", label="三号", idx=2)
    assert provisioning.guard_removable(workspace, "yu", current_prefix="i/koki")["id"] == "yu"


def test_cannot_remove_the_primary_instance(workspace: Path) -> None:
    save_instance(workspace, instance_id="rong", label="主号", idx=0)
    with pytest.raises(provisioning.ProvisionError, match="主实例"):
        provisioning.guard_removable(workspace, "rong", current_prefix="/i/koki")


def test_cannot_remove_a_name_that_is_not_on_the_roster(workspace: Path) -> None:
    with pytest.raises(provisioning.ProvisionError, match="不在清单"):
        provisioning.guard_removable(workspace, "koki", current_prefix="")


def test_removable_instance_passes(workspace: Path) -> None:
    save_instance(workspace, instance_id="koki", label="小号", idx=1, accounts={"qq": "1000000002"})
    row = provisioning.guard_removable(workspace, "koki", current_prefix="")
    assert row["id"] == "koki" and row["accounts"] == {"qq": "1000000002"}


@pytest.mark.parametrize("name", ["stickers", "Koki", "k", "../rong"])
def test_a_malformed_name_never_reaches_the_roster(workspace: Path, name: str) -> None:
    with pytest.raises(provisioning.ProvisionError):
        provisioning.guard_removable(workspace, name, current_prefix="")
    with pytest.raises(provisioning.ProvisionError):
        provisioning.forget(workspace, name)
    with pytest.raises(provisioning.ProvisionError):
        provisioning.progress(workspace, name)


def test_forget_takes_the_entry_off_the_roster(workspace: Path) -> None:
    save_instance(workspace, instance_id="koki", label="小号", idx=1)
    provisioning.forget(workspace, "koki")
    assert load_instances(workspace) == []


# ── 进度 ──

def test_progress_reads_success_marker(workspace: Path) -> None:
    provisioning.log_path(workspace, "koki").parent.mkdir(parents=True, exist_ok=True)
    provisioning.log_path(workspace, "koki").write_text("拉起容器\nDONE ok\n", encoding="utf-8")
    state = provisioning.progress(workspace, "koki")
    assert state["id"] == "koki"
    assert state["finished"] and state["ok"]


def test_progress_surfaces_the_failure_reason(workspace: Path) -> None:
    provisioning.log_path(workspace, "koki").parent.mkdir(parents=True, exist_ok=True)
    provisioning.log_path(workspace, "koki").write_text(
        "拉起容器\nDONE fail: 容器 90 秒没写出 webui.json\n", encoding="utf-8",
    )
    state = provisioning.progress(workspace, "koki")
    assert state["finished"] and not state["ok"]
    assert "webui.json" in state["detail"]


def test_progress_on_a_missing_log_is_not_finished(workspace: Path) -> None:
    state = provisioning.progress(workspace, "koki")
    assert state["lines"] == [] and not state["finished"]


def test_the_progress_log_sits_outside_the_workspace(workspace: Path) -> None:
    # 删实例会把工作区改名，日志跟着搬走就再也读不到结果。
    log = provisioning.log_path(workspace, "koki")
    assert log == provisioning.data_root(workspace) / "provision-koki.log"
    assert not log.is_relative_to(provisioning.data_root(workspace) / "koki")


# ── systemd 单元名 ──

def test_the_unit_templates_carry_the_instance_name() -> None:
    assert provisioning.CREATE_UNIT.format(instance_id="koki") == "kazebot-provision@koki.service"
    assert provisioning.REMOVE_UNIT.format(instance_id="koki") == "kazebot-deprovision@koki.service"


def test_a_unit_is_started_without_waiting_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    async def accept(*args: str) -> tuple[int, str]:
        seen.append(args)
        return 0, ""

    monkeypatch.setattr(provisioning, "_systemctl", accept)
    asyncio.run(provisioning.start_unit("kazebot-provision@koki.service"))
    # oneshot 单元会跑上一两分钟，不能把请求挂在这里。
    assert seen == [("start", "--no-block", "kazebot-provision@koki.service")]


def test_a_unit_that_will_not_start_names_the_usual_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    # polkit 规则和单元都装在 /usr/local/lib 与 /etc 下，git 更新不会同步过去。
    async def refuse(*_args: str) -> tuple[int, str]:
        return 1, "Access denied"

    monkeypatch.setattr(provisioning, "_systemctl", refuse)
    with pytest.raises(provisioning.ProvisionError, match="polkit 规则和 root 侧脚本是最新的吗"):
        asyncio.run(provisioning.start_unit("kazebot-provision@koki.service"))


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


def test_root_script_validates_the_instance_name_before_anything_else() -> None:
    lines = [l.strip() for l in _script_code(SCRIPT).splitlines() if l.strip()]
    guard = next(i for i, l in enumerate(lines) if "^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$" in l)
    reserved = next(i for i, l in enumerate(lines) if "!= stickers" in l)
    for risky in ("docker ", "systemctl ", "mv ", "install "):
        hits = [i for i, l in enumerate(lines) if l.startswith(risky)]
        assert all(i > guard for i in hits), f"{risky} 出现在实例名校验之前"
        assert all(i > reserved for i in hits), f"{risky} 出现在保留名校验之前"


def test_the_root_script_names_the_instance_when_it_edits_cloudflared() -> None:
    # ingress 的路径规则按实例名拼，传成别的东西就把另一台的流量改没了。
    body = _script_code(SCRIPT)
    for action in ("add", "remove"):
        assert f'{action} --config "$CF_CONF" --instance "$INSTANCE"' in body


def test_the_command_line_entry_points_validate_the_same_way() -> None:
    for path in (NEW_INSTANCE, INSTALLER):
        body = _script_code(path)
        assert "^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$" in body
        # 实例名是纯数字（老习惯按 QQ 号开）又没另给号时，它就是这个实例的 QQ 号。
        assert '=~ ^[0-9]+$' in body


@pytest.mark.parametrize("path", [NEW_INSTANCE, INSTALLER], ids=lambda p: p.name)
def test_the_command_line_entry_points_refuse_the_shared_sticker_name(path: Path) -> None:
    # 叫 stickers 的话工作区就是共享表情包库那个目录，建下去等于往库里灌实例数据。
    lines = [l.strip() for l in _script_code(path).splitlines() if l.strip()]
    root_check = next(i for i, l in enumerate(lines) if '"$(id -u)" -eq 0' in l)
    shape = next(i for i, l in enumerate(lines) if "^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$" in l)
    reserved = next(i for i, l in enumerate(lines) if "!= stickers" in l)
    first_action = next(i for i, l in enumerate(lines) if l.startswith('echo "==> '))
    assert root_check < shape < reserved < first_action
    assert "实例名不能叫 stickers" in lines[reserved]


@pytest.mark.parametrize("path", [NEW_INSTANCE, INSTALLER], ids=lambda p: p.name)
def test_the_command_line_entry_points_refuse_a_qq_number_that_is_not_digits(path: Path) -> None:
    # 非数字的号会一路走到 normalize_accounts 才炸，那时候工作区和单元已经落下去了。
    lines = [l.strip() for l in _script_code(path).splitlines() if l.strip()]
    inferred = next(i for i, l in enumerate(lines) if l.startswith("if [[ -z"))
    guard = next(i for i, l in enumerate(lines) if "QQ 号必须是数字" in l)
    first_action = next(i for i, l in enumerate(lines) if l.startswith('echo "==> '))
    # 实例名是纯数字时那个号是推出来的，推完才轮到校验。
    assert inferred < guard < first_action


_ZH_USAGE = "install_provision.sh <主实例名> <主实例QQ号>"
_ZH_STALE = "<第一个号的QQ号>"


@pytest.mark.parametrize(("path", "usage", "stale"), [
    pytest.param(INSTALL_DOC, _ZH_USAGE, _ZH_STALE, id="INSTALL.md"),
    pytest.param(BOOTSTRAP, _ZH_USAGE, _ZH_STALE, id="bootstrap.sh"),
    pytest.param(README, _ZH_USAGE, _ZH_STALE, id="README.md"),
    pytest.param(
        README_EN,
        "install_provision.sh <main instance name> <main instance QQ number>",
        "<first account's QQ number>",
        id="README.en.md",
    ),
])
def test_the_multi_instance_hint_asks_for_the_instance_name_first(
    path: Path, usage: str, stale: str,
) -> None:
    # 照着只给一个 QQ 号的写法跑，那串数字会被当成实例名，主实例就叫那个号了。
    body = path.read_text(encoding="utf-8")

    assert usage in body
    assert stale not in body


@pytest.mark.parametrize(("path", "workspace", "stale"), [
    pytest.param(README, "<实例名>/", "<QQ号>/", id="README.md"),
    pytest.param(README_EN, "<instance>/", "<uin>/", id="README.en.md"),
])
def test_the_layout_names_the_extra_workspaces_by_instance(
    path: Path, workspace: str, stale: str,
) -> None:
    # 工作区目录按实例名建，示意图画成 QQ 号的话，多开之后照着找目录只会扑空。
    body = path.read_text(encoding="utf-8")

    assert workspace in body
    assert stale not in body


def test_the_installer_registers_the_main_instance_through_the_shared_validators() -> None:
    # 自己再写一遍校验就会和 supervisor 漂移，宽的那一份说了算。
    code = _heredoc(INSTALLER, "PY", "save_instance")
    assert "from supervisor.provisioning import normalize_accounts, validate_instance_id" in code
    assert "validate_instance_id(sys.argv[2])" in code
    assert "normalize_accounts(" in code


def _install_main(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """把安装脚本里登记主实例的那段 Python 在临时清单上真跑一遍。"""
    env = {
        **os.environ,
        "CLONOTH_INSTANCES_FILE": str(tmp_path / "instances.yaml"),
        "PYTHONIOENCODING": "utf-8",
    }
    env.pop("CLONOTH_URL_PREFIX", None)
    return subprocess.run(
        [sys.executable, "-c", _heredoc(INSTALLER, "PY", "save_instance"), str(REPO_ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )


def test_the_installer_puts_the_main_instance_on_the_roster_at_index_zero(tmp_path: Path) -> None:
    done = _install_main(tmp_path, "rong", "1000000001")
    assert done.returncode == 0, done.stderr
    rows = yaml.safe_load((tmp_path / "instances.yaml").read_text(encoding="utf-8"))["instances"]
    assert rows == [
        {"id": "rong", "label": "主实例 rong", "path": "", "idx": 0, "accounts": {"qq": "1000000001"}},
    ]


def test_the_installer_leaves_the_accounts_empty_when_no_number_was_given(tmp_path: Path) -> None:
    done = _install_main(tmp_path, "rong", "")
    assert done.returncode == 0, done.stderr
    rows = yaml.safe_load((tmp_path / "instances.yaml").read_text(encoding="utf-8"))["instances"]
    assert rows[0]["accounts"] == {}


@pytest.mark.parametrize("main,qq", [
    ("stickers", ""), ("Rong", ""), ("r", ""), ("../rong", ""), ("", ""),
    ("rong", "abc"), ("rong", "1000000001; id"),
])
def test_the_installer_refuses_a_bad_name_or_number_without_touching_the_roster(
    tmp_path: Path, main: str, qq: str,
) -> None:
    done = _install_main(tmp_path, main, qq)
    assert done.returncode != 0
    assert "ProvisionError" in done.stderr
    assert not (tmp_path / "instances.yaml").exists()


def test_the_roster_edit_matches_new_and_old_entries_alike(tmp_path: Path) -> None:
    # root 侧摘清单的那段跟 supervisor 用同一份文件，认不出老条目就会留下幽灵实例。
    roster = tmp_path / "instances.yaml"
    roster.write_text(
        "instances:\n"
        "  - {id: rong, label: 主号, path: '', idx: 0}\n"
        "  - {uin: '1000000002', label: 小号, path: /i/1000000002, idx: 1}\n"
        "  - {id: koki, label: koki, path: /i/koki, idx: 2}\n",
        encoding="utf-8",
    )
    code = _heredoc(SCRIPT, "PY", "kept")

    for target, left in (("1000000002", ["rong", "koki"]), ("koki", ["rong"])):
        done = subprocess.run(
            [sys.executable, "-c", code, str(roster), target],
            capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, done.stderr
        rows = yaml.safe_load(roster.read_text(encoding="utf-8"))["instances"]
        assert [str(r.get("id") or r.get("uin")) for r in rows] == left


def _strike(tmp_path: Path, entry: str, target: str) -> list[str]:
    """root 侧摘清单的那段，在一份两条的清单上真跑一遍，返回剩下的实例名。"""
    roster = tmp_path / "instances.yaml"
    roster.write_text(
        f"instances:\n  - {{id: rong, label: 主号, path: '', idx: 0}}\n  - {entry}\n", encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, "-c", _heredoc(SCRIPT, "PY", "kept"), str(roster), target],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert done.returncode == 0, done.stderr
    rows = yaml.safe_load(roster.read_text(encoding="utf-8"))["instances"]
    return [str(row.get("id") or row.get("uin")).strip() for row in rows]


@pytest.mark.parametrize("entry,target", [
    ("{id: '  koki  ', label: 小号, path: /i/koki, idx: 2}", "koki"),
    ("{id: '   ', uin: '1000000002', label: 小号, path: /i/1000000002, idx: 2}", "1000000002"),
    ("{id: 1000000002, label: 小号, path: /i/1000000002, idx: 2}", "1000000002"),
    ("{uin: '1000000002', label: 小号, path: /i/1000000002, idx: 2}", "1000000002"),
])
def test_the_roster_edit_recognises_an_entry_the_way_supervisor_does(
    tmp_path: Path, entry: str, target: str,
) -> None:
    # 认法比 supervisor 窄一格就摘不掉，清单里留下一个占着序号和端口的幽灵实例。
    assert _strike(tmp_path, entry, target) == ["rong"]


@pytest.mark.parametrize("target", ["nobody", "1000000009", "", "ron"])
def test_the_roster_edit_keeps_everyone_when_nothing_matches(tmp_path: Path, target: str) -> None:
    entry = "{id: koki, label: 小号, path: /i/koki, idx: 2}"
    assert _strike(tmp_path, entry, target) == ["rong", "koki"]


def test_the_roster_edit_survives_a_missing_file(tmp_path: Path) -> None:
    code = _heredoc(SCRIPT, "PY", "kept")
    done = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "nothing.yaml"), "koki"],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "nothing.yaml").exists()


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
