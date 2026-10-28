"""实例清单：多开实例写不了数据根下的清单，换上的号记在自己工作区里，读清单的人叠上去。

写清单的一方读不懂文件就停手，不拿空表盖回去；锁文件和 root 侧删实例脚本用的是同一个名字。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _lock_holder import WAIT_SEC, LockHolder  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from supervisor import instances, platform_accounts  # noqa: E402
from supervisor.instances import (  # noqa: E402
    InstanceDirectoryError,
    instance_workspace,
    instances_file,
    is_instance_id,
    load_instances,
    record_account,
    remove_instance,
    save_instance,
)

_MAIN_ACCOUNT = "1000000001"
_OLD_ACCOUNT = "1000000002"
_NEW_ACCOUNT = "1000000003"


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """序号 0 的实例住在代码目录里，别让它落到真仓库上。"""
    root = tmp_path / "code"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code_root: Path) -> Path:
    """数据根下的共享清单，本进程是挂在 /i/koki 下的多开实例。"""
    root = tmp_path / "kazebot-data"
    root.mkdir()
    monkeypatch.setenv(config_layout.INSTANCES_FILE_ENV, str(root / "instances.yaml"))
    monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/koki")
    return root


@pytest.fixture
def koki(data_root: Path) -> Path:
    workspace = data_root / "koki"
    (workspace / "data").mkdir(parents=True)
    save_instance(workspace, instance_id="rong", label="主号", idx=0, account=_MAIN_ACCOUNT)
    save_instance(workspace, instance_id="koki", label="小号", idx=1, account=_OLD_ACCOUNT)
    return workspace


def _report(workspace: Path, instance_id: str, account: str, platform: str = "qq") -> Path:
    path = config_layout.instance_account_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"id": instance_id, "platform": platform, "account": account}), encoding="utf-8")
    return path


def _accounts(workspace: Path) -> dict[str, str]:
    return {row["id"]: row["account"] for row in load_instances(workspace)}


def _stored_accounts(data_root: Path) -> dict[str, str]:
    rows = yaml.safe_load((data_root / "instances.yaml").read_text(encoding="utf-8"))["instances"]
    return {row["id"]: row.get("account") or "" for row in rows}


class TestWhereEachInstanceLives:
    def test_this_instance_the_main_one_and_the_others(self, koki: Path, data_root: Path, code_root: Path) -> None:
        rows = {row["id"]: row for row in load_instances(koki)}

        assert instance_workspace(koki, rows["koki"]) == koki
        assert instance_workspace(koki, rows["rong"]) == code_root
        assert instance_workspace(koki, {"id": "gamma", "path": "/i/gamma", "idx": 2}) == data_root / "gamma"

    @pytest.mark.parametrize("bad", ["../escape", "Upper", "stickers", "shared", "a", "x" * 40, "", "1e5"])
    def test_a_name_that_is_not_an_instance_id_maps_nowhere(self, koki: Path, bad: str) -> None:
        # 清单手改得动，拿任意字符串拼出来的路径可能落在数据根外面。
        assert not is_instance_id(bad)
        assert instance_workspace(koki, {"id": bad, "path": "/i/x", "idx": 3}) is None

    def test_without_a_data_root_the_others_cannot_be_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(config_layout.INSTANCES_FILE_ENV, raising=False)
        monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/koki")

        assert instance_workspace(tmp_path, {"id": "gamma", "path": "/i/gamma", "idx": 2}) is None


class TestSelfReportedAccounts:
    def test_a_reported_account_wins_over_the_roster(self, koki: Path) -> None:
        _report(koki, "koki", _NEW_ACCOUNT)

        assert _accounts(koki) == {"rong": _MAIN_ACCOUNT, "koki": _NEW_ACCOUNT}

    def test_the_main_instance_report_is_read_from_the_code_directory(self, koki: Path, code_root: Path) -> None:
        _report(code_root, "rong", _NEW_ACCOUNT)

        assert _accounts(koki)["rong"] == _NEW_ACCOUNT

    def test_a_report_written_under_another_name_is_ignored(self, koki: Path, code_root: Path) -> None:
        # 代码目录里那份可能是主实例改名之前留下的。
        _report(code_root, "old_main", _NEW_ACCOUNT)

        assert _accounts(koki)["rong"] == _MAIN_ACCOUNT

    @pytest.mark.parametrize("body", ["[broken", "- a list\n", "id: koki\nplatform: qq\n", "id: koki\naccount: '1'\n"])
    def test_an_unusable_report_falls_back_to_the_roster(self, koki: Path, body: str) -> None:
        config_layout.instance_account_path(koki).write_text(body, encoding="utf-8")

        assert _accounts(koki)["koki"] == _OLD_ACCOUNT

    def test_an_unreadable_report_is_skipped_without_its_text(
        self, koki: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        config_layout.instance_account_path(koki).write_text(
            "id: koki\nplatform: qq\naccount: qq-SECRET-account: x\n", encoding="utf-8",
        )

        with caplog.at_level(logging.DEBUG, logger="supervisor.instances"):
            accounts = _accounts(koki)

        assert accounts["koki"] == _OLD_ACCOUNT
        assert "第 3 行第 27 列：mapping values are not allowed here" in caplog.text
        assert "qq-SECRET" not in caplog.text

    def test_a_double_login_is_caught_through_the_report(self, koki: Path, code_root: Path) -> None:
        _report(code_root, "rong", _NEW_ACCOUNT)

        owner = instances.account_owner(koki, "qq", _NEW_ACCOUNT, prefix="/i/koki")

        assert owner is not None and owner["id"] == "rong"


class TestRecordingAnAccount:
    def test_a_sandboxed_instance_records_into_its_own_workspace(
        self, koki: Path, data_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # kazebot@ 的沙箱只放开它自己的工作区：数据根是只读挂载，access 也报不可写。
        roster = data_root / "instances.yaml"
        before = roster.read_bytes()
        real_access = instances.os.access
        monkeypatch.setattr(
            instances.os, "access",
            lambda path, mode, **kw: False if Path(path) == data_root else real_access(path, mode, **kw),
        )

        assert record_account(koki, prefix="/i/koki", platform_id="qq", account=_NEW_ACCOUNT) is True

        assert roster.read_bytes() == before
        assert yaml.safe_load(config_layout.instance_account_path(koki).read_text(encoding="utf-8")) == {
            "id": "koki", "platform": "qq", "account": _NEW_ACCOUNT,
        }
        assert _accounts(koki)["koki"] == _NEW_ACCOUNT

    def test_a_writable_roster_gets_the_account_too(self, koki: Path, data_root: Path) -> None:
        assert record_account(koki, prefix="/i/koki", platform_id="qq", account=_NEW_ACCOUNT) is True

        assert _stored_accounts(data_root)["koki"] == _NEW_ACCOUNT
        assert _accounts(koki)["koki"] == _NEW_ACCOUNT

    def test_switching_back_to_the_roster_account_is_a_change(self, koki: Path) -> None:
        # 比的是叠上上报之后的那个号：上报写着新号、清单还是旧号时，换回旧号也得记下来。
        _report(koki, "koki", _NEW_ACCOUNT)

        assert record_account(koki, prefix="/i/koki", platform_id="qq", account=_OLD_ACCOUNT) is True
        assert _accounts(koki)["koki"] == _OLD_ACCOUNT

    def test_a_report_that_cannot_be_written_is_an_error_not_a_quiet_success(
        self, koki: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def refuse(*_args: Any, **_kwargs: Any) -> str:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(instances.config_layout, "write_config_text", refuse)

        with pytest.raises(OSError):
            record_account(koki, prefix="/i/koki", platform_id="qq", account=_NEW_ACCOUNT)

    def test_a_roster_mirror_failure_is_only_logged(
        self, koki: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(instances, "_write", refuse)

        with caplog.at_level(logging.WARNING, logger="supervisor.instances"):
            assert record_account(koki, prefix="/i/koki", platform_id="qq", account=_NEW_ACCOUNT) is True

        assert _accounts(koki)["koki"] == _NEW_ACCOUNT
        assert "disk full" in caplog.text


class TestABrokenRosterIsNeverOverwritten:
    @pytest.fixture
    def broken(self, koki: Path, data_root: Path) -> Path:
        roster = data_root / "instances.yaml"
        roster.write_text("instances: [ this is: not: valid\n", encoding="utf-8")
        return roster

    def test_saving_refuses(self, koki: Path, broken: Path) -> None:
        before = broken.read_bytes()
        with pytest.raises(InstanceDirectoryError):
            save_instance(koki, instance_id="gamma", label="三号", idx=2)
        assert broken.read_bytes() == before

    def test_removing_refuses(self, koki: Path, broken: Path) -> None:
        before = broken.read_bytes()
        with pytest.raises(InstanceDirectoryError):
            remove_instance(koki, "koki")
        assert broken.read_bytes() == before

    def test_recording_refuses(self, koki: Path, broken: Path) -> None:
        before = broken.read_bytes()
        with pytest.raises(InstanceDirectoryError):
            record_account(koki, prefix="/i/koki", platform_id="qq", account=_NEW_ACCOUNT)
        assert broken.read_bytes() == before

    def test_a_reader_that_only_shows_it_sees_nothing_and_logs_once(
        self, koki: Path, broken: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="supervisor.instances"):
            assert load_instances(koki) == []
            assert load_instances(koki) == []

        assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 1

    def test_a_caller_that_needs_the_truth_gets_an_error(self, koki: Path, broken: Path) -> None:
        with pytest.raises(InstanceDirectoryError):
            load_instances(koki, required=True)

    def test_the_warning_names_the_place_but_not_the_line(
        self, koki: Path, data_root: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # 清单里记着各实例登的号，日志会被导出、贴进工单。
        (data_root / "instances.yaml").write_text(
            "instances:\n  - id: koki\n    account: qq-SECRET-account: x\n", encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="supervisor.instances"):
            assert load_instances(koki) == []

        assert "第 3 行第 31 列：mapping values are not allowed here" in caplog.text
        assert "qq-SECRET" not in caplog.text

    def test_the_error_a_writer_gets_carries_no_line_on_its_chain(self, koki: Path, data_root: Path) -> None:
        # 开实例、删实例再包一层 raise ... from 往上抛，漏到 500 时整条链都会打进日志。
        (data_root / "instances.yaml").write_text(
            "instances:\n  - id: koki\n    account: qq-SECRET-account: x\n", encoding="utf-8",
        )

        with pytest.raises(InstanceDirectoryError) as caught:
            load_instances(koki, required=True)

        assert str(caught.value) == "实例目录暂时不可用"
        for link in (caught.value.__cause__, caught.value.__context__):
            assert link is not None and "qq-SECRET" not in str(link)

    def test_the_reason_a_writer_gets_names_the_file_and_the_place(self, koki: Path, data_root: Path) -> None:
        roster = data_root / "instances.yaml"
        roster.write_text("instances:\n  - id: koki\n    account: qq-SECRET-account: x\n", encoding="utf-8")

        with pytest.raises(InstanceDirectoryError) as caught:
            load_instances(koki, required=True)

        assert caught.value.reason == (
            f"实例清单 {roster} 读不懂：第 3 行第 31 列：mapping values are not allowed here"
        )

    @pytest.mark.parametrize("body,problem", [
        ("instances: koki\n", "instances 不是列表"),
        ("just text\n", "顶层既不是列表，也不是带 instances 的映射"),
    ])
    def test_a_roster_of_the_wrong_shape_says_which_shape(
        self, koki: Path, data_root: Path, body: str, problem: str,
    ) -> None:
        roster = data_root / "instances.yaml"
        roster.write_text(body, encoding="utf-8")

        with pytest.raises(InstanceDirectoryError) as caught:
            load_instances(koki, required=True)

        assert str(caught.value) == "实例目录格式无效"
        assert caught.value.reason == f"实例清单 {roster} 读不懂：{problem}"

    def test_the_double_login_check_cannot_pass_a_roster_it_cannot_read(self, koki: Path, broken: Path) -> None:
        # 查不了不等于没人占着：当成空表，同一个号就能在两个实例上登。
        with pytest.raises(InstanceDirectoryError):
            instances.account_owner(koki, "qq", _MAIN_ACCOUNT, prefix="/i/koki")

    def test_an_empty_roster_is_no_instances_yet_not_an_error(self, koki: Path, data_root: Path) -> None:
        (data_root / "instances.yaml").write_text("", encoding="utf-8")

        rows = save_instance(koki, instance_id="gamma", label="三号", idx=2)

        assert [row["id"] for row in rows] == ["gamma"]


class TestTheRosterLock:
    def test_writers_wait_for_the_lock_file_config_layout_names(self, koki: Path, data_root: Path) -> None:
        # root 侧删实例脚本 flock 的就是这个文件，两边名字不同就互相锁不住。
        roster = data_root / "instances.yaml"
        holder = LockHolder(config_layout.lock_path(roster))
        done = threading.Event()

        def save() -> None:
            save_instance(koki, instance_id="gamma", label="三号", idx=2)
            done.set()

        worker = threading.Thread(target=save)
        try:
            worker.start()
            assert not done.wait(0.5)
        finally:
            holder.release()
        worker.join(WAIT_SEC)
        assert done.is_set()
        assert config_layout.lock_path(roster).name == ".instances.yaml.lock"
        assert not roster.with_name("instances.yaml.lock").exists()

    def test_the_root_side_script_flocks_the_same_file(self) -> None:
        script = (_ROOT / "deploy" / "provision_instance.sh").read_text(encoding="utf-8")
        name = config_layout.lock_path(Path("instances.yaml")).name

        assert f'flock "$DATA_ROOT/{name}"' in script
        # flock 可能以 root 建出锁文件，要还给 kazebot，supervisor 下次才锁得上。
        assert f'chown "$OWNER:$OWNER" "$DATA_ROOT/instances.yaml" "$DATA_ROOT/{name}"' in script


class TestTheConsoleIsTold:
    """号已经换上了，没记进清单只是别的实例少一道拦截：响应照样成功，但要带上原因。"""

    class _Ops:
        async def switch(self, account: str) -> dict[str, Any]:
            return {"account": account, "pinned": True}

        async def pin(self) -> str:
            return _NEW_ACCOUNT

    @pytest.fixture
    def state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        monkeypatch.delenv(config_layout.INSTANCES_FILE_ENV, raising=False)
        events: list[dict[str, Any]] = []
        return SimpleNamespace(workspace_root=tmp_path, eventlog=SimpleNamespace(append=lambda **kw: events.append(kw)))

    @pytest.mark.parametrize("failure", [OSError("read-only"), InstanceDirectoryError("实例目录暂时不可用")])
    def test_a_failed_record_comes_back_as_roster_error(
        self, state: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: Exception,
    ) -> None:
        def refuse(*_args: Any, **_kwargs: Any) -> bool:
            raise failure

        monkeypatch.setattr(platform_accounts, "record_account", refuse)

        switched = asyncio.run(platform_accounts.switch_account(state, QQ_NAPCAT, self._Ops(), _NEW_ACCOUNT))
        pinned = asyncio.run(platform_accounts.pin_account(state, QQ_NAPCAT, self._Ops()))

        assert switched["account"] == _NEW_ACCOUNT and str(failure) in switched["roster_error"]
        assert pinned["ok"] is True and str(failure) in pinned["roster_error"]

    def test_a_clean_record_adds_nothing(self, state: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_accounts, "record_account", lambda *_args, **_kwargs: True)

        switched = asyncio.run(platform_accounts.switch_account(state, QQ_NAPCAT, self._Ops(), _NEW_ACCOUNT))

        assert "roster_error" not in switched

    def test_a_roster_error_says_where_the_file_broke(
        self, state: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def refuse(*_args: Any, **_kwargs: Any) -> bool:
            raise InstanceDirectoryError("实例目录暂时不可用", "实例清单 /srv/instances.yaml 读不懂：第 2 行第 5 列：x")

        monkeypatch.setattr(platform_accounts, "record_account", refuse)

        switched = asyncio.run(platform_accounts.switch_account(state, QQ_NAPCAT, self._Ops(), _NEW_ACCOUNT))

        assert switched["roster_error"].endswith("实例清单 /srv/instances.yaml 读不懂：第 2 行第 5 列：x")

    def test_a_roster_nobody_can_read_stops_the_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from fastapi import HTTPException

        roster = tmp_path / "instances.yaml"
        roster.write_text("instances: [ this is: not: valid\n", encoding="utf-8")
        monkeypatch.setenv(config_layout.INSTANCES_FILE_ENV, str(roster))
        switched: list[str] = []

        class Ops(self._Ops):
            async def switch(self, account: str) -> dict[str, Any]:
                switched.append(account)
                return await super().switch(account)

        state = SimpleNamespace(workspace_root=tmp_path, eventlog=SimpleNamespace(append=lambda **_kw: None))

        with pytest.raises(HTTPException) as caught:
            asyncio.run(platform_accounts.switch_account(state, QQ_NAPCAT, Ops(), _NEW_ACCOUNT))

        assert caught.value.status_code == 503
        assert caught.value.detail.startswith(f"查不了 {_NEW_ACCOUNT} 是不是已经登在别的实例上，没有换号：实例清单 {roster}")
        assert switched == []
