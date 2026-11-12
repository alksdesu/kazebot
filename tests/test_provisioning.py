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

import config_layout
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
WRITABLE_SCRIPT = REPO_ROOT / "deploy" / "enable_instance_writable.sh"
WRITABLE_UNIT = REPO_ROOT / "deploy" / "systemd" / "kazebot-writable@.service"
UNIT_DIR = REPO_ROOT / "deploy" / "systemd"
INSTALLER = REPO_ROOT / "deploy" / "install_provision.sh"
NEW_INSTANCE = REPO_ROOT / "deploy" / "new_instance.sh"
CLOUDFLARED = REPO_ROOT / "deploy" / "cloudflared_ingress.py"
POLKIT = REPO_ROOT / "deploy" / "polkit" / "49-kazebot-provision.rules"
INSTALL_DOC = REPO_ROOT / "deploy" / "INSTALL.md"
MULTI_ACCOUNT_DOC = REPO_ROOT / "deploy" / "MULTI-ACCOUNT.md"
BOOTSTRAP = REPO_ROOT / "deploy" / "bootstrap.sh"
DEPLOY = REPO_ROOT / "deploy" / "deploy.sh"
IMAGE_DIR = REPO_ROOT / "deploy" / "napcat-image"
IMAGE_ENV = IMAGE_DIR / "image.env"
BUILD_IMAGE = IMAGE_DIR / "build.sh"
ONEBOT_README = REPO_ROOT / "adapters" / "onebot" / "README.md"
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


def _qq() -> PlatformDescriptor:
    return provisioning.resolve_platform("qq")


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


# ── 新实例的平台与账号 ──

@pytest.mark.parametrize("raw", [None, "", "   ", "qq", "  QQ  "])
def test_an_unstated_platform_is_qq(raw: object) -> None:
    # 改造前建出来的只有 QQ 实例，没写就跟它们一样。
    assert provisioning.resolve_platform(raw).id == "qq"


@pytest.mark.parametrize("name", ["onebot", "onebot11"])
def test_a_historical_alias_still_names_the_platform(name: str) -> None:
    assert provisioning.resolve_platform(name).id == "qq"


@pytest.mark.parametrize("name", ["nowhere", "qq_group", "qq_private", "别的"])
def test_an_unknown_platform_name_is_refused(name: str) -> None:
    # 会话通道名不是平台名：认宽了，清单里的 platform 和运行期认的那个会对不上。
    with pytest.raises(provisioning.ProvisionError, match="不认识的平台"):
        provisioning.resolve_platform(name)


def test_a_blank_account_is_an_instance_nobody_has_logged_in_yet() -> None:
    # 前端的表单里留空就是「先不登，扫完码再记」。
    qq = provisioning.resolve_platform("qq")
    assert provisioning.normalize_account(qq, None) == ""
    assert provisioning.normalize_account(qq, "  ") == ""


def test_an_account_is_normalised_to_the_platform_id_shape() -> None:
    assert provisioning.normalize_account(provisioning.resolve_platform("qq"), "  1000000002 ") == "1000000002"


@pytest.mark.parametrize("account", ["abc", "10000 00002", "1000000002; id", "١٢٣٤٥"])
def test_an_account_that_does_not_fit_the_platform_is_refused(account: str) -> None:
    with pytest.raises(provisioning.ProvisionError, match="账号写法不对"):
        provisioning.normalize_account(provisioning.resolve_platform("qq"), account)


@pytest.mark.parametrize("body,expected", [
    ({"platform": "qq", "account": " 1000000002 "}, ("qq", "1000000002")),
    ({"platform": "onebot", "account": "1000000002"}, ("qq", "1000000002")),
    ({"platform": "qq"}, ("qq", "")),
    ({"account": "1000000002"}, ("qq", "1000000002")),
    ({"platform": "", "account": ""}, ("qq", "")),
    ({}, ("qq", "")),
])
def test_the_request_says_which_platform_and_which_account(
    body: dict[str, Any], expected: tuple[str, str],
) -> None:
    platform, account = provisioning.platform_account_from(body)
    assert (platform.id, account) == expected


@pytest.mark.parametrize("legacy,expected", [
    ({"qq": " 1000000002 "}, ("qq", "1000000002")),
    ({"  QQ  ": "1000000002"}, ("qq", "1000000002")),
    ({"qq": "1000000002", "telegram": ""}, ("qq", "1000000002")),
    ({"qq": ""}, ("qq", "")),
    ({}, ("qq", "")),
])
def test_an_old_console_still_gets_its_one_platform_through(
    legacy: dict[str, str], expected: tuple[str, str],
) -> None:
    # 没刷新页面的控制台还发着「平台 id: 账号」的映射，只填了一项时照认。
    platform, account = provisioning.platform_account_from({"accounts": legacy})
    assert (platform.id, account) == expected


def test_an_old_console_asking_for_two_platforms_at_once_is_refused() -> None:
    # 替它挑一个就是猜，猜错开出来的实例跑的是另一个平台。
    with pytest.raises(provisioning.ProvisionError, match="一个实例只跑一个平台"):
        provisioning.platform_account_from({"accounts": {"qq": "1000000002", "telegram": "@yu"}})


def test_the_new_fields_win_over_an_old_accounts_mapping() -> None:
    body = {"platform": "qq", "account": "1000000002", "accounts": {"telegram": "@yu"}}
    platform, account = provisioning.platform_account_from(body)
    assert (platform.id, account) == ("qq", "1000000002")


def test_accounts_must_be_a_mapping_of_platform_to_account() -> None:
    with pytest.raises(provisioning.ProvisionError, match="平台 id"):
        provisioning.platform_account_from({"accounts": [("qq", "1000000002")]})


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
    assert (rows[1]["platform"], rows[1]["account"]) == ("qq", "1000000002")


# ── 计划与脚手架 ──

def test_plan_refuses_a_name_already_on_the_roster(workspace: Path) -> None:
    save_instance(workspace, instance_id="rong", label="主号", idx=0)
    with pytest.raises(provisioning.ProvisionError, match="已经在清单里"):
        provisioning.plan_instance(workspace, instance_id="rong", label="")


def test_plan_refuses_an_account_another_instance_already_holds(workspace: Path) -> None:
    # 同一个号两边登会互相踢下线，数据也会分成两份。
    save_instance(workspace, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")
    with pytest.raises(provisioning.ProvisionError, match="已经归实例 主号 管了"):
        provisioning.plan_instance(
            workspace, instance_id="koki", label="", platform=_qq(), account="1000000001",
        )


@pytest.mark.parametrize("body", [
    {"platform": "qq", "account": " 1000000001 "},
    {"platform": "  QQ  ", "account": "1000000001"},
    {"platform": "onebot", "account": "\t1000000001 "},
    {"accounts": {"  QQ  ": " 1000000001 "}},
])
def test_plan_refuses_a_held_account_however_it_was_typed(workspace: Path, body: dict[str, Any]) -> None:
    # 请求体里的写法拦不住冲突检查：解析请求和清单查询走的是同一套清洗。
    save_instance(workspace, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")
    platform, account = provisioning.platform_account_from(body)
    with pytest.raises(provisioning.ProvisionError, match="已经归实例 主号 管了"):
        provisioning.plan_instance(
            workspace, instance_id="koki", label="", platform=platform, account=account,
        )


def test_plan_counts_the_instances_hanging_under_a_prefix_too(workspace: Path) -> None:
    # 新实例还没有自己的前缀，谁都不该被当成「那就是我」放过去。
    save_instance(workspace, instance_id="koki", label="小号", idx=1, platform="qq", account="1000000002")
    with pytest.raises(provisioning.ProvisionError, match="已经归实例 小号 管了"):
        provisioning.plan_instance(
            workspace, instance_id="yu", label="", platform=_qq(), account="1000000002",
        )


def test_plan_only_checks_the_platform_it_is_opening_on(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 别的平台上一个长得一样的号不是冲突，拦下来就没法在两个平台上用同一串数字开实例。
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 2)
    save_instance(workspace, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")
    with _extra_platform("elsewhere") as other:
        plan = provisioning.plan_instance(
            workspace, instance_id="koki", label="", platform=other, account="1000000001",
        )
    assert (plan.platform, plan.account) == ("elsewhere", "1000000001")


def test_plan_lets_through_an_account_nobody_holds(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 2)
    save_instance(workspace, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")
    plan = provisioning.plan_instance(
        workspace, instance_id="koki", label="", platform=_qq(), account="1000000002",
    )
    assert (plan.platform, plan.account) == ("qq", "1000000002")


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


def test_a_plan_carries_the_prefix_ports_and_account(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    plan = provisioning.plan_instance(
        workspace, instance_id="koki", label=" 小号 ", platform=_qq(), account=" 1000000002 ",
    )
    assert plan.idx == 1 and plan.label == "小号"
    assert plan.prefix == "/i/koki"
    assert plan.ports == ports_for(1)
    assert (plan.platform, plan.account) == ("qq", "1000000002")
    assert plan.workspace == provisioning.data_root(workspace) / "koki"


def test_a_plan_without_a_platform_opens_a_qq_instance(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    plan = provisioning.plan_instance(workspace, instance_id="koki", label="")
    assert (plan.platform, plan.account) == ("qq", "")


def test_a_nameless_plan_is_labelled_with_its_own_name(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    assert provisioning.plan_instance(workspace, instance_id="koki", label="").label == "koki"


@needs_symlink
def test_scaffold_links_code_dirs_and_writes_env(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    plan = provisioning.plan_instance(
        workspace, instance_id="koki", label="小号", platform=_qq(), account="1000000002",
    )
    provisioning.scaffold(plan, workspace)

    for name in config_layout.CODE_LINKS:
        assert (plan.workspace / name).is_symlink()
    env = (plan.workspace / ".env").read_text(encoding="utf-8")
    assert "CLONOTH_PORT=8775" in env
    assert "CLONOTH_URL_PREFIX=i/koki" in env
    rows = load_instances(workspace)
    assert [r["id"] for r in rows] == ["koki"]
    assert (rows[0]["platform"], rows[0]["account"]) == ("qq", "1000000002")


@pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
def test_scaffold_gives_config_and_data_owner_only_directories(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # config/ 有密钥、data/ 有令牌和会话，和 bootstrap.sh 的 0700 对齐，挡住同机其他用户
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    plan = provisioning.plan_instance(workspace, instance_id="koki", label="")
    provisioning.scaffold(plan, workspace)

    assert (plan.workspace / "config").stat().st_mode & 0o777 == 0o700
    assert (plan.workspace / "data").stat().st_mode & 0o777 == 0o700


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


def test_the_env_opens_with_the_instance_its_platform_and_its_supervisor_port(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 运行期处处认这一个值，漏了这行整个实例会退回默认的 QQ。
    lines = provisioning._env_body(_plan(workspace, monkeypatch, 1)).splitlines()
    assert lines[0] == "# 实例 koki (koki)"
    assert lines[1] == "CLONOTH_PLATFORM=qq"
    assert lines[2] == "CLONOTH_PORT=8775"
    assert lines[3] == "CLONOTH_BASE_URL=http://127.0.0.1:8775/i/koki"


def test_the_env_declares_the_platform_the_instance_was_opened_on(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    with _extra_platform("elsewhere") as other:
        plan = provisioning.plan_instance(workspace, instance_id="koki", label="", platform=other)
        assert "CLONOTH_PLATFORM=elsewhere" in provisioning._env_body(plan)


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
    body = provisioning._env_body(_plan(workspace, monkeypatch, 0, "rong"))
    assert "CLONOTH_BASE_URL=http://127.0.0.1:8765" in body
    assert "CLONOTH_URL_PREFIX" not in body


def test_a_platform_without_the_hook_writes_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    with _extra_platform("silent", instance_ports=(InstancePort("hush", 9300, 10),)) as silent:
        plan = provisioning.plan_instance(workspace, instance_id="koki", label="", platform=silent)
        body = provisioning._env_body(plan)
    assert "9310" not in body
    assert "hush" not in body


def test_another_platform_lines_do_not_land_in_this_instance_env(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 别的平台的端口和令牌写进来，只会让人以为这个实例也在跑它。
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    with _extra_platform("elsewhere") as other:
        body = provisioning._env_body(
            provisioning.plan_instance(workspace, instance_id="koki", label="", platform=other),
        )
    assert "ONEBOT_FORWARD_BRIDGE_PORT" not in body
    assert "NAPCAT_WEBUI_URL" not in body


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
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    with _extra_platform(
        "noisy",
        instance_ports=(InstancePort("noisy", 9300, 10),),
        hooks={HOOK_INSTANCE_ENV: "noisy.provisioning"},
    ) as noisy:
        plan = provisioning.plan_instance(workspace, instance_id="koki", label="", platform=noisy)
        body = provisioning._env_body(plan)

    assert seen == [{"noisy": 9310}]
    assert "NOISY_INDEX=1" in body and "NOISY_PORT=9310" in body


def test_a_hook_module_that_breaks_on_import_is_not_a_missing_hook(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跳过等于悄悄开出一个缺了这个平台配置的实例，起来才发现连不上。"""
    def lookup(platform: PlatformDescriptor, name: str) -> Any:
        raise KeyError("napcat ports")

    plan = _plan(workspace, monkeypatch, 1)
    monkeypatch.setattr(provisioning, "platform_hook", lookup)

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
    save_instance(workspace, instance_id="koki", label="小号", idx=1, platform="qq", account="1000000002")
    row = provisioning.guard_removable(workspace, "koki", current_prefix="")
    assert row["id"] == "koki" and (row["platform"], row["account"]) == ("qq", "1000000002")


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


def test_active_progress_exposes_a_resumable_state(workspace: Path) -> None:
    provisioning.log_path(workspace, "koki").parent.mkdir(parents=True, exist_ok=True)
    provisioning.log_path(workspace, "koki").write_text("等 supervisor 应答\n", encoding="utf-8")

    state = provisioning.progress(workspace, "koki")

    assert state["state"] == "running" and state["retryable"] is True and state["action"] == "create"


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


def test_a_host_without_systemctl_is_a_refusal_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    # 找不到 systemctl 时 create_subprocess_exec 直接抛 OSError，穿出去页面上只剩一句「服务器错误」
    async def missing(*_args: str) -> tuple[int, str]:
        raise FileNotFoundError(2, "No such file or directory", "systemctl")

    monkeypatch.setattr(provisioning, "_systemctl", missing)
    with pytest.raises(provisioning.ProvisionError, match="调不起 systemctl"):
        asyncio.run(provisioning.start_unit("kazebot-provision@koki.service"))


def test_a_line_handed_to_root_never_glues_onto_a_half_written_one(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 上一次停在半行时接着写，「DONE fail: 中断」和这一行粘成一行，读出来还是上一次的失败
    log = provisioning.log_path(workspace, "koki")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes("[10:00:00] 停服务\nDONE fail: 中断".encode("utf-8"))

    async def accept(_unit: str) -> None:
        return None

    monkeypatch.setattr(provisioning, "start_unit", accept)
    asyncio.run(provisioning.hand_to_root(
        workspace, "koki", "kazebot-deprovision@koki.service", opening=provisioning.REMOVAL_REQUESTED,
    ))

    state = provisioning.progress(workspace, "koki")
    assert state["finished"] is False
    assert state["lines"][-2:] == ["DONE fail: 中断", provisioning.REMOVAL_REQUESTED]


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
    reserved = next(i for i, l in enumerate(lines) if l.startswith("case "))
    for risky in ("docker ", "systemctl ", "mv ", "install "):
        hits = [i for i, l in enumerate(lines) if l.startswith(risky)]
        assert all(i > guard for i in hits), f"{risky} 出现在实例名校验之前"
        assert all(i > reserved for i in hits), f"{risky} 出现在保留名校验之前"


def test_the_root_script_names_the_instance_when_it_edits_cloudflared() -> None:
    # ingress 的路径规则按实例名拼，传成别的东西就把另一台的流量改没了。
    body = _script_code(SCRIPT)
    for action in ("add", "remove"):
        assert f'{action} --config "$CF_CONF" --instance "$INSTANCE"' in body


def test_the_root_script_only_builds_the_platform_the_env_declares() -> None:
    # root 侧只搭得起 QQ 那一套（NapCat 容器 + kazebot-qq@），别的平台装半套比不装更难查。
    body = _script_code(SCRIPT)
    assert "CLONOTH_PLATFORM" in body
    assert '[ "$platform" = qq ]' in body


def test_the_root_script_checks_the_prefixed_health_and_logs_the_admin_token() -> None:
    body = _script_code(SCRIPT)

    assert '127.0.0.1:$sup_port/i/$INSTANCE/v1/health' in body
    assert "--noproxy '*'" in body and 'waited" -ge 300' in body
    assert 'Supervisor admin token:' in body
    assert '$WS/data/.admin_token' in body


def test_the_bootstrap_env_declares_the_platform() -> None:
    # 单机装出来的实例也要写这一行，运行期处处认它。
    assert "CLONOTH_PLATFORM=qq" in BOOTSTRAP.read_text(encoding="utf-8")


def test_the_command_line_entry_point_opens_a_qq_instance() -> None:
    code = _heredoc(NEW_INSTANCE, "PY", "plan_instance")
    assert "platform=QQ_NAPCAT" in code
    assert "accounts=" not in code


def test_the_command_line_entry_points_validate_the_same_way() -> None:
    for path in (NEW_INSTANCE, INSTALLER):
        body = _script_code(path)
        assert "^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$" in body
        # 实例名是纯数字（老习惯按 QQ 号开）又没另给号时，它就是这个实例的 QQ 号。
        assert '=~ ^[0-9]+$' in body


@pytest.mark.parametrize("path", [NEW_INSTANCE, INSTALLER], ids=lambda p: p.name)
def test_the_command_line_entry_points_refuse_the_names_the_data_root_took(path: Path) -> None:
    # 叫 stickers 或 shared 的话工作区就是共享表情包库、共享配置组那个目录，建下去等于往里灌实例数据。
    lines = [l.strip() for l in _script_code(path).splitlines() if l.strip()]
    root_check = next(i for i, l in enumerate(lines) if '"$(id -u)" -eq 0' in l)
    shape = next(i for i, l in enumerate(lines) if "^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$" in l)
    reserved = next(i for i, l in enumerate(lines) if l.startswith("case "))
    first_action = next(i for i, l in enumerate(lines) if l.startswith('echo "==> '))
    assert root_check < shape < reserved < first_action
    assert "stickers|shared)" in lines[reserved]
    assert "实例名不能叫" in lines[reserved]


@pytest.mark.parametrize("path", [NEW_INSTANCE, INSTALLER], ids=lambda p: p.name)
def test_the_command_line_entry_points_refuse_a_qq_number_that_is_not_digits(path: Path) -> None:
    # 非数字的号会一路走到 normalize_account 才炸，那时候工作区和单元已经落下去了。
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
    pytest.param(MULTI_ACCOUNT_DOC, _ZH_USAGE, _ZH_STALE, id="MULTI-ACCOUNT.md"),
])
def test_the_multi_instance_hint_asks_for_the_instance_name_first(
    path: Path, usage: str, stale: str,
) -> None:
    # 照着只给一个 QQ 号的写法跑，那串数字会被当成实例名，主实例就叫那个号了。
    body = path.read_text(encoding="utf-8")

    assert usage in body
    assert stale not in body


def test_the_layout_names_the_extra_workspaces_by_instance() -> None:
    # 工作区目录按实例名建，示意图画成 QQ 号的话，多开之后照着找目录只会扑空。
    body = MULTI_ACCOUNT_DOC.read_text(encoding="utf-8")

    assert "<实例名>/" in body
    assert "<QQ号>/" not in body


@pytest.mark.parametrize(("path", "stale"), [
    pytest.param(README, (_ZH_STALE, "<QQ号>/"), id="README.md"),
    pytest.param(README_EN, ("<first account's QQ number>", "<uin>/"), id="README.en.md"),
])
def test_the_readmes_carry_no_stale_multi_instance_hint(path: Path, stale: tuple[str, ...]) -> None:
    # README 不讲多开的细节，交给 deploy/ 下的两份文档；留着旧写法的话，照抄的人会把 QQ 号当成实例名。
    body = path.read_text(encoding="utf-8")

    assert not [text for text in stale if text in body]


def test_the_installer_registers_the_main_instance_through_the_shared_validators() -> None:
    # 自己再写一遍校验就会和 supervisor 漂移，宽的那一份说了算。
    code = _heredoc(INSTALLER, "PY", "save_instance")
    assert "from supervisor.provisioning import normalize_account, validate_instance_id" in code
    assert "validate_instance_id(sys.argv[2])" in code
    assert "normalize_account(" in code
    assert "platform=QQ_NAPCAT.id" in code


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
    assert rows == [{
        "id": "rong", "label": "主实例 rong", "path": "", "idx": 0,
        "platform": "qq", "account": "1000000001", "uin": "1000000001",
    }]


def test_the_installer_leaves_the_account_empty_when_no_number_was_given(tmp_path: Path) -> None:
    done = _install_main(tmp_path, "rong", "")
    assert done.returncode == 0, done.stderr
    rows = yaml.safe_load((tmp_path / "instances.yaml").read_text(encoding="utf-8"))["instances"]
    assert rows[0]["platform"] == "qq" and rows[0]["account"] == ""


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
    assert '"$REPO/deploy/enable_instance_writable.sh" "$LIB/enable_instance_writable.sh"' in installer
    assert 'kazebot-writable@.service' in installer
    script = _script_code(SCRIPT)
    assert "UNITS=/usr/local/lib/kazebot/units" in script
    # 从 /opt/kazebot/deploy 直接装单元，agent 改一行就能让 root 跑它的东西。
    assert "$REPO/deploy/systemd" not in script


def test_polkit_rule_only_grants_start_on_the_fixed_templates() -> None:
    body = POLKIT.read_text(encoding="utf-8")
    assert 'subject.user !== "kazebot"' in body
    assert 'action.lookup("verb") === "start"' in body
    assert "kazebot-(provision|deprovision|writable)@" in body
    # 授权只能是那一个 action id，多一个就是多一条提权路径。
    assert body.count("polkit.Result.YES") == 1


def test_code_write_helper_has_a_fixed_dropin_and_restart_target() -> None:
    body = _script_code(WRITABLE_SCRIPT)
    assert 'DROPIN_DIR="/etc/systemd/system/${UNIT}.d"' in body
    assert 'DROPIN="$DROPIN_DIR/50-writable.conf"' in body
    assert "ReadWritePaths=/opt/kazebot" in body
    assert "NoNewPrivileges=no" in body
    assert "RestrictSUIDSGID=no" in body
    assert 'systemctl restart "$UNIT"' in body
    assert "/opt/kazebot/" not in body.replace("/opt/kazebot\n", "")


def test_code_write_unit_runs_only_the_root_owned_helper() -> None:
    body = _script_code(WRITABLE_UNIT)
    assert "Type=oneshot" in body
    assert "ExecStart=/usr/local/lib/kazebot/enable_instance_writable.sh %i" in body
    assert "/opt/kazebot/deploy" not in body


@pytest.mark.parametrize("unit, argument", [("napcat-account.service", ""), ("napcat-account@.service", " %i")])
def test_account_action_units_run_only_the_root_owned_runner(unit: str, argument: str) -> None:
    lines = _script_code(UNIT_DIR / unit).splitlines()
    assert "Type=oneshot" in lines
    # 系统解释器加 -I：工作区里的解释器和模块都归 kazebot，root 不能碰。
    assert f"ExecStart=/usr/bin/python3 -I /usr/local/lib/kazebot/napcat_account_runner.py{argument}" in lines
    assert not any("/opt/kazebot" in line for line in lines)


def test_each_instance_watches_its_own_account_request() -> None:
    main = (UNIT_DIR / "napcat-account.path").read_text(encoding="utf-8").splitlines()
    assert "PathExists=/opt/kazebot/data/napcat-account.request" in main
    assert "Unit=napcat-account.service" in main
    template = (UNIT_DIR / "napcat-account@.path").read_text(encoding="utf-8").splitlines()
    # 多开实例的请求落在自己的工作区；盯着主实例那个路径的话，按钮只会等到超时。
    assert "PathExists=/opt/kazebot-data/%i/data/napcat-account.request" in template
    assert "Unit=napcat-account@%i.service" in template


def test_the_account_trigger_follows_an_instance_through_its_life() -> None:
    script = _script_code(SCRIPT)
    assert '"$UNITS"/napcat-account@.path "$UNITS"/napcat-account@.service /etc/systemd/system/' in script
    create, remove = script.split("do_remove() {", 1)
    assert '"napcat-account@$INSTANCE.path"' in create.split("systemctl enable --now", 1)[1].split("\n\n", 1)[0]
    assert '"napcat-account@$INSTANCE.path"' in remove.split("systemctl disable --now", 1)[1].split("\n\n", 1)[0]


@pytest.mark.parametrize("script", [INSTALLER, BOOTSTRAP], ids=["install_provision.sh", "bootstrap.sh"])
def test_the_account_runner_is_installed_where_the_agent_cannot_write(script: Path) -> None:
    body = _script_code(script)
    assert '"$REPO/deploy/napcat_account_runner.py" "$LIB/napcat_account_runner.py"' in body
    assert "systemctl enable --now napcat-account.path" in body


def test_instances_made_before_the_account_trigger_get_it_on_reinstall() -> None:
    body = _script_code(INSTALLER)
    assert "/etc/systemd/system/multi-user.target.wants/kazebot@*.service" in body
    assert 'systemctl enable --now "napcat-account@$existing.path"' in body
    # 清单归 kazebot 写，照它启用单元等于让 agent 指定 root 盯哪个目录。
    assert "instances.yaml" not in body.split("启用账号动作触发", 1)[1].split("把主实例登记进清单", 1)[0]


def _image_tag() -> str:
    line = next(l for l in IMAGE_ENV.read_text(encoding="utf-8").splitlines() if l.startswith("NAPCAT_IMAGE="))
    return line.split("=", 1)[1]


class TestSelfBuiltImage:
    """上游镜像仓库和 QQ 的 deb 下载都没了，镜像只能本机自建；起容器的几处要认同一个标签。"""

    def test_the_alias_is_declared_once_and_every_consumer_reads_it(self) -> None:
        # 别名不带版本：朋友拿官网当天的 QQ 构建出来也能直接被 bootstrap 认到，版本号在镜像自己的 tag 里。
        assert _image_tag() == "kazebot/napcat:current"
        bootstrap = _script_code(BOOTSTRAP)
        assert '. "$REPO/deploy/napcat-image/image.env"' in bootstrap
        assert 'IMAGE="${KAZEBOT_NAPCAT_IMAGE:-$NAPCAT_IMAGE}"' in bootstrap
        assert '. "$HERE/image.env"' in _script_code(BUILD_IMAGE)
        for path in (BOOTSTRAP, SCRIPT, INSTALLER, DEPLOY, BUILD_IMAGE, INSTALL_DOC, ONEBOT_README, README, README_EN):
            assert "mlikiowa" not in path.read_text(encoding="utf-8"), path.name

    def test_bootstrap_refuses_to_run_a_container_from_an_image_that_is_not_there(self) -> None:
        body = _script_code(BOOTSTRAP)
        assert 'docker image inspect "$IMAGE"' in body
        assert "deploy/napcat-image/build.sh" in body
        assert "docker pull" not in body

    def test_provisioning_follows_a_running_container_then_the_installed_tag(self) -> None:
        # root 跑的脚本只认 /usr/local/lib 里的副本；/opt/kazebot 归 kazebot，读那边等于让 agent 定镜像。
        body = _script_code(SCRIPT)
        assert "docker ps --filter 'name=^napcat' --format '{{.Image}}'" in body
        assert "/usr/local/lib/kazebot/image.env" in body
        assert "docker inspect napcat --format" not in body
        assert "/opt/kazebot/deploy/napcat-image" not in body
        for path in (INSTALLER, BOOTSTRAP):
            assert '"$REPO/deploy/napcat-image/image.env" "$LIB/image.env"' in _script_code(path), path.name
        assert "/usr/local/lib/kazebot/image.env" in _script_code(DEPLOY)

    def test_the_dockerfile_keeps_the_device_identity_and_the_postinst_steps(self) -> None:
        body = (IMAGE_DIR / "Dockerfile").read_text(encoding="utf-8")
        assert "ARG MACHINE_ID=" in body and '> /etc/machine-id' in body
        assert "chmod 4755 /opt/QQ/chrome-sandbox" in body
        assert "ln -sf /opt/QQ/qq /usr/bin/qq" in body
        assert 'VOLUME ["/app/napcat/config", "/app/.config/QQ"]' in body
        assert body.rstrip().endswith('ENTRYPOINT ["bash", "entrypoint.sh"]')
        assert "localhost" not in (IMAGE_DIR / "entrypoint.sh").read_text(encoding="utf-8").split("fake_file /etc/hosts", 1)[0]

    @pytest.fixture
    def qq_tree(self, tmp_path: Path) -> Path:
        app = tmp_path / "qq" / "opt" / "QQ" / "resources" / "app"
        app.mkdir(parents=True)
        (tmp_path / "qq" / "usr" / "share" / "applications").mkdir(parents=True)
        (tmp_path / "qq" / "usr" / "share" / "applications" / "qq.desktop").write_text("desktop", encoding="utf-8")
        binary = tmp_path / "qq" / "opt" / "QQ" / "qq"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        (tmp_path / "qq" / "opt" / "QQ" / "chrome-sandbox").write_bytes(b"sandbox")
        (tmp_path / "qq" / "opt" / "QQ" / "._junk").write_bytes(b"apple")
        (app / "wrapper.node").write_bytes(b"node")
        (app / "package.json").write_text('{\n  "name": "QQ",\n  "version": "3.2.34-53644",\n  "main": "./x.js"\n}\n', encoding="utf-8")
        return tmp_path / "qq"

    def _shell_zip(self, tmp_path: Path, *, with_core: bool = True) -> Path:
        import zipfile

        path = tmp_path / "NapCat.Shell.zip"
        with zipfile.ZipFile(path, "w") as zf:
            if with_core:
                zf.writestr("napcat.mjs", 'const v="4.18.42";console.log("NapCat.Core Version: "+v)')
            zf.writestr("config/napcat.json", "{}")
        return path

    def _build(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(BUILD_IMAGE), *args], capture_output=True, text=True, encoding="utf-8", timeout=120,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )

    def test_the_context_is_assembled_from_an_unpacked_tree(self, tmp_path: Path, qq_tree: Path) -> None:
        context = tmp_path / "ctx"
        done = self._build(
            "--qq", str(qq_tree), "--shell", str(self._shell_zip(tmp_path)),
            "--machine-id", "8e1509f19f844df08b0c4497e27dcc37", "--context-only", str(context),
        )
        assert done.returncode == 0, done.stderr
        assert (context / "Dockerfile").read_bytes() == (IMAGE_DIR / "Dockerfile").read_bytes()
        assert (context / "entrypoint.sh").read_bytes() == (IMAGE_DIR / "entrypoint.sh").read_bytes()
        assert (context / "NapCat.Shell.zip").exists()
        assert (context / "qq" / "opt" / "QQ" / "resources" / "app" / "wrapper.node").exists()
        assert (context / "qq" / "usr" / "share" / "applications" / "qq.desktop").exists()
        # macOS 打包带进来的 AppleDouble 文件不进镜像
        assert not (context / "qq" / "opt" / "QQ" / "._junk").exists()
        args = dict(l.split("=", 1) for l in (context / "build.args").read_text(encoding="utf-8").splitlines())
        assert args == {
            "MACHINE_ID": "8e1509f19f844df08b0c4497e27dcc37", "TAG": "kazebot/napcat:4.18.42-qq3.2.34",
            "ALIAS": _image_tag(), "QQ_VERSION": "3.2.34-53644", "NAPCAT_VERSION": "4.18.42",
        }
        assert '-t "$TAG" -t "$NAPCAT_IMAGE"' in _script_code(BUILD_IMAGE)

    def test_a_zip_without_the_core_and_a_broken_tree_are_refused(self, tmp_path: Path, qq_tree: Path) -> None:
        bad_zip = self._build("--qq", str(qq_tree), "--shell", str(self._shell_zip(tmp_path, with_core=False)), "--context-only", str(tmp_path / "a"))
        assert bad_zip.returncode == 1 and "napcat.mjs" in bad_zip.stderr
        (qq_tree / "opt" / "QQ" / "resources" / "app" / "wrapper.node").unlink()
        broken = self._build("--qq", str(qq_tree), "--shell", str(self._shell_zip(tmp_path)), "--context-only", str(tmp_path / "b"))
        assert broken.returncode == 1 and "wrapper.node" in broken.stderr
        bad_id = self._build("--qq", str(qq_tree), "--shell", str(self._shell_zip(tmp_path)), "--machine-id", "nope", "--context-only", str(tmp_path / "c"))
        assert bad_id.returncode == 1 and "machine-id" in bad_id.stderr

    def test_an_explicit_tag_wins_over_the_derived_one(self, tmp_path: Path, qq_tree: Path) -> None:
        context = tmp_path / "ctx"
        done = self._build("--qq", str(qq_tree), "--shell", str(self._shell_zip(tmp_path)), "--tag", "kazebot/napcat:trial", "--context-only", str(context))
        assert done.returncode == 0, done.stderr
        assert "TAG=kazebot/napcat:trial" in (context / "build.args").read_text(encoding="utf-8")
