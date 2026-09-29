"""共享配置组：几个实例的 config/ 链到数据根下同一份。

换的是整套配置，错一步就等于把一个实例的全部设置换成别人的。所以这里量的是：建组抄走什么、加入时旧配置归档到哪、
退出后两边是不是真的各是各的、删组会不会把还在用它的实例甩掉，以及中途出错时工作区有没有留在能起得来的样子。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor import share_groups  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.instances import save_instance  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.share_groups import ShareGroupError, _make_dir_link  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "share-groups-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _link(link: Path, target: Path) -> None:
    """建一个目录链接。Windows 上普通账号建不了软链，退到目录联接，判定和读写都一样。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    _make_dir_link(link, target)


def _instance(root: Path, name: str) -> Path:
    workspace = root / name
    (workspace / "data").mkdir(parents=True, exist_ok=True)
    (workspace / "config").mkdir(parents=True, exist_ok=True)
    return workspace


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """序号 0 的实例住在代码目录里，别让它落到真仓库上。"""
    root = tmp_path / "code"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "kazebot-data"
    root.mkdir()
    monkeypatch.setenv(config_layout.INSTANCES_FILE_ENV, str(root / "instances.yaml"))
    monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/beta")
    return root


@pytest.fixture
def ws(data_root: Path, code_root: Path) -> Path:
    """本实例是序号 1 的 beta；清单里另有代码目录里的 alpha 和数据根下的 gamma。"""
    workspace = _instance(data_root, "beta")
    _instance(data_root, "gamma")
    save_instance(workspace, instance_id="alpha", label="主号", idx=0)
    save_instance(workspace, instance_id="beta", label="小号", idx=1)
    save_instance(workspace, instance_id="gamma", label="三号", idx=2)
    (workspace / "config" / "models.yaml").write_text("version: 1\n", encoding="utf-8")
    (workspace / "config" / "nodes").mkdir()
    (workspace / "config" / "nodes" / "main.yaml").write_text("id: main\n", encoding="utf-8")
    return workspace


@pytest.fixture
def shared(data_root: Path) -> Path:
    return data_root / config_layout.SHARED_DIR


class TestBeforeAnyGroupExists:
    def test_a_single_instance_deployment_says_it_cannot_share(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(config_layout.INSTANCES_FILE_ENV, raising=False)
        solo = _instance(tmp_path, "solo")

        overview = share_groups.overview(solo)

        assert overview["available"] is False
        assert overview["current"] is None and overview["groups"] == []
        assert overview["reason"]

    def test_creating_a_group_without_a_data_root_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(config_layout.INSTANCES_FILE_ENV, raising=False)
        solo = _instance(tmp_path, "solo")

        with pytest.raises(ShareGroupError) as caught:
            share_groups.create_group(solo, "team")

        assert config_layout.INSTANCES_FILE_ENV in str(caught.value)

    def test_a_multi_instance_deployment_with_no_groups_is_still_available(self, ws: Path) -> None:
        overview = share_groups.overview(ws)

        assert overview == {
            "available": True, "reason": "", "current": None, "groups": [], "archives": [],
        }

    @pytest.mark.parametrize("name", ["", "Team", "1team", "a", "te am", "team-1", "a" * 33, None])
    def test_a_name_that_could_climb_out_of_the_shared_directory_is_refused(
        self, ws: Path, name: Any,
    ) -> None:
        # 组名会直接拼成数据根下的路径，宽一格就能写到别的实例的工作区里。
        with pytest.raises(ShareGroupError):
            share_groups.create_group(ws, name)

    @pytest.mark.parametrize("name", ["..", "../beta", "stickers/../../etc"])
    def test_a_traversal_name_is_refused(self, ws: Path, name: str) -> None:
        with pytest.raises(ShareGroupError):
            share_groups.create_group(ws, name)


class TestCreatingAGroup:
    def test_the_current_config_becomes_the_group_config(self, ws: Path, shared: Path) -> None:
        result = share_groups.create_group(ws, "team")

        assert result["group"] == "team"
        assert (shared / "team" / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"
        assert (shared / "team" / "config" / "nodes" / "main.yaml").is_file()

    def test_the_instance_config_becomes_a_link_to_the_group(self, ws: Path, shared: Path) -> None:
        share_groups.create_group(ws, "team")

        assert config_layout.is_dir_link(ws / "config")
        assert config_layout.config_share_group(ws) == "team"
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"

    def test_the_old_config_is_archived_under_data(self, ws: Path) -> None:
        result = share_groups.create_group(ws, "team")

        archive = ws / result["archived"]
        assert result["archived"].startswith(f"data/{share_groups.ARCHIVE_PREFIX}")
        assert (archive / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"

    def test_the_group_records_who_made_it_and_when(self, ws: Path, shared: Path) -> None:
        share_groups.create_group(ws, "team")

        meta = json.loads((shared / "team" / "group.json").read_text(encoding="utf-8"))
        assert meta["name"] == "team"
        assert meta["created_by"] == "小号"
        assert meta["created_at"]

    def test_shareable_keys_follow_the_config_into_the_group(self, ws: Path, shared: Path) -> None:
        (ws / ".env").write_text(
            "OPENAI_API_KEY=sk-test\nPORT=8090\nCLONOTH_WORKSPACE=/opt/x\nEXA_API_KEY=exa-test\n",
            encoding="utf-8",
        )

        result = share_groups.create_group(ws, "team")

        assert result["copied_env"] == ["EXA_API_KEY", "OPENAI_API_KEY"]
        body = config_layout.read_env_file(shared / "team" / config_layout.SHARED_SECRETS_FILE)
        assert dict(body) == {"OPENAI_API_KEY": "sk-test", "EXA_API_KEY": "exa-test"}

    def test_the_instance_identity_stays_in_the_instance_env(self, ws: Path, shared: Path) -> None:
        # 端口和监听地址抄过去，组里每个实例都会去抢同一个端口。
        (ws / ".env").write_text(
            "HOST=0.0.0.0\nPORT=8090\nONEBOT_ACCESS_TOKEN=abc\nNAPCAT_WEBUI_URL=http://127.0.0.1:6199\n"
            "CLONOTH_PORT=8775\nCLONOTH_PLATFORM=qq\nCLONOTH_ADMIN_TOKEN=t\n",
            encoding="utf-8",
        )

        result = share_groups.create_group(ws, "team")

        assert result["copied_env"] == []
        assert not (shared / "team" / config_layout.SHARED_SECRETS_FILE).exists()

    def test_an_instance_without_an_env_copies_nothing(self, ws: Path, shared: Path) -> None:
        result = share_groups.create_group(ws, "team")

        assert result["copied_env"] == []

    def test_lock_files_are_left_behind(self, ws: Path, shared: Path) -> None:
        # 抄过去的锁文件会让组里的实例以为有人正拿着锁。
        (ws / "config" / "models.yaml.lock").write_text("", encoding="utf-8")
        (ws / "config" / "__pycache__").mkdir()

        share_groups.create_group(ws, "team")

        assert not (shared / "team" / "config" / "models.yaml.lock").exists()
        assert not (shared / "team" / "config" / "__pycache__").exists()

    def test_the_second_group_of_the_same_name_is_refused(self, ws: Path) -> None:
        share_groups.create_group(ws, "team")
        share_groups.leave_group(ws)

        with pytest.raises(ShareGroupError) as caught:
            share_groups.create_group(ws, "team")

        assert "team" in str(caught.value)

    def test_an_instance_already_in_a_group_must_leave_first(self, ws: Path) -> None:
        share_groups.create_group(ws, "team")

        with pytest.raises(ShareGroupError) as caught:
            share_groups.create_group(ws, "other")

        assert "team" in str(caught.value)

    def test_a_dangling_config_link_stops_the_whole_thing(self, ws: Path, data_root: Path) -> None:
        gone = data_root / config_layout.SHARED_DIR / "old" / "config"
        gone.mkdir(parents=True)
        shutil.rmtree(ws / "config")
        _link(ws / "config", gone)
        gone.rmdir()

        with pytest.raises(config_layout.ConfigLayoutError):
            share_groups.create_group(ws, "team")

    def test_a_failed_copy_leaves_the_instance_exactly_as_it_was(
        self, ws: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 半个组加一个指过去的链接，等于这个实例再也起不来。
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        monkeypatch.setattr(shutil, "copytree", boom)

        with pytest.raises(OSError):
            share_groups.create_group(ws, "team")

        assert not config_layout.is_dir_link(ws / "config")
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"
        assert [entry.name for entry in shared.iterdir() if entry.is_dir()] == []

    @pytest.fixture
    def swap_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只让「把链接换成 config/」那一下失败，前面建组目录的那次改名照常。"""
        real = os.replace

        def fail_on_config(src: Any, dst: Any, **kwargs: Any) -> Any:
            if Path(dst).name == config_layout.CONFIG_DIR:
                raise OSError("换不上")
            return real(src, dst, **kwargs)

        monkeypatch.setattr(os, "replace", fail_on_config)

    def test_a_failed_swap_puts_the_archived_config_back(self, swap_fails: None, ws: Path) -> None:
        # 旧配置已经改名归档、新链接还没换上那一瞬间出错，工作区就只剩一个没有 config/ 的壳。
        with pytest.raises(OSError):
            share_groups.create_group(ws, "team")

        assert not config_layout.is_dir_link(ws / "config")
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"
        assert [entry.name for entry in (ws / "data").iterdir()] == []

    def test_a_failed_swap_takes_the_half_built_group_away_again(
        self, swap_fails: None, ws: Path, shared: Path,
    ) -> None:
        # 本实例没换过去，组就当没建过：留着的话名字被占住，重试一次连报错都变成「已经有这个组」。
        with pytest.raises(OSError):
            share_groups.create_group(ws, "team")

        assert list(shared.glob("team*")) == []
        assert share_groups.overview(ws)["groups"] == []

    def test_a_failed_link_takes_the_half_built_group_away_again(
        self, ws: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("建不了链接")

        monkeypatch.setattr(share_groups, "_make_dir_link", boom)

        with pytest.raises(OSError):
            share_groups.create_group(ws, "team")

        assert list(shared.glob("team*")) == []
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"

    def test_retrying_after_a_failed_link_works(
        self, ws: Path, shared: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 回滚得干净到能原名重来一次，否则操作的人只能换个组名。
        real = share_groups._make_dir_link
        pending = [OSError("建不了链接")]

        def fail_once(*args: Any, **kwargs: Any) -> None:
            if pending:
                raise pending.pop()
            real(*args, **kwargs)

        monkeypatch.setattr(share_groups, "_make_dir_link", fail_once)

        with pytest.raises(OSError):
            share_groups.create_group(ws, "team")

        assert share_groups.create_group(ws, "team")["group"] == "team"
        assert config_layout.config_share_group(ws) == "team"


class TestJoiningAGroup:
    @pytest.fixture
    def team(self, ws: Path, data_root: Path) -> Path:
        """gamma 建好的组，本实例还没加进去。"""
        gamma = data_root / "gamma"
        (gamma / "config" / "runtime.yaml").write_text("keep: 1\n", encoding="utf-8")
        share_groups.create_group(gamma, "team")
        return config_layout.share_group_config_dir("team")

    def test_joining_swaps_the_config_for_the_group_one(self, ws: Path, team: Path) -> None:
        result = share_groups.join_group(ws, "team")

        assert result["group"] == "team"
        assert config_layout.config_share_group(ws) == "team"
        assert (ws / "config" / "runtime.yaml").read_text(encoding="utf-8") == "keep: 1\n"

    def test_the_replaced_config_is_archived_not_deleted(self, ws: Path, team: Path) -> None:
        result = share_groups.join_group(ws, "team")

        archive = ws / result["archived"]
        assert archive.is_dir()
        assert (archive / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"

    def test_two_joins_in_the_same_second_get_separate_archives(self, ws: Path, team: Path) -> None:
        # 归档名只到秒，同一秒里加入两次会把上一份盖掉。
        first = share_groups.join_group(ws, "team")["archived"]
        share_groups.leave_group(ws)
        second = share_groups.join_group(ws, "team")["archived"]

        assert first != second
        assert (ws / first).is_dir() and (ws / second).is_dir()

    def test_joining_a_group_that_is_not_there_is_refused(self, ws: Path) -> None:
        with pytest.raises(ShareGroupError) as caught:
            share_groups.join_group(ws, "team")

        assert "team" in str(caught.value)

    def test_joining_the_group_you_are_already_in_is_refused(self, ws: Path, team: Path) -> None:
        share_groups.join_group(ws, "team")

        with pytest.raises(ShareGroupError):
            share_groups.join_group(ws, "team")

    def test_joining_a_second_group_needs_leaving_the_first(self, ws: Path, team: Path) -> None:
        share_groups.join_group(ws, "team")
        (config_layout.share_group_dir("other") / config_layout.CONFIG_DIR).mkdir(parents=True)

        with pytest.raises(ShareGroupError) as caught:
            share_groups.join_group(ws, "other")

        assert "team" in str(caught.value)


class TestLeavingAGroup:
    def test_leaving_copies_the_group_config_back(self, ws: Path, shared: Path) -> None:
        share_groups.create_group(ws, "team")
        (shared / "team" / "config" / "later.yaml").write_text("later: 1\n", encoding="utf-8")

        result = share_groups.leave_group(ws)

        assert result == {"group": "team"}
        assert not config_layout.is_dir_link(ws / "config")
        assert (ws / "config" / "later.yaml").read_text(encoding="utf-8") == "later: 1\n"

    def test_after_leaving_the_two_copies_drift_apart(self, ws: Path, shared: Path) -> None:
        share_groups.create_group(ws, "team")
        share_groups.leave_group(ws)

        (ws / "config" / "models.yaml").write_text("version: 2\n", encoding="utf-8")

        assert (shared / "team" / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"

    def test_leaving_when_you_are_in_no_group_is_refused(self, ws: Path) -> None:
        with pytest.raises(ShareGroupError):
            share_groups.leave_group(ws)

    def test_a_failed_copy_leaves_the_link_in_place(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        share_groups.create_group(ws, "team")

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        monkeypatch.setattr(shutil, "copytree", boom)

        with pytest.raises(OSError):
            share_groups.leave_group(ws)

        assert config_layout.config_share_group(ws) == "team"
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"


class TestConcurrentGroupChanges:
    @pytest.fixture
    def while_waiting(self, monkeypatch: pytest.MonkeyPatch):
        def run(change: Callable[[], Any], meanwhile: Callable[[], None]) -> Any:
            waiting = threading.Event()
            resume = threading.Event()
            original_lock = share_groups._locked_groups
            caller = threading.current_thread()

            @contextmanager
            def controlled_lock(root: Path) -> Iterator[None]:
                if threading.current_thread() is not caller:
                    waiting.set()
                    assert resume.wait(10), "等锁请求未获准继续"
                with original_lock(root):
                    yield

            with monkeypatch.context() as patch:
                patch.setattr(share_groups, "_locked_groups", controlled_lock)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(change)
                    try:
                        assert waiting.wait(10), "请求没有进入组锁"
                        meanwhile()
                    finally:
                        resume.set()
                    return pending.result(timeout=10)

        return run

    def test_leave_uses_the_group_current_when_the_lock_is_acquired(
        self, ws: Path, shared: Path, while_waiting,
    ) -> None:
        share_groups.create_group(ws, "team")
        other = shared / "other" / "config"
        other.mkdir(parents=True)
        (other / "models.yaml").write_text("version: 2\n", encoding="utf-8")

        def switch_group() -> None:
            share_groups.leave_group(ws)
            share_groups.join_group(ws, "other")

        result = while_waiting(lambda: share_groups.leave_group(ws), switch_group)

        assert result == {"group": "other"}
        assert not config_layout.is_dir_link(ws / "config")
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 2\n"
        assert (shared / "team" / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"
        assert (other / "models.yaml").read_text(encoding="utf-8") == "version: 2\n"

    def test_duplicate_leave_is_a_client_error_and_preserves_local_changes(
        self, ws: Path, while_waiting,
    ) -> None:
        share_groups.create_group(ws, "team")

        def leave_and_edit() -> None:
            share_groups.leave_group(ws)
            (ws / "config" / "models.yaml").write_text("version: 2\n", encoding="utf-8")

        with pytest.raises(ShareGroupError, match="不在任何共享组"):
            while_waiting(lambda: share_groups.leave_group(ws), leave_and_edit)

        assert not config_layout.is_dir_link(ws / "config")
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 2\n"
        assert not list(ws.glob(".config.*.leaving"))

    @pytest.mark.parametrize("action", ["create", "join"])
    def test_joined_while_waiting_is_refused_with_the_current_group(
        self, ws: Path, shared: Path, while_waiting, action: str,
    ) -> None:
        (shared / "team" / "config").mkdir(parents=True)
        if action == "join":
            (shared / "other" / "config").mkdir(parents=True)
        change = share_groups.create_group if action == "create" else share_groups.join_group

        with pytest.raises(ShareGroupError, match="team"):
            while_waiting(lambda: change(ws, "other"), lambda: share_groups.join_group(ws, "team"))

        assert config_layout.config_share_group(ws) == "team"
        assert len(list((ws / "data").glob(f"{share_groups.ARCHIVE_PREFIX}*"))) == 1
        if action == "create":
            assert not (shared / "other").exists()

    @pytest.mark.parametrize("action", ["create", "join"])
    def test_left_while_waiting_can_use_the_local_config(
        self, ws: Path, shared: Path, while_waiting, action: str,
    ) -> None:
        share_groups.create_group(ws, "team")
        if action == "join":
            (shared / "other" / "config").mkdir(parents=True)
        change = share_groups.create_group if action == "create" else share_groups.join_group

        result = while_waiting(lambda: change(ws, "other"), lambda: share_groups.leave_group(ws))

        assert result["group"] == "other"
        assert config_layout.config_share_group(ws) == "other"
        assert (ws / result["archived"] / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"


class TestWhoIsInAGroup:
    def test_the_current_instance_is_marked(self, ws: Path) -> None:
        share_groups.create_group(ws, "team")

        groups = share_groups.overview(ws)["groups"]

        assert [g["name"] for g in groups] == ["team"]
        assert groups[0]["members"] == [{"id": "beta", "label": "小号", "current": True}]

    def test_the_root_instance_lives_in_the_code_directory(
        self, ws: Path, code_root: Path, shared: Path,
    ) -> None:
        # 序号 0 的工作区就是代码目录，按数据根下的实例名去找它只会扑空。
        share_groups.create_group(ws, "team")
        shutil.rmtree(code_root / "config")
        _link(code_root / "config", shared / "team" / "config")

        members = share_groups.overview(ws)["groups"][0]["members"]

        assert sorted(m["id"] for m in members) == ["alpha", "beta"]
        assert {m["id"]: m["current"] for m in members} == {"alpha": False, "beta": True}

    def test_an_instance_under_the_data_root_is_found_by_its_name(
        self, ws: Path, data_root: Path, shared: Path,
    ) -> None:
        share_groups.create_group(ws, "team")
        gamma = data_root / "gamma"
        shutil.rmtree(gamma / "config")
        _link(gamma / "config", shared / "team" / "config")

        members = share_groups.overview(ws)["groups"][0]["members"]

        assert sorted(m["id"] for m in members) == ["beta", "gamma"]

    def test_a_group_nobody_joined_lists_no_members(self, ws: Path) -> None:
        share_groups.create_group(ws, "team")
        share_groups.leave_group(ws)

        assert share_groups.overview(ws)["groups"][0]["members"] == []

    def test_the_overview_lists_what_this_instance_archived(self, ws: Path) -> None:
        share_groups.create_group(ws, "team")

        overview = share_groups.overview(ws)

        assert overview["current"] == "team"
        assert len(overview["archives"]) == 1
        assert overview["archives"][0].startswith(f"data/{share_groups.ARCHIVE_PREFIX}")

    def test_a_stray_directory_in_the_shared_root_is_not_a_group(self, ws: Path, shared: Path) -> None:
        share_groups.create_group(ws, "team")
        (shared / "Not A Group").mkdir()
        (shared / "half").mkdir()

        assert [g["name"] for g in share_groups.overview(ws)["groups"]] == ["team"]


class TestDeletingAGroup:
    def test_a_group_with_members_is_kept(self, ws: Path, shared: Path) -> None:
        share_groups.create_group(ws, "team")

        with pytest.raises(ShareGroupError) as caught:
            share_groups.delete_group(ws, "team")

        assert "小号" in str(caught.value)
        assert (shared / "team" / "config").is_dir()

    def test_an_empty_group_is_renamed_rather_than_removed(self, ws: Path, shared: Path) -> None:
        # 清单漏记的实例可能还链着它，改回名字就能救回来。
        share_groups.create_group(ws, "team")
        share_groups.leave_group(ws)

        result = share_groups.delete_group(ws, "team")

        assert result["group"] == "team"
        assert result["archived"].startswith("team.deleted-")
        assert not (shared / "team").exists()
        assert (shared / result["archived"] / "config" / "models.yaml").is_file()

    def test_a_deleted_group_is_no_longer_listed(self, ws: Path) -> None:
        share_groups.create_group(ws, "team")
        share_groups.leave_group(ws)
        share_groups.delete_group(ws, "team")

        assert share_groups.overview(ws)["groups"] == []

    def test_deleting_a_group_that_is_not_there_is_refused(self, ws: Path) -> None:
        with pytest.raises(ShareGroupError):
            share_groups.delete_group(ws, "team")

    def test_deleting_with_a_bad_name_is_refused(self, ws: Path) -> None:
        with pytest.raises(ShareGroupError):
            share_groups.delete_group(ws, "../gamma")


class TestTheHttpEndpoints:
    @pytest.fixture
    def call(self, ws: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
        monkeypatch.setattr(admin_api, "_admin_token", "")
        monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
        app = create_app(
            state=SupervisorState(
                workspace_root=ws,
                eventlog=EventLog(ws / "data" / "events.jsonl", run_id="run-share-groups"),
                policy=PolicyEngine(workspace_root=ws),
            ),
            process_manager=None,
            config_store=ConfigStore(path=ws / "config" / "models.yaml", workspace_root=ws),
        )

        def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            async def go() -> httpx.Response:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                    return await http.request(method, url, headers=kwargs.pop("headers", _AUTH), **kwargs)

            return asyncio.run(go())

        return request

    @pytest.mark.parametrize(("method", "url"), [
        ("GET", "/v1/share-groups"),
        ("POST", "/v1/share-groups"),
        ("POST", "/v1/share-groups/leave"),
        ("POST", "/v1/share-groups/team/join"),
        ("DELETE", "/v1/share-groups/team"),
    ], ids=lambda value: str(value))
    def test_every_route_needs_the_admin_token(self, call, ws: Path, method: str, url: str) -> None:
        # 换整套配置是本机上权限最高的一个动作，控制台之外谁都不该走到。
        response = call(method, url, headers={}, json={"name": "team"})

        assert response.status_code == 401
        assert config_layout.config_share_group(ws) is None

    def test_the_listing_says_the_instance_is_in_no_group(self, call) -> None:
        body = call("GET", "/v1/share-groups").json()

        assert body["available"] is True
        assert body["current"] is None and body["groups"] == []

    def test_creating_a_group_returns_the_new_overview(self, call, ws: Path) -> None:
        body = call("POST", "/v1/share-groups", json={"name": "team"}).json()

        assert body["group"] == "team"
        assert body["overview"]["current"] == "team"
        assert [g["name"] for g in body["overview"]["groups"]] == ["team"]

    def test_creating_a_group_is_written_to_the_event_log(self, call, ws: Path) -> None:
        call("POST", "/v1/share-groups", json={"name": "team"})

        events = [
            json.loads(line)
            for line in (ws / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        changes = [e for e in events if e.get("type") == "share_group_changed"]
        assert [e["payload"]["action"] for e in changes] == ["create"]
        assert changes[0]["payload"]["group"] == "team"

    def test_a_bad_name_comes_back_as_a_client_error(self, call) -> None:
        response = call("POST", "/v1/share-groups", json={"name": "Team"})

        assert response.status_code == 400
        assert response.json()["detail"]

    def test_a_body_that_is_not_an_object_is_refused(self, call) -> None:
        assert call("POST", "/v1/share-groups", json=["team"]).status_code == 400

    def test_an_empty_body_is_refused(self, call) -> None:
        assert call("POST", "/v1/share-groups", content=b"").status_code == 400

    def test_joining_and_leaving_go_through_the_same_way(self, call, ws: Path, data_root: Path) -> None:
        gamma = data_root / "gamma"
        share_groups.create_group(gamma, "team")

        joined = call("POST", "/v1/share-groups/team/join").json()
        assert joined["group"] == "team" and joined["archived"]
        assert config_layout.config_share_group(ws) == "team"

        left = call("POST", "/v1/share-groups/leave").json()
        assert left["group"] == "team"
        assert config_layout.config_share_group(ws) is None

    def test_leaving_when_you_are_in_no_group_is_a_client_error(self, call) -> None:
        assert call("POST", "/v1/share-groups/leave").status_code == 400

    def test_deleting_a_group_with_members_is_a_client_error(self, call, ws: Path) -> None:
        call("POST", "/v1/share-groups", json={"name": "team"})

        response = call("DELETE", "/v1/share-groups/team")

        assert response.status_code == 400
        assert config_layout.config_share_group(ws) == "team"

    def test_deleting_an_empty_group_reports_where_it_went(self, call, ws: Path) -> None:
        call("POST", "/v1/share-groups", json={"name": "team"})
        call("POST", "/v1/share-groups/leave")

        body = call("DELETE", "/v1/share-groups/team").json()

        assert body["archived"].startswith("team.deleted-")
        assert body["overview"]["groups"] == []

    def test_a_disk_failure_is_a_server_error(
        self, call, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        monkeypatch.setattr(shutil, "copytree", boom)

        response = call("POST", "/v1/share-groups", json={"name": "team"})

        assert response.status_code == 500
        assert "磁盘满了" in response.json()["detail"]
