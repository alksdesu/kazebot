"""每一处整份写回文本文件的地方，都不能改掉这份文件原本的换行符。

Path.write_text 默认按平台转换换行：Windows 上保存一次就把整份 LF 文件改成 CRLF，
Linux 上把整份 CRLF 文件改成 LF —— 用户只改了一行，diff 却是整个文件。
断言一律按字节，两个平台上结论必须一样。

读取一侧不在这里的射程内：接口读出来仍然走通用换行，返回给前端的永远是 LF。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DRAWTOOLS = _ROOT / "tools" / "drawtools"
for _p in (str(_ROOT), str(_DRAWTOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import supervisor.admin_api as admin_api  # noqa: E402
import toolbox.mcp_runtime as mcp_runtime  # noqa: E402
from adapters._shared.live_config import BOOL, LiveConfig, LiveKey, int_in  # noqa: E402
from engine.builtin.agent_manage import create_agent  # noqa: E402
from supervisor.admin_api import create_admin_router  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.scheduler import save_schedules, schedules_path  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import OpenAIConfigUpdateIn  # noqa: E402
from toolbox.builtins.apply_diff import apply_diff  # noqa: E402
from toolbox.builtins.manage_secret import manage_secret  # noqa: E402
from toolbox.builtins.search_in_files import search_in_files  # noqa: E402
from toolbox.builtins.write_file import write_file  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

_CRLF_SEED = b"version: 1\r\nkeep: me\r\nnested:\r\n  a: 1\r\n"
_LF_SEED = b"version: 1\nkeep: me\nnested:\n  a: 1\n"

# 新内容一律用 LF 送进来：前端拿到的原文永远是 LF，保存回去的也是 LF。
_NEW_TEXT = "version: 2\nkeep: me\nnested:\n  a: 2\n"


# ---------------------------------------------------------------------------
#  断言
# ---------------------------------------------------------------------------

def assert_crlf(path: Path) -> None:
    """整份都是 CRLF：没有落单的 LF，也没有被转了两遍的 \\r\\r\\n。"""
    data = path.read_bytes()
    assert b"\r\n" in data, f"{path.name} 丢了 CRLF：{data!r}"
    assert b"\n" not in data.replace(b"\r\n", b""), f"{path.name} 混进了裸 LF：{data!r}"
    assert b"\r\r" not in data, f"{path.name} 被转了两遍：{data!r}"


def assert_lf(path: Path) -> None:
    data = path.read_bytes()
    assert b"\r" not in data, f"{path.name} 被转成了 CRLF：{data!r}"
    assert b"\n" in data, f"{path.name} 一行都没有：{data!r}"


def as_crlf(text: str) -> bytes:
    return text.replace("\n", "\r\n").encode("utf-8")


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """/v1/admin/config/** 的 TestClient，和 test_config_write_backup 同一个装法。"""
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_admin_router(workspace_root=tmp_path), prefix="/v1/admin/config")

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, f"/v1/admin/config{url}", headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


@pytest.fixture
def supervisor_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """整个 supervisor app：平台配置和全局功能设置挂在这上面。"""
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.delenv("CLONOTH_QQ_CONFIG_PATH", raising=False)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-newline"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=kwargs.pop("headers", _AUTH), **kwargs)

        return asyncio.run(go())

    return call


class _Ctx(ToolContext):
    """离线 ToolContext：不连 supervisor，按预设 safety_level 直接放行。"""

    def __init__(self, workspace_root: Path, *, safety: str = "auto") -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
        )
        self._safety = safety

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": self._safety}

    async def check_cancelled(self) -> bool:
        return False


@pytest.fixture
def ctx(tmp_path: Path) -> _Ctx:
    return _Ctx(tmp_path)


# ---------------------------------------------------------------------------
#  控制台的十二个 raw 配置端点（admin_api._write_text）
# ---------------------------------------------------------------------------

# 端点、落盘路径；覆盖不同目录布局与文件类型（yaml / py / md）。
RAW_ENDPOINTS = (
    ("/config/raw", "config/models.yaml"),
    ("/runtime/raw", "config/runtime.yaml"),
    ("/policy/raw", "config/policy.yaml"),
    ("/schedules/raw", "data/schedules.yaml"),
    ("/mcp-clients/raw", "config/mcp.yaml"),
    ("/nodes/qq.orchestrator/raw", "config/nodes/qq.orchestrator.yaml"),
    ("/node-files/shared.md/raw", "config/nodes/shared.md"),
    ("/skills/writing/raw", "config/skills/writing/SKILL.md"),
    ("/tools/weather/raw", "config/tools/weather.py"),
)

# /tools/{name}/raw 只认注册表里已有的工具，头一份得从 POST 建。
CREATE_ENDPOINTS = (
    ("/nodes", "config/nodes/scout.yaml", "scout"),
    ("/tools", "config/tools/weather.py", "weather"),
)


class TestTheConsoleRawConfigEndpoints:
    @pytest.mark.parametrize("url,rel", RAW_ENDPOINTS)
    def test_a_crlf_file_stays_crlf(self, admin, tmp_path: Path, url: str, rel: str) -> None:
        seed(tmp_path / rel, _CRLF_SEED)

        response = admin("PUT", url, json={"content": _NEW_TEXT})

        assert response.status_code == 200, response.text
        assert_crlf(tmp_path / rel)
        assert (tmp_path / rel).read_bytes() == as_crlf(_NEW_TEXT)

    @pytest.mark.parametrize("url,rel", RAW_ENDPOINTS)
    def test_an_lf_file_stays_lf(self, admin, tmp_path: Path, url: str, rel: str) -> None:
        seed(tmp_path / rel, _LF_SEED)

        response = admin("PUT", url, json={"content": _NEW_TEXT})

        assert response.status_code == 200, response.text
        assert_lf(tmp_path / rel)
        assert (tmp_path / rel).read_bytes() == _NEW_TEXT.encode("utf-8")

    @pytest.mark.parametrize(
        "url,rel", [pair for pair in RAW_ENDPOINTS if not pair[0].startswith("/tools/")],
    )
    def test_a_first_write_lands_verbatim(self, admin, tmp_path: Path, url: str, rel: str) -> None:
        # 文件还不存在：内容是什么就写什么，Windows 上也不许偷偷变成 CRLF。
        response = admin("PUT", url, json={"content": _NEW_TEXT})

        assert response.status_code == 200, response.text
        assert (tmp_path / rel).read_bytes() == _NEW_TEXT.encode("utf-8")

    @pytest.mark.parametrize("url,rel,ident", CREATE_ENDPOINTS)
    def test_a_created_file_lands_verbatim(
        self, admin, tmp_path: Path, url: str, rel: str, ident: str,
    ) -> None:
        response = admin("POST", url, json={"id": ident, "content": _NEW_TEXT})

        assert response.status_code == 200, response.text
        assert (tmp_path / rel).read_bytes() == _NEW_TEXT.encode("utf-8")

    def test_content_that_arrives_with_crlf_still_follows_the_file(self, admin, tmp_path: Path) -> None:
        # 前端偶尔会把 CRLF 一起 POST 回来。以文件为准，不是以请求体为准。
        seed(tmp_path / "config/runtime.yaml", _LF_SEED)

        admin("PUT", "/runtime/raw", json={"content": _NEW_TEXT.replace("\n", "\r\n")})

        assert (tmp_path / "config/runtime.yaml").read_bytes() == _NEW_TEXT.encode("utf-8")

    def test_saving_a_crlf_file_twice_does_not_pile_up_carriage_returns(
        self, admin, tmp_path: Path,
    ) -> None:
        seed(tmp_path / "config/runtime.yaml", _CRLF_SEED)

        admin("PUT", "/runtime/raw", json={"content": _NEW_TEXT})
        first = (tmp_path / "config/runtime.yaml").read_bytes()
        admin("PUT", "/runtime/raw", json={"content": _NEW_TEXT})

        assert (tmp_path / "config/runtime.yaml").read_bytes() == first
        assert_crlf(tmp_path / "config/runtime.yaml")

    @pytest.mark.parametrize("data", [_CRLF_SEED, _LF_SEED])
    def test_the_backup_is_a_byte_for_byte_copy(self, admin, tmp_path: Path, data: bytes) -> None:
        # .bak 是覆盖前的退路，换行被改过的备份等于退不回去。
        seed(tmp_path / "config/runtime.yaml", data)

        admin("PUT", "/runtime/raw", json={"content": _NEW_TEXT})

        assert (tmp_path / "config/runtime.yaml.bak").read_bytes() == data

    def test_the_readback_is_always_lf(self, admin, tmp_path: Path) -> None:
        # 读一侧没改：接口给前端的永远是 LF，否则编辑器里每行尾巴都多一个字符。
        seed(tmp_path / "config/runtime.yaml", _CRLF_SEED)

        body = admin("GET", "/runtime/raw").json()

        assert "\r" not in body["content"]


class TestTheDrawtoolsEndpoints:
    DRAW_ENDPOINTS = (
        ("/drawtools/settings/raw", "config/drawtools/settings.yaml"),
        ("/drawtools/characters/raw", "config/drawtools/character_tags.yaml"),
        ("/drawtools/prompts/top-system/raw", "config/drawtools/prompts/novelai/top-system.md"),
        ("/drawtools/prompts/tag-guide/raw", "config/drawtools/prompts/novelai/tag-guide.md"),
    )

    @pytest.mark.parametrize("url,rel", DRAW_ENDPOINTS)
    def test_a_crlf_file_stays_crlf(self, admin, tmp_path: Path, url: str, rel: str) -> None:
        seed(tmp_path / rel, _CRLF_SEED)

        response = admin("PUT", url, json={"content": _NEW_TEXT})

        assert response.status_code == 200, response.text
        assert (tmp_path / rel).read_bytes() == as_crlf(_NEW_TEXT)

    @pytest.mark.parametrize("url,rel", DRAW_ENDPOINTS)
    def test_an_lf_file_stays_lf(self, admin, tmp_path: Path, url: str, rel: str) -> None:
        seed(tmp_path / rel, _LF_SEED)

        admin("PUT", url, json={"content": _NEW_TEXT})

        assert (tmp_path / rel).read_bytes() == _NEW_TEXT.encode("utf-8")

    @pytest.mark.parametrize("url,rel", DRAW_ENDPOINTS)
    def test_a_first_write_lands_verbatim(self, admin, tmp_path: Path, url: str, rel: str) -> None:
        admin("PUT", url, json={"content": _NEW_TEXT})

        assert (tmp_path / rel).read_bytes() == _NEW_TEXT.encode("utf-8")


# ---------------------------------------------------------------------------
#  平台配置与全局功能设置（config_files.write_raw，tmp + replace）
# ---------------------------------------------------------------------------

_QQ_TEXT = "channels:\n  allowed_groups: [1]\n"
_FEATURES_TEXT = "trigger:\n  signals:\n    name: true\n"


class TestThePlatformConfigFile:
    def test_a_crlf_qq_yaml_stays_crlf(self, supervisor_call, tmp_path: Path) -> None:
        seed(tmp_path / "config/qq.yaml", as_crlf("channels:\r\n  allowed_groups: [9]\r\n"))

        response = supervisor_call(
            "PUT", "/v1/platforms/qq/config/raw", json={"content": _QQ_TEXT},
        )

        assert response.status_code == 200, response.text
        assert (tmp_path / "config/qq.yaml").read_bytes() == as_crlf(_QQ_TEXT)

    def test_an_lf_qq_yaml_stays_lf(self, supervisor_call, tmp_path: Path) -> None:
        seed(tmp_path / "config/qq.yaml", b"channels:\n  allowed_groups: [9]\n")

        supervisor_call("PUT", "/v1/platforms/qq/config/raw", json={"content": _QQ_TEXT})

        assert (tmp_path / "config/qq.yaml").read_bytes() == _QQ_TEXT.encode("utf-8")

    def test_a_first_write_lands_verbatim(self, supervisor_call, tmp_path: Path) -> None:
        supervisor_call("PUT", "/v1/platforms/qq/config/raw", json={"content": _QQ_TEXT})

        assert (tmp_path / "config/qq.yaml").read_bytes() == _QQ_TEXT.encode("utf-8")

    def test_the_readback_is_always_lf(self, supervisor_call, tmp_path: Path) -> None:
        seed(tmp_path / "config/qq.yaml", as_crlf(_QQ_TEXT))

        body = supervisor_call("GET", "/v1/platforms/qq/config/raw").json()

        assert "\r" not in body["content"]

    def test_the_replace_leaves_no_tmp_file_behind(self, supervisor_call, tmp_path: Path) -> None:
        seed(tmp_path / "config/qq.yaml", as_crlf(_QQ_TEXT))

        supervisor_call("PUT", "/v1/platforms/qq/config/raw", json={"content": _QQ_TEXT})

        assert not list((tmp_path / "config").glob("*.tmp"))
        assert [p.name for p in (tmp_path / "config").iterdir() if p.name.startswith("qq")] == ["qq.yaml"]

    def test_the_legacy_qq_route_writes_the_same_bytes(self, admin, tmp_path: Path) -> None:
        # 没刷新的旧控制台还在调 /v1/admin/config/qq/raw，它也走 write_raw。
        seed(tmp_path / "config/qq.yaml", as_crlf("channels:\r\n  allowed_groups: [9]\r\n"))

        response = admin("PUT", "/qq/raw", json={"content": _QQ_TEXT})

        assert response.status_code == 200, response.text
        assert (tmp_path / "config/qq.yaml").read_bytes() == as_crlf(_QQ_TEXT)


class TestTheGlobalFeatureConfigFile:
    def test_a_crlf_features_yaml_stays_crlf(self, supervisor_call, tmp_path: Path) -> None:
        seed(tmp_path / "config/features.yaml", as_crlf("trigger:\n  signals:\n    name: false\n"))

        response = supervisor_call(
            "PUT", "/v1/features/config/raw", json={"content": _FEATURES_TEXT},
        )

        assert response.status_code == 200, response.text
        assert (tmp_path / "config/features.yaml").read_bytes() == as_crlf(_FEATURES_TEXT)

    def test_an_lf_features_yaml_stays_lf(self, supervisor_call, tmp_path: Path) -> None:
        seed(tmp_path / "config/features.yaml", b"trigger:\n  signals:\n    name: false\n")

        supervisor_call("PUT", "/v1/features/config/raw", json={"content": _FEATURES_TEXT})

        assert (tmp_path / "config/features.yaml").read_bytes() == _FEATURES_TEXT.encode("utf-8")

    def test_a_first_write_lands_verbatim(self, supervisor_call, tmp_path: Path) -> None:
        supervisor_call("PUT", "/v1/features/config/raw", json={"content": _FEATURES_TEXT})

        assert (tmp_path / "config/features.yaml").read_bytes() == _FEATURES_TEXT.encode("utf-8")


# ---------------------------------------------------------------------------
#  模型渠道配置（config_store._save_yaml / _save_raw）
# ---------------------------------------------------------------------------

class TestTheModelConfigStore:
    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_upserting_a_provider_keeps_the_newline(
        self, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        path = seed(tmp_path / "config/models.yaml", data)
        store = ConfigStore(path=path, workspace_root=tmp_path)

        store.upsert_provider("openai", base_url="https://api.example.com", model="gpt-4o")

        checker(path)
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["openai"]["model"] == "gpt-4o"

    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_switching_the_active_model_keeps_the_newline(
        self, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        # 群里一句「/切换模型」就走这条，落的是 _save_yaml。
        path = seed(tmp_path / "config/models.yaml", data)
        store = ConfigStore(path=path, workspace_root=tmp_path)

        store.update_openai(OpenAIConfigUpdateIn(model="gpt-4o-mini"))

        checker(path)
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["openai"]["model"] == "gpt-4o-mini"

    @pytest.mark.parametrize("newline,checker", [(b"\r\n", assert_crlf), (b"\n", assert_lf)])
    def test_the_defaults_rewrite_keeps_the_newline(
        self, tmp_path: Path, newline: bytes, checker: Callable[[Path], None],
    ) -> None:
        # 顶层不是映射时 reload() 会落一份默认配置回去，走的是 _save_yaml。
        path = seed(tmp_path / "config/models.yaml", b"- one\n- two\n".replace(b"\n", newline))
        store = ConfigStore(path=path, workspace_root=tmp_path)

        store.reload()

        checker(path)

    def test_a_brand_new_config_file_is_written_as_lf(self, tmp_path: Path) -> None:
        path = tmp_path / "config/models.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        store = ConfigStore(path=path, workspace_root=tmp_path)

        store.upsert_provider("openai", model="gpt-4o")

        assert_lf(path)


# ---------------------------------------------------------------------------
#  策略文件（policy.replace_config 的 tmp + replace，以及第一次生成）
# ---------------------------------------------------------------------------

class TestThePolicyFile:
    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_replacing_the_policy_keeps_the_newline(
        self, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        path = seed(tmp_path / "config/policy.yaml", data)
        engine = PolicyEngine(workspace_root=tmp_path)

        engine.replace_config({"version": 1, "extra_roots": ["/srv/shared"]})

        checker(path)
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["extra_roots"] == ["/srv/shared"]

    def test_a_generated_policy_file_is_lf(self, tmp_path: Path) -> None:
        # 没有 config/policy.yaml 时构造 PolicyEngine 会落一份默认策略。
        PolicyEngine(workspace_root=tmp_path)

        assert_lf(tmp_path / "config" / "policy.yaml")

    def test_the_replace_leaves_no_tmp_file_behind(self, tmp_path: Path) -> None:
        seed(tmp_path / "config/policy.yaml", _CRLF_SEED)
        engine = PolicyEngine(workspace_root=tmp_path)

        engine.replace_config({"version": 1})

        # 锁文件挨着配置放，是替换之外唯一留下的东西。
        assert sorted(p.name for p in (tmp_path / "config").iterdir()) == [".policy.yaml.lock", "policy.yaml"]

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
    def test_the_replace_does_not_loosen_the_permissions(self, tmp_path: Path) -> None:
        # tmp 走 umask 通常是 0644，替换过去会把策略暴露给同机其他用户。
        path = seed(tmp_path / "config/policy.yaml", _CRLF_SEED)
        os.chmod(path, 0o600)
        engine = PolicyEngine(workspace_root=tmp_path)

        engine.replace_config({"version": 1})

        assert path.stat().st_mode & 0o777 == 0o600
        assert_crlf(path)

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
    def test_a_generated_policy_file_is_owner_only(self, tmp_path: Path) -> None:
        PolicyEngine(workspace_root=tmp_path)

        assert (tmp_path / "config" / "policy.yaml").stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
#  定时任务表（scheduler._write_schedule_file 的 tmp + replace）
# ---------------------------------------------------------------------------

_SCHEDULE = {"id": "s1", "cron": "0 9 * * *", "node": "qq.orchestrator", "prompt": "早安"}


class TestTheScheduleFile:
    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_saving_schedules_keeps_the_newline(
        self, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        path = seed(schedules_path(tmp_path), data)

        save_schedules(tmp_path, [_SCHEDULE])

        checker(path)
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["schedules"][0]["id"] == "s1"

    def test_a_first_schedule_file_is_lf(self, tmp_path: Path) -> None:
        save_schedules(tmp_path, [_SCHEDULE])

        assert_lf(schedules_path(tmp_path))

    def test_the_replace_leaves_no_tmp_file_behind(self, tmp_path: Path) -> None:
        path = seed(schedules_path(tmp_path), _CRLF_SEED)

        save_schedules(tmp_path, [_SCHEDULE])

        assert not list(path.parent.glob("*.tmp"))


# ---------------------------------------------------------------------------
#  文件工具：write_file / apply_diff / search_in_files
# ---------------------------------------------------------------------------

class TestTheWriteFileTool:
    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_overwriting_follows_the_existing_file(
        self, ctx: _Ctx, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        path = seed(tmp_path / "notes/todo.md", data)

        result = asyncio.run(write_file({"path": "notes/todo.md", "content": _NEW_TEXT}, ctx))

        assert result["ok"] is True
        checker(path)

    def test_a_new_file_takes_the_content_verbatim(self, ctx: _Ctx, tmp_path: Path) -> None:
        asyncio.run(write_file({"path": "notes/fresh.md", "content": _NEW_TEXT}, ctx))

        assert (tmp_path / "notes/fresh.md").read_bytes() == _NEW_TEXT.encode("utf-8")

    def test_a_new_file_keeps_crlf_the_model_sent(self, ctx: _Ctx, tmp_path: Path) -> None:
        asyncio.run(write_file({"path": "notes/fresh.md", "content": "a\r\nb\r\n"}, ctx))

        assert (tmp_path / "notes/fresh.md").read_bytes() == b"a\r\nb\r\n"

    def test_the_reported_size_is_what_is_on_disk_not_what_was_sent(
        self, ctx: _Ctx, tmp_path: Path,
    ) -> None:
        # 覆盖 CRLF 文件时每行多一个字节，按入参内容算就会少报。
        path = seed(tmp_path / "notes/todo.md", _CRLF_SEED)
        content = "one\ntwo\nthree\n"

        result = asyncio.run(write_file({"path": "notes/todo.md", "content": content}, ctx))

        assert result["data"]["bytes"] == path.stat().st_size
        assert result["data"]["bytes"] != len(content.encode("utf-8"))

    @pytest.mark.parametrize(
        "data,content",
        [
            (_LF_SEED, "one\ntwo\n"),
            (_CRLF_SEED, "one\ntwo\n"),
            (_LF_SEED, "你好\n好\n"),
            (_CRLF_SEED, "你好\n好\n"),
            (None, "one\ntwo\n"),
        ],
    )
    def test_the_reported_size_always_matches_the_file(
        self, ctx: _Ctx, tmp_path: Path, data: bytes | None, content: str,
    ) -> None:
        path = tmp_path / "notes/todo.md"
        if data is None:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            seed(path, data)

        result = asyncio.run(write_file({"path": "notes/todo.md", "content": content}, ctx))

        assert result["data"]["bytes"] == path.stat().st_size


class TestTheApplyDiffTool:
    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_a_patched_file_keeps_its_newline(
        self, ctx: _Ctx, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        # 读一侧走通用换行，所以 search 串按 LF 写；写回要还原成原文件的样子。
        path = seed(tmp_path / "notes/todo.md", data)

        result = asyncio.run(apply_diff(
            {"path": "notes/todo.md", "diffs": [{"search": "keep: me\n", "replace": "keep: us\n"}]},
            ctx,
        ))

        assert result["ok"] is True, result
        checker(path)
        assert "keep: us" in path.read_text(encoding="utf-8")

    def test_only_the_patched_line_changes_in_a_crlf_file(self, ctx: _Ctx, tmp_path: Path) -> None:
        # 整份换行被改掉时，diff 会显示每一行都变了 —— 这条钉住「只有一行变」。
        path = seed(tmp_path / "notes/todo.md", _CRLF_SEED)

        asyncio.run(apply_diff(
            {"path": "notes/todo.md", "diffs": [{"search": "keep: me\n", "replace": "keep: us\n"}]},
            ctx,
        ))

        assert path.read_bytes() == _CRLF_SEED.replace(b"keep: me", b"keep: us")


class TestTheSearchReplaceTool:
    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_a_replaced_file_keeps_its_newline(
        self, ctx: _Ctx, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        path = seed(tmp_path / "notes/todo.md", data)

        result = asyncio.run(search_in_files(
            {"query": "keep", "mode": "replace", "replace": "hold", "path": "notes", "pattern": "**/*.md"},
            ctx,
        ))

        assert result.get("ok") is not False, result
        checker(path)
        assert "hold: me" in path.read_text(encoding="utf-8")

    def test_only_the_matched_text_changes_in_a_crlf_file(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "notes/todo.md", _CRLF_SEED)

        asyncio.run(search_in_files(
            {"query": "keep", "mode": "replace", "replace": "hold", "path": "notes", "pattern": "**/*.md"},
            ctx,
        ))

        assert path.read_bytes() == _CRLF_SEED.replace(b"keep", b"hold")


# ---------------------------------------------------------------------------
#  .env（manage_secret._atomic_write 的 tmp + replace）
# ---------------------------------------------------------------------------

class TestTheDotenvFile:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (b"EXISTING=1\r\nOTHER=2\r\n", b"EXISTING=1\r\nOTHER=2\r\nNEW_KEY=v\r\n"),
            (b"EXISTING=1\nOTHER=2\n", b"EXISTING=1\nOTHER=2\nNEW_KEY=v\n"),
        ],
    )
    def test_setting_a_secret_keeps_the_newline(
        self, ctx: _Ctx, tmp_path: Path, data: bytes, expected: bytes,
    ) -> None:
        path = seed(tmp_path / ".env", data)

        result = asyncio.run(manage_secret({"action": "set", "name": "NEW_KEY", "value": "v"}, ctx))

        assert result["ok"] is True, result
        assert path.read_bytes() == expected

    @pytest.mark.parametrize(
        "data,expected",
        [
            (b"KEEP=1\r\nDROP=2\r\n", b"KEEP=1\r\n"),
            (b"KEEP=1\nDROP=2\n", b"KEEP=1\n"),
        ],
    )
    def test_unsetting_a_secret_keeps_the_newline(
        self, ctx: _Ctx, tmp_path: Path, data: bytes, expected: bytes,
    ) -> None:
        path = seed(tmp_path / ".env", data)

        asyncio.run(manage_secret({"action": "unset", "name": "DROP"}, ctx))

        assert path.read_bytes() == expected

    def test_a_brand_new_dotenv_is_lf(self, ctx: _Ctx, tmp_path: Path) -> None:
        asyncio.run(manage_secret({"action": "set", "name": "NEW_KEY", "value": "v"}, ctx))

        assert (tmp_path / ".env").read_bytes() == b"NEW_KEY=v\n"


# ---------------------------------------------------------------------------
#  MCP 客户端配置（mcp_runtime._save_raw_config）
# ---------------------------------------------------------------------------

_MCP_SPEC = {"id": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "server"]}


class TestTheMcpClientConfig:
    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_upserting_a_client_keeps_the_newline(
        self, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        path = seed(tmp_path / "config/mcp.yaml", data)

        mcp_runtime.upsert_client(tmp_path, _MCP_SPEC)

        checker(path)
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["clients"]["fs"]["command"] == "npx"

    @pytest.mark.parametrize("data,checker", [(_CRLF_SEED, assert_crlf), (_LF_SEED, assert_lf)])
    def test_deleting_a_client_keeps_the_newline(
        self, tmp_path: Path, data: bytes, checker: Callable[[Path], None],
    ) -> None:
        path = seed(tmp_path / "config/mcp.yaml", data)
        mcp_runtime.upsert_client(tmp_path, _MCP_SPEC)

        assert mcp_runtime.delete_client(tmp_path, "fs") is True
        checker(path)

    def test_a_first_mcp_config_is_lf(self, tmp_path: Path) -> None:
        mcp_runtime.upsert_client(tmp_path, _MCP_SPEC)

        assert_lf(tmp_path / "config" / "mcp.yaml")


# ---------------------------------------------------------------------------
#  create_agent：新节点文件与调用者的 delegate_targets
# ---------------------------------------------------------------------------

_TEMPLATE = {"id": "tmpl", "type": "ai", "name": "模板", "tool_access": {"mode": "all"}}


class TestTheCreateAgentTool:
    def test_a_new_node_file_is_written_as_lf(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "config/nodes/tmpl.yaml", yaml.safe_dump(_TEMPLATE).encode("utf-8"))

        result = asyncio.run(create_agent({"name": "helper", "template": "tmpl"}, ctx))

        assert result["ok"] is True, result
        assert_lf(tmp_path / "config/nodes/helper.yaml")

    @pytest.mark.parametrize("newline,checker", [(b"\r\n", assert_crlf), (b"\n", assert_lf)])
    def test_the_callers_delegate_list_keeps_its_newline(
        self, ctx: _Ctx, tmp_path: Path, newline: bytes, checker: Callable[[Path], None],
    ) -> None:
        seed(tmp_path / "config/nodes/tmpl.yaml", yaml.safe_dump(_TEMPLATE).encode("utf-8"))
        caller = seed(
            tmp_path / "config/nodes/boss.yaml",
            yaml.safe_dump({"id": "boss", "type": "ai", "delegate_targets": ["old"]})
            .encode("utf-8").replace(b"\n", newline),
        )

        result = asyncio.run(create_agent(
            {"name": "helper", "template": "tmpl", "caller_node_id": "boss"}, ctx,
        ))

        assert result["data"]["caller_updated"] is True, result
        checker(caller)
        assert yaml.safe_load(caller.read_text(encoding="utf-8"))["delegate_targets"] == ["old", "helper"]


# ---------------------------------------------------------------------------
#  热载配置的进程内写回（LiveConfig.save 的 tmp + replace）
# ---------------------------------------------------------------------------

_LIVE_KEYS = (
    LiveKey("enabled", "feature.enabled", BOOL, False),
    LiveKey("limit", "feature.limit", int_in(min_value=1, max_value=10), 3),
)


def _live(path: Path) -> LiveConfig:
    return LiveConfig(_LIVE_KEYS, path=lambda: path)


class TestTheLiveConfigSave:
    def test_a_crlf_config_stays_crlf(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "chat.yaml", as_crlf("feature:\n  enabled: false\n  limit: 3\n"))
        config = _live(path)

        config.save({"feature": {"enabled": True, "limit": 5}})

        assert_crlf(path)
        assert config.live.limit == 5

    def test_an_lf_config_stays_lf(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "chat.yaml", b"feature:\n  enabled: false\n  limit: 3\n")
        config = _live(path)

        config.save({"feature": {"enabled": True, "limit": 5}})

        assert_lf(path)
        assert config.live.limit == 5

    def test_a_first_save_is_lf(self, tmp_path: Path) -> None:
        config = _live(tmp_path / "chat.yaml")

        config.save({"feature": {"enabled": True, "limit": 5}})

        assert_lf(tmp_path / "chat.yaml")

    def test_saving_twice_does_not_pile_up_carriage_returns(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "chat.yaml", as_crlf("feature:\n  enabled: false\n"))
        config = _live(path)

        config.save({"feature": {"enabled": True, "limit": 5}})
        first = path.read_bytes()
        config.save({"feature": {"enabled": True, "limit": 5}})

        assert path.read_bytes() == first
        assert b"\r\r" not in first

    def test_the_replace_leaves_no_tmp_file_behind(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "chat.yaml", as_crlf("feature:\n  enabled: false\n"))

        _live(path).save({"feature": {"enabled": True}})

        assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["chat.yaml"]


# ---------------------------------------------------------------------------
#  drawtools 的用户配置
# ---------------------------------------------------------------------------

_DRAW_SETTINGS = {
    "params": {
        "selected_preset_id": "default-v45-full",
        "presets": [
            {"id": "cute", "name": "可爱风", "aliases": ["萌"]},
            {"id": "default-v45-full", "name": "默认 (V4.5 Full)"},
        ],
    },
}


class TestTheDrawtoolsSettingsFile:
    @pytest.fixture
    def settings_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        import common as drawtools_common

        def install(newline: bytes) -> Path:
            path = tmp_path / "settings.yaml"
            body = yaml.safe_dump(_DRAW_SETTINGS, allow_unicode=True, sort_keys=False)
            path.write_bytes(body.encode("utf-8").replace(b"\n", newline))
            monkeypatch.setattr(drawtools_common, "SETTINGS_PATH", path)
            return path

        return install

    def test_a_crlf_settings_file_stays_crlf(self, settings_file) -> None:
        import common as drawtools_common

        path = settings_file(b"\r\n")

        target = drawtools_common.set_selected_preset("萌")

        assert target["id"] == "cute"
        assert_crlf(path)
        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert written["params"]["selected_preset_id"] == "cute"

    def test_an_lf_settings_file_stays_lf(self, settings_file) -> None:
        import common as drawtools_common

        path = settings_file(b"\n")

        drawtools_common.set_selected_preset("萌")

        assert_lf(path)
        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert written["params"]["selected_preset_id"] == "cute"

    def test_switching_twice_does_not_pile_up_carriage_returns(self, settings_file) -> None:
        import common as drawtools_common

        path = settings_file(b"\r\n")

        drawtools_common.set_selected_preset("萌")
        first = path.read_bytes()
        drawtools_common.set_selected_preset("萌")

        assert path.read_bytes() == first
        assert b"\r\r" not in first


class TestBootstrappingTheDrawtoolsUserConfigs:
    @pytest.fixture
    def draw_dirs(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
        """(仓库发的模板目录, 实例的运行时配置目录)。"""
        import config_io

        code = tmp_path / "code"
        (code / "tools" / "drawtools" / "prompts" / "novelai").mkdir(parents=True)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setattr(config_io.config_layout, "CODE_ROOT", code)
        monkeypatch.setattr(config_io, "WORKSPACE_ROOT", workspace)
        return code / "tools" / "drawtools", workspace / "config" / "drawtools"

    EXAMPLES = (
        ("settings.example.yaml", "settings.yaml"),
        ("character_tags.example.yaml", "character_tags.yaml"),
        ("prompts/novelai/top-system.example.md", "prompts/novelai/top-system.md"),
        ("prompts/novelai/output-format.example.md", "prompts/novelai/output-format.md"),
        ("prompts/novelai/tag-guide.example.md", "prompts/novelai/tag-guide.md"),
    )

    @pytest.mark.parametrize("data", [_CRLF_SEED, _LF_SEED])
    def test_the_copy_is_byte_for_byte(self, draw_dirs: tuple[Path, Path], data: bytes) -> None:
        import config_io

        examples, runtime = draw_dirs
        for src, _dst in self.EXAMPLES:
            seed(examples / src, data)

        created = config_io.ensure_user_configs()

        assert len(created) == len(self.EXAMPLES)
        for src, dst in self.EXAMPLES:
            assert (runtime / dst).read_bytes() == (examples / src).read_bytes() == data

    def test_the_new_files_are_reported_by_their_workspace_path(self, draw_dirs: tuple[Path, Path]) -> None:
        import config_io

        examples, _runtime = draw_dirs
        seed(examples / "settings.example.yaml", _LF_SEED)

        assert config_io.ensure_user_configs() == ["config/drawtools/settings.yaml"]

    def test_an_existing_user_config_is_not_touched(self, draw_dirs: tuple[Path, Path]) -> None:
        import config_io

        examples, runtime = draw_dirs
        seed(examples / "settings.example.yaml", _LF_SEED)
        mine = seed(runtime / "settings.yaml", _CRLF_SEED)

        created = config_io.ensure_user_configs()

        assert created == []
        assert mine.read_bytes() == _CRLF_SEED

    def test_a_missing_example_creates_nothing(self, draw_dirs: tuple[Path, Path]) -> None:
        import config_io

        _examples, runtime = draw_dirs

        assert config_io.ensure_user_configs() == []
        assert not (runtime / "settings.yaml").exists()
