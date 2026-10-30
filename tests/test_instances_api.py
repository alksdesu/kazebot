"""/v1/instances：控制台的多开实例端点。

建实例会让 root 侧的 oneshot 单元跑起来，单元名就是拼进去的实例名 —— 这里钉住的是
「请求体走到哪个单元名」和「什么样的请求根本走不到那一步」。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api  # noqa: E402
import supervisor.instances as instances  # noqa: E402
from supervisor import provisioning  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.instances import save_instance  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "instances-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_ROUTES = (
    ("GET", "/v1/instances"),
    ("POST", "/v1/instances"),
    ("GET", "/v1/instances/koki/progress"),
    ("DELETE", "/v1/instances/koki"),
    ("POST", "/v1/admin/runtime/code-write"),
)


@pytest.fixture
def units(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """root 侧的单元不真启动，只记下被要求启动的是哪个。"""
    started: list[str] = []

    async def start_unit(unit: str) -> None:
        started.append(unit)

    monkeypatch.setattr(provisioning, "start_unit", start_unit)
    return started


@pytest.fixture
def scaffolded(monkeypatch: pytest.MonkeyPatch) -> list[provisioning.Plan]:
    """本机不一定建得了软链，脚手架本身在 test_provisioning.py 里量。"""
    built: list[provisioning.Plan] = []

    def scaffold(plan: provisioning.Plan, workspace_root: Path) -> None:
        built.append(plan)
        save_instance(
            workspace_root, instance_id=plan.instance_id, label=plan.label, idx=plan.idx,
            platform=plan.platform, account=plan.account,
        )

    monkeypatch.setattr(provisioning, "scaffold", scaffold)
    return built


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "kazebot-data"
    root.mkdir()
    monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(root / "instances.yaml"))
    monkeypatch.delenv("CLONOTH_URL_PREFIX", raising=False)
    return tmp_path


@pytest.fixture
def call(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    monkeypatch.setattr(provisioning, "allocate_index", lambda *_a, **_k: 1)
    app = create_app(
        state=SupervisorState(
            workspace_root=workspace,
            eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-instances"),
            policy=PolicyEngine(workspace_root=workspace),
        ),
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=kwargs.pop("headers", _AUTH), **kwargs)

        return asyncio.run(go())

    return request


class TestAdminToken:
    @pytest.mark.parametrize("method,url", _ROUTES, ids=lambda value: str(value))
    def test_every_route_needs_the_admin_token(self, call, units: list[str], method: str, url: str) -> None:
        # 清单里记着各实例登录的真实账号，建删实例更是直接落到 root 侧。
        response = call(method, url, headers={}, json={"id": "koki"})

        assert response.status_code == 401
        assert units == []


class TestListing:
    def test_a_single_instance_deployment_shows_no_switcher(self, call) -> None:
        assert call("GET", "/v1/instances").json() == {"current_prefix": "", "instances": []}

    def test_each_row_says_whether_it_is_the_one_you_are_on(
        self, call, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_instance(workspace, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")
        save_instance(workspace, instance_id="koki", label="小号", idx=1)
        monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/koki")

        body = call("GET", "/v1/instances").json()

        assert body["current_prefix"] == "/i/koki"
        assert body["instances"] == [
            {
                "id": "rong", "label": "主号", "path": "", "idx": 0,
                "platform": "qq", "account": "1000000001", "current": False,
            },
            {
                "id": "koki", "label": "小号", "path": "/i/koki", "idx": 1,
                "platform": "qq", "account": "", "current": True,
            },
        ]

    def test_an_unreadable_roster_is_reported_instead_of_listed_as_empty(self, call, workspace: Path) -> None:
        # 控制台拿空表当单实例，换号前的双登检查就一律放行了。
        roster = workspace / "kazebot-data" / "instances.yaml"
        roster.write_text("instances:\n  - id: koki\n    account: qq-SECRET-account: x\n", encoding="utf-8")

        response = call("GET", "/v1/instances")

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert str(roster) in detail and "第 3 行第 31 列：mapping values are not allowed here" in detail
        assert "qq-SECRET" not in detail

    def test_listing_carries_an_active_provisioning_job_for_page_recovery(
        self, call, workspace: Path,
    ) -> None:
        save_instance(workspace, instance_id="koki", label="小号", idx=1)
        provisioning.log_path(workspace, "koki").write_text("等 supervisor 应答\n", encoding="utf-8")

        row = call("GET", "/v1/instances").json()["instances"][0]

        assert row["provisioning"]["id"] == "koki"
        assert row["provisioning"]["state"] == "running"


class TestCodeWrite:
    def test_code_write_is_scoped_to_the_current_multi_instance(self, call, units: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/koki")

        response = call("POST", "/v1/admin/runtime/code-write")

        assert response.status_code == 200
        assert response.json()["instance_id"] == "koki"
        assert units == ["kazebot-writable@koki.service"]

    def test_single_instance_cannot_enable_multi_instance_code_write(self, call, units: list[str]) -> None:
        response = call("POST", "/v1/admin/runtime/code-write")

        assert response.status_code == 409
        assert units == []


class TestCreate:
    def test_a_new_instance_is_planned_scaffolded_and_handed_to_root(
        self, call, units: list[str], scaffolded: list[provisioning.Plan], workspace: Path,
    ) -> None:
        response = call(
            "POST", "/v1/instances",
            json={"id": "koki", "label": "小号", "platform": "qq", "account": "1000000002"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "id": "koki", "label": "小号", "idx": 1, "prefix": "/i/koki",
            "ports": {"supervisor": 8775, "bot": 8090, "bridge": 8779, "napcat": 6199},
            "workspace": str(provisioning.data_root(workspace) / "koki"),
            "platform": "qq", "account": "1000000002",
        }
        assert [plan.instance_id for plan in scaffolded] == ["koki"]
        assert units == ["kazebot-provision@koki.service"]

    def test_an_old_console_still_gets_its_one_platform_through(
        self, call, units: list[str], scaffolded: list[provisioning.Plan],
    ) -> None:
        # 没刷新页面的控制台还发着「平台 id: 账号」的映射。
        body = call("POST", "/v1/instances", json={"id": "koki", "accounts": {"qq": "1000000002"}}).json()

        assert (body["platform"], body["account"]) == ("qq", "1000000002")

    def test_an_old_console_asking_for_two_platforms_at_once_is_refused(
        self, call, units: list[str], scaffolded: list[provisioning.Plan],
    ) -> None:
        response = call(
            "POST", "/v1/instances",
            json={"id": "koki", "accounts": {"qq": "1000000002", "telegram": "@koki"}},
        )

        assert response.status_code == 400
        assert "一个实例只跑一个平台" in response.json()["detail"]
        assert scaffolded == [] and units == []

    def test_the_roster_is_claimed_before_root_is_asked_to_start(
        self, call, units: list[str], scaffolded: list[provisioning.Plan], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 并发两个请求时后来的要看见序号已被占，否则两个实例分到同一组端口。
        order: list[str] = []
        scaffold, start_unit = provisioning.scaffold, provisioning.start_unit

        def note_scaffold(plan: provisioning.Plan, workspace_root: Path) -> None:
            order.append("scaffold")
            scaffold(plan, workspace_root)

        async def note_start(unit: str) -> None:
            order.append("start")
            await start_unit(unit)

        monkeypatch.setattr(provisioning, "scaffold", note_scaffold)
        monkeypatch.setattr(provisioning, "start_unit", note_start)

        assert call("POST", "/v1/instances", json={"id": "koki"}).status_code == 200
        assert order == ["scaffold", "start"]
        assert units == ["kazebot-provision@koki.service"]

    def test_a_nameless_instance_is_labelled_with_its_own_name(
        self, call, units: list[str], scaffolded: list[provisioning.Plan],
    ) -> None:
        body = call("POST", "/v1/instances", json={"id": "koki"}).json()

        assert body["label"] == "koki" and (body["platform"], body["account"]) == ("qq", "")

    @pytest.mark.parametrize("body,refusal", [
        ({"id": "Koki"}, "实例名"),
        ({"id": "stickers"}, "stickers"),
        ({"id": ""}, "实例名"),
        ({"id": "koki", "platform": "nowhere"}, "不认识的平台"),
        ({"id": "koki", "platform": "qq_group"}, "不认识的平台"),
        ({"id": "koki", "account": "abc"}, "账号写法不对"),
        ({"id": "koki", "accounts": {"nowhere": "1"}}, "不认识的平台"),
        ({"id": "koki", "accounts": {"qq": "abc"}}, "账号写法不对"),
        ({"id": "koki", "accounts": ["qq"]}, "accounts"),
    ])
    def test_a_refused_plan_never_reaches_root(
        self, call, units: list[str], scaffolded: list[provisioning.Plan],
        body: dict[str, Any], refusal: str,
    ) -> None:
        response = call("POST", "/v1/instances", json=body)

        assert response.status_code == 400 and refusal in response.json()["detail"]
        assert scaffolded == [] and units == []

    def test_a_name_already_on_the_roster_is_refused(
        self, call, units: list[str], scaffolded: list[provisioning.Plan], workspace: Path,
    ) -> None:
        save_instance(workspace, instance_id="koki", label="小号", idx=1)

        response = call("POST", "/v1/instances", json={"id": "koki"})

        assert response.status_code == 400 and "已经在清单里" in response.json()["detail"]
        assert units == []

    def test_a_number_another_instance_holds_is_refused(
        self, call, units: list[str], scaffolded: list[provisioning.Plan], workspace: Path,
    ) -> None:
        save_instance(workspace, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")

        response = call("POST", "/v1/instances", json={"id": "koki", "platform": "qq", "account": "1000000001"})

        assert response.status_code == 400 and "已经归实例 主号 管了" in response.json()["detail"]
        assert units == []

    def test_an_unreadable_roster_stops_creation_and_says_where_it_broke(
        self, call, units: list[str], scaffolded: list[provisioning.Plan], workspace: Path,
    ) -> None:
        # 坏的是服务器上的文件，不是请求：只说「读不懂」，管理员不知道去修哪一行。
        roster = workspace / "kazebot-data" / "instances.yaml"
        roster.write_text("instances:\n  - id: koki\n    account: qq-SECRET-account: x\n", encoding="utf-8")

        response = call("POST", "/v1/instances", json={"id": "xiaoyu"})

        assert response.status_code == 503
        assert response.json()["detail"] == (
            f"实例清单 {roster} 读不懂：第 3 行第 31 列：mapping values are not allowed here。先在服务器上修好它"
        )
        assert "qq-SECRET" not in response.text
        assert scaffolded == [] and units == []

    def test_a_body_that_is_not_an_object_is_refused(self, call, units: list[str]) -> None:
        response = call("POST", "/v1/instances", json=["koki"])

        assert response.status_code == 400 and units == []

    def test_a_workspace_that_cannot_be_built_says_why_instead_of_a_bare_server_error(
        self, call, units: list[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def scaffold(plan: provisioning.Plan, workspace_root: Path) -> None:
            raise PermissionError(13, "Permission denied", str(plan.workspace / "web"))

        monkeypatch.setattr(provisioning, "scaffold", scaffold)

        response = call("POST", "/v1/instances", json={"id": "koki"})

        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail.startswith("新实例没建成：") and "Permission denied" in detail
        assert units == []

    def test_a_creation_root_never_started_ends_its_progress_with_the_reason(
        self, call, scaffolded: list[provisioning.Plan], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 清单已经占上了：进度停在「等 root 侧接手」的话，控制台每次打开都接着轮询一个不会来的结果
        async def refuse(unit: str) -> None:
            raise provisioning.ProvisionError(f"启动 {unit} 失败：Access denied")

        monkeypatch.setattr(provisioning, "start_unit", refuse)

        assert call("POST", "/v1/instances", json={"id": "koki"}).status_code == 400

        progress = call("GET", "/v1/instances/koki/progress").json()
        assert progress["finished"] is True and progress["ok"] is False
        assert progress["detail"] == "启动 kazebot-provision@koki.service 失败：Access denied"

    def test_running_out_of_indexes_is_a_refusal_not_a_crash(
        self, call, units: list[str], scaffolded: list[provisioning.Plan], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 开满了是操作的人要看见的话。穿成 500 的话页面上只有一句「服务器错误」。
        monkeypatch.setattr(provisioning, "allocate_index", instances.allocate_index)
        monkeypatch.setattr(instances, "port_in_use", lambda *_a, **_k: True)

        response = call("POST", "/v1/instances", json={"id": "koki"})

        assert response.status_code == 400 and "没有空闲序号了" in response.json()["detail"]
        assert scaffolded == [] and units == []

    def test_a_full_roster_is_refused_the_same_way(
        self, call, units: list[str], scaffolded: list[provisioning.Plan],
        workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(provisioning, "allocate_index", instances.allocate_index)
        monkeypatch.setattr(instances, "port_in_use", lambda *_a, **_k: False)
        for n in range(instances.MAX_INDEX + 1):
            save_instance(workspace, instance_id=f"inst{n}", label="", idx=n)

        response = call("POST", "/v1/instances", json={"id": "koki"})

        assert response.status_code == 400 and "没有空闲序号了" in response.json()["detail"]
        assert scaffolded == [] and units == []


class TestProgress:
    def test_the_log_is_handed_back_line_by_line(self, call, workspace: Path) -> None:
        provisioning.log_path(workspace, "koki").write_text("拉起容器\nDONE ok\n", encoding="utf-8")

        assert call("GET", "/v1/instances/koki/progress").json() == {
            "id": "koki", "lines": ["拉起容器", "DONE ok"], "finished": True, "ok": True, "detail": "",
        }

    def test_nothing_written_yet_is_not_finished(self, call) -> None:
        body = call("GET", "/v1/instances/koki/progress").json()

        assert body["lines"] == [] and body["finished"] is False

    @pytest.mark.parametrize("name,status", [("stickers", 400), ("Koki", 400), ("..%2F..%2Fetc", 404)])
    def test_a_malformed_name_is_refused_rather_than_probed(
        self, call, workspace: Path, name: str, status: int,
    ) -> None:
        # 实例名会拼进要读的日志文件名。
        assert call("GET", f"/v1/instances/{name}/progress").status_code == status


class TestDelete:
    def test_removing_an_instance_hands_the_name_to_root(
        self, call, units: list[str], workspace: Path,
    ) -> None:
        save_instance(workspace, instance_id="koki", label="小号", idx=1)

        response = call("DELETE", "/v1/instances/koki")

        assert response.status_code == 200 and response.json() == {"ok": True, "id": "koki"}
        assert units == ["kazebot-deprovision@koki.service"]

    @pytest.mark.parametrize("earlier", [
        "拉起容器\nDONE ok\n",
        "[10:00:00] 停服务\nDONE fail: 第 180 行执行失败\n",
    ], ids=["after-creation", "retry-after-failure"])
    def test_progress_read_before_root_starts_is_not_the_earlier_result(
        self, call, units: list[str], workspace: Path, earlier: str,
    ) -> None:
        # root 只往后追加：接手之前控制台紧跟着读到的，是建实例时的 DONE ok 或上一次删除的 DONE fail
        save_instance(workspace, instance_id="koki", label="小号", idx=1)
        provisioning.log_path(workspace, "koki").write_text(earlier, encoding="utf-8")

        assert call("DELETE", "/v1/instances/koki").status_code == 200

        progress = call("GET", "/v1/instances/koki/progress").json()
        assert progress["finished"] is False
        assert (progress["state"], progress["action"]) == ("running", "remove")
        assert progress["lines"][-1] == provisioning.REMOVAL_REQUESTED

    def test_a_removal_root_never_started_ends_its_progress_with_the_reason(
        self, call, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_instance(workspace, instance_id="koki", label="小号", idx=1)

        async def refuse(unit: str) -> None:
            raise provisioning.ProvisionError(f"启动 {unit} 失败：Access denied")

        monkeypatch.setattr(provisioning, "start_unit", refuse)

        assert call("DELETE", "/v1/instances/koki").status_code == 400

        progress = call("GET", "/v1/instances/koki/progress").json()
        assert progress["finished"] is True and progress["ok"] is False
        assert progress["detail"] == "启动 kazebot-deprovision@koki.service 失败：Access denied"

    def test_a_progress_file_that_cannot_be_written_does_not_stop_the_removal(
        self, call, units: list[str], workspace: Path,
    ) -> None:
        save_instance(workspace, instance_id="koki", label="小号", idx=1)
        provisioning.log_path(workspace, "koki").mkdir()

        assert call("DELETE", "/v1/instances/koki").status_code == 200
        assert units == ["kazebot-deprovision@koki.service"]

    def test_the_entry_stays_until_root_says_it_is_done(self, call, units: list[str], workspace: Path) -> None:
        # 由完成方摘清单：root 那边失败的话，至少不会出现「清单查无此实例、进程却还在跑」的幽灵。
        save_instance(workspace, instance_id="koki", label="小号", idx=1)

        call("DELETE", "/v1/instances/koki")

        assert call("GET", "/v1/instances").json()["instances"][0]["id"] == "koki"

    def test_an_unreadable_roster_stops_removal_and_says_where_it_broke(
        self, call, units: list[str], workspace: Path,
    ) -> None:
        roster = workspace / "kazebot-data" / "instances.yaml"
        roster.write_text("instances: [ this is: not: valid\n", encoding="utf-8")

        response = call("DELETE", "/v1/instances/koki")

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail.startswith(f"实例清单 {roster} 读不懂：第 1 行") and detail.endswith("。先在服务器上修好它")
        assert "this is" not in detail
        assert units == []

    @pytest.mark.parametrize("name,prefix,refusal", [
        ("koki", "/i/koki", "正在用"),
        ("rong", "/i/koki", "主实例"),
        ("nobody", "", "不在清单"),
        ("stickers", "", "实例名"),
    ])
    def test_a_refused_removal_never_reaches_root(
        self, call, units: list[str], workspace: Path, monkeypatch: pytest.MonkeyPatch,
        name: str, prefix: str, refusal: str,
    ) -> None:
        save_instance(workspace, instance_id="rong", label="主号", idx=0)
        save_instance(workspace, instance_id="koki", label="小号", idx=1)
        monkeypatch.setenv("CLONOTH_URL_PREFIX", prefix)

        response = call("DELETE", f"/v1/instances/{name}")

        assert response.status_code == 400 and refusal in response.json()["detail"]
        assert units == []
