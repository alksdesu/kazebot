"""配置叠加层：引擎按什么顺序读，控制台按什么规则写。

仓库自带的节点、片段和 runtime 在代码目录的 defaults/ 下，实例的 config/ 里只放改过的那几份。
两侧必须对齐：界面上显示的是哪一份、保存后落到哪一份、引擎下一次读到的是不是它。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from engine import node as node_mod  # noqa: E402
from engine.builtin.agent_manage import create_agent  # noqa: E402
from engine.builtin.knowledge_inject import load_skill_catalog  # noqa: E402
from engine.prompt import _expand_includes  # noqa: E402
from plugins.draw_character_inject import _load_character_tags  # noqa: E402
from supervisor.admin_api import create_admin_router  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BASE = "/v1/admin/config"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _touch(path: Path) -> Path:
    """把 mtime 往后推：连着两次写在同一个时钟刻度里，缓存戳看起来没变。"""
    later = path.stat().st_mtime + 2
    os.utime(path, (later, later))
    return path


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时代码目录，真仓库的 defaults/ 和 tools/ 一个字节都不碰。"""
    root = tmp_path / "code"
    (root / config_layout.DEFAULT_NODES_DIR).mkdir(parents=True)
    (root / config_layout.LEGACY_DRAWTOOLS_DIR).mkdir(parents=True)
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def admin(ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_admin_router(workspace_root=ws), prefix=_BASE)

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, f"{_BASE}{url}", headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


class _Ctx(ToolContext):
    """离线 ToolContext：不连 supervisor，策略一律放行。"""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


# ---------------------------------------------------------------------------
#  引擎读到的是哪一份
# ---------------------------------------------------------------------------

class TestNodeLookup:
    def test_the_bundled_default_is_found_without_any_instance_file(self, ws: Path, code_root: Path) -> None:
        default = _write(config_layout.default_nodes_dir() / "alpha.yaml", "type: ai\nprompt: default\n")

        assert node_mod.node_file(ws, "alpha") == default
        assert node_mod.load_node(ws, "alpha").prompt == "default"

    def test_the_instance_override_wins_over_the_default(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "alpha.yaml", "type: ai\nprompt: default\n")
        own = _write(config_layout.nodes_dir(ws) / "alpha.yaml", "type: ai\nprompt: own\n")

        assert node_mod.node_file(ws, "alpha") == own
        assert node_mod.load_node(ws, "alpha").prompt == "own"

    def test_a_system_node_beats_both(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "alpha.yaml", "type: ai\nprompt: default\n")
        _write(config_layout.nodes_dir(ws) / "alpha.yaml", "type: ai\nprompt: own\n")
        system = _write(ws / "engine" / "system_nodes" / "alpha.yaml", "type: ai\nprompt: system\n")

        assert node_mod.node_file(ws, "alpha") == system

    def test_the_listing_covers_all_three_directories_without_duplicates(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "alpha.yaml", "type: ai\n")
        _write(config_layout.default_nodes_dir() / "beta.yaml", "type: ai\n")
        own = _write(config_layout.nodes_dir(ws) / "alpha.yaml", "type: ai\n")
        system = _write(ws / "engine" / "system_nodes" / "sys.yaml", "type: ai\n")

        found = node_mod.node_files(ws)

        assert set(found) == {system, own, config_layout.default_nodes_dir() / "beta.yaml"}

    def test_templates_and_examples_are_not_dispatchable(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_persistent_agent.template.yaml", "type: ai\n")
        _write(config_layout.default_nodes_dir() / "alpha.example.yaml", "type: ai\n")
        alpha = _write(config_layout.default_nodes_dir() / "alpha.yaml", "type: ai\n")

        assert node_mod.node_files(ws) == [alpha]


class TestPromptIncludes:
    def test_an_instance_fragment_is_inlined(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_shared.md", "default text")
        _write(config_layout.nodes_dir(ws) / "_shared.md", "own text")

        assert _expand_includes("A{{include:_shared.md}}B", ws) == "Aown textB"

    def test_the_bundled_fragment_is_inlined_when_the_instance_has_none(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_shared.md", "default text")

        assert _expand_includes("{{include:_shared.md}}", ws) == "default text"

    def test_the_template_stands_in_for_a_missing_fragment(self, ws: Path, code_root: Path) -> None:
        # 仓库只发 _persona.example.md，用户还没把它改成自己的 _persona.md。
        _write(config_layout.default_nodes_dir() / "_persona.example.md", "example persona")

        assert _expand_includes("{{include:_persona.md}}", ws) == "example persona"

    def test_a_drawtools_include_reads_the_instance_file(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_code_dir() / "character_tags.example.yaml", "characters: []\n")
        _write(config_layout.drawtools_file(ws, "character_tags.yaml"), "characters: [own]\n")

        assert _expand_includes("{{include:drawtools/character_tags.yaml}}", ws) == "characters: [own]\n"

    def test_a_drawtools_include_falls_back_to_the_template(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_code_dir() / "character_tags.example.yaml", "characters: []\n")

        assert _expand_includes("{{include:drawtools/character_tags.yaml}}", ws) == "characters: []\n"

    @pytest.mark.parametrize("name", ["/etc/passwd", "C:/Windows/system.ini", "../secrets.md", "a/../../secrets.md"])
    def test_a_name_that_leaves_the_fragment_directories_is_left_unexpanded(
        self, ws: Path, code_root: Path, name: str,
    ) -> None:
        _write(config_layout.config_dir(ws) / "secrets.md", "leak")
        config_layout.nodes_dir(ws).mkdir(parents=True, exist_ok=True)

        assert _expand_includes(f"{{{{include:{name}}}}}", ws) == f"{{{{include:{name}}}}}"

    def test_a_fragment_that_is_not_there_is_left_unexpanded(self, ws: Path, code_root: Path) -> None:
        assert _expand_includes("{{include:_missing.md}}", ws) == "{{include:_missing.md}}"

    def test_a_fragment_may_include_another_one(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_outer.md", "outer<{{include:_inner.md}}>")
        _write(config_layout.default_nodes_dir() / "_inner.md", "inner")

        assert _expand_includes("{{include:_outer.md}}", ws) == "outer<inner>"

    def test_a_fragment_including_itself_stops_instead_of_spinning(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_loop.md", "{{include:_loop.md}}")

        assert _expand_includes("{{include:_loop.md}}", ws) == "{{include:_loop.md}}"


class TestRuntimeOverlay:
    def test_without_any_file_the_code_defaults_are_used(self, ws: Path, code_root: Path) -> None:
        assert clonoth_runtime.load_runtime_config(ws)["engine"]["max_steps"] == 32

    def test_the_bundled_runtime_overrides_the_code_defaults(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")

        assert clonoth_runtime.load_runtime_config(ws)["engine"]["max_steps"] == 40

    def test_the_instance_file_only_overrides_the_keys_it_writes(self, ws: Path, code_root: Path) -> None:
        # 发版给默认 runtime 加的新键，改过 runtime 的实例也照样拿得到。
        _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n  streaming: true\n")
        _write(config_layout.runtime_path(ws), "engine:\n  max_steps: 64\n")

        merged = clonoth_runtime.load_runtime_config(ws)

        assert merged["engine"]["max_steps"] == 64
        assert merged["engine"]["streaming"] is True
        assert merged["engine"]["retry"]["max_retries"] == 3

    def test_editing_the_bundled_layer_invalidates_the_cache(self, ws: Path, code_root: Path) -> None:
        # 缓存只按实例那份的 mtime 算的话，升级换了默认 runtime 也不会重读。
        default = _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")
        assert clonoth_runtime.load_runtime_config(ws)["engine"]["max_steps"] == 40

        _touch(_write(default, "engine:\n  max_steps: 41\n"))

        assert clonoth_runtime.load_runtime_config(ws)["engine"]["max_steps"] == 41

    def test_editing_the_instance_layer_invalidates_the_cache(self, ws: Path, code_root: Path) -> None:
        own = _write(config_layout.runtime_path(ws), "engine:\n  max_steps: 50\n")
        assert clonoth_runtime.load_runtime_config(ws)["engine"]["max_steps"] == 50

        _touch(_write(own, "engine:\n  max_steps: 51\n"))

        assert clonoth_runtime.load_runtime_config(ws)["engine"]["max_steps"] == 51


class TestSkillsAndDrawtoolsFollowTheLayout:
    def test_skills_are_read_from_the_config_directory(self, ws: Path, code_root: Path) -> None:
        _write(
            config_layout.skills_dir(ws) / "alpha" / "SKILL.md",
            "---\nname: alpha\ndescription: d\n---\nbody\n",
        )

        catalog = load_skill_catalog(ws, _use_cache=False)

        assert [item["name"] for item in catalog] == ["alpha"]

    def test_the_old_skills_directory_is_no_longer_read(self, ws: Path, code_root: Path) -> None:
        # 搬家只复制不删，旧目录还在；再读它就成了改了新位置不生效。
        _write(ws / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\n---\nbody\n")

        assert load_skill_catalog(ws, _use_cache=False) == []

    def test_the_character_library_comes_from_the_instance_file(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_code_dir() / "character_tags.example.yaml", "characters:\n  - name: sample\n")
        _write(config_layout.drawtools_file(ws, "character_tags.yaml"), "characters:\n  - name: own\n")

        assert _load_character_tags(ws) == [{"name": "own"}]

    def test_the_character_library_falls_back_to_the_template(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_code_dir() / "character_tags.example.yaml", "characters:\n  - name: sample\n")

        assert _load_character_tags(ws) == [{"name": "sample"}]

    def test_the_drawtools_subprocess_reads_the_workspace_it_was_given(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 工具以子进程跑，工作区只能从 env 认；认错了就读到另一个实例的绘图设置。
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(ws))
        source = _ROOT / "tools" / "drawtools" / "common.py"
        spec = importlib.util.spec_from_file_location("drawtools_common_under_test", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.WORKSPACE_ROOT == ws
        assert module.SETTINGS_PATH == ws / "config" / "drawtools" / "settings.yaml"
        assert module.CHARACTER_TAGS_PATH == ws / "config" / "drawtools" / "character_tags.yaml"
        assert module.SETTINGS_EXAMPLE_PATH == _ROOT / "tools" / "drawtools" / "settings.example.yaml"


class TestCreateAgentNeverTouchesTheBundledDefaults:
    def test_a_default_node_caller_gets_an_override(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "tmpl.yaml", yaml.safe_dump({"id": "tmpl", "type": "ai"}))
        caller = _write(
            config_layout.default_nodes_dir() / "caller.yaml",
            yaml.safe_dump({"id": "caller", "type": "ai", "delegate_targets": []}),
        )
        before = caller.read_bytes()

        result = asyncio.run(create_agent(
            {"name": "helper", "template": "tmpl", "caller_node_id": "caller"}, _Ctx(ws),
        ))

        assert result["ok"] is True, result
        assert result["data"]["caller_updated"] is True
        assert caller.read_bytes() == before
        override = yaml.safe_load((config_layout.nodes_dir(ws) / "caller.yaml").read_text(encoding="utf-8"))
        assert override["delegate_targets"] == ["helper"]

    def test_the_new_node_lands_in_the_instance_directory(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "tmpl.yaml", yaml.safe_dump({"id": "tmpl", "type": "ai"}))

        result = asyncio.run(create_agent({"name": "helper", "template": "tmpl"}, _Ctx(ws)))

        assert result["data"]["config_path"] == "config/nodes/helper.yaml"
        assert (config_layout.nodes_dir(ws) / "helper.yaml").is_file()
        assert not (config_layout.default_nodes_dir() / "helper.yaml").exists()

    def test_an_id_already_held_by_a_default_node_is_refused(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "tmpl.yaml", yaml.safe_dump({"id": "tmpl", "type": "ai"}))
        _write(config_layout.default_nodes_dir() / "helper.yaml", yaml.safe_dump({"id": "helper", "type": "ai"}))

        result = asyncio.run(create_agent({"name": "helper", "template": "tmpl"}, _Ctx(ws)))

        assert result["ok"] is False
        assert "defaults/nodes/helper.yaml" in result["error"]


# ---------------------------------------------------------------------------
#  控制台写的是哪一份
# ---------------------------------------------------------------------------

class TestThePathGuard:
    @pytest.mark.parametrize("bad", ["..", ".", "../skills-evil", "alpha/../../outside"])
    def test_a_path_leaving_the_base_directory_is_refused(self, admin, bad: str) -> None:
        # 按字符串前缀比会放过 skills-evil 这种同名开头的兄弟目录。
        response = admin("POST", "/skills", json={"id": bad, "content": "x"})

        assert response.status_code == 400

    def test_a_legitimate_nested_name_is_allowed(self, admin, ws: Path) -> None:
        response = admin("POST", "/skills", json={"id": "writing", "content": "---\nname: writing\n---\n"})

        assert response.status_code == 200, response.text
        assert (config_layout.skills_dir(ws) / "writing" / "SKILL.md").is_file()


class TestTheNodesEndpoint:
    def _seed(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "alpha.yaml", "id: alpha\ntype: ai\nprompt: default\n")
        _write(config_layout.default_nodes_dir() / "beta.yaml", "id: beta\ntype: ai\nprompt: default\n")
        _write(config_layout.nodes_dir(ws) / "alpha.yaml", "id: alpha\ntype: ai\nprompt: own\n")
        _write(config_layout.nodes_dir(ws) / "own.yaml", "id: own\ntype: ai\n")
        _write(ws / "engine" / "system_nodes" / "sys.yaml", "id: sys\ntype: ai\n")

    def test_every_entry_says_which_layer_it_came_from(self, admin, ws: Path, code_root: Path) -> None:
        self._seed(ws, code_root)

        rows = {row["id"]: row for row in admin("GET", "/nodes").json()}

        assert rows["sys"]["source"] == "system"
        assert rows["alpha"]["source"] == "override"
        assert rows["beta"]["source"] == "default"
        assert rows["own"]["source"] == "custom"

    def test_an_override_identical_to_the_default_is_flagged(self, admin, ws: Path, code_root: Path) -> None:
        # 一字不差的覆盖什么也没改，只会让以后默认的更新到不了这个实例。
        body = "id: beta\ntype: ai\nprompt: default\n"
        _write(config_layout.default_nodes_dir() / "beta.yaml", body)
        _write(config_layout.nodes_dir(ws) / "beta.yaml", body)

        row = next(row for row in admin("GET", "/nodes").json() if row["id"] == "beta")

        assert row["same_as_default"] is True

    def test_an_override_that_actually_differs_is_not_flagged(self, admin, ws: Path, code_root: Path) -> None:
        self._seed(ws, code_root)

        row = next(row for row in admin("GET", "/nodes").json() if row["id"] == "alpha")

        assert row["same_as_default"] is False

    def test_reading_a_node_returns_the_file_that_is_actually_loaded(self, admin, ws: Path, code_root: Path) -> None:
        self._seed(ws, code_root)

        body = admin("GET", "/nodes/beta/raw").json()

        assert body["content"] == "id: beta\ntype: ai\nprompt: default\n"
        assert body["source"] == "default"
        assert body["path"] == "defaults/nodes/beta.yaml"

    def test_a_node_that_does_not_exist_is_a_404(self, admin, ws: Path, code_root: Path) -> None:
        assert admin("GET", "/nodes/nope/raw").status_code == 404

    def test_saving_a_default_node_creates_an_override(self, admin, ws: Path, code_root: Path) -> None:
        default = _write(config_layout.default_nodes_dir() / "beta.yaml", "id: beta\nprompt: default\n")

        response = admin("PUT", "/nodes/beta/raw", json={"content": "id: beta\nprompt: edited\n"})

        assert response.status_code == 200, response.text
        assert response.json()["path"] == "config/nodes/beta.yaml"
        assert (config_layout.nodes_dir(ws) / "beta.yaml").read_text(encoding="utf-8") == "id: beta\nprompt: edited\n"
        assert default.read_text(encoding="utf-8") == "id: beta\nprompt: default\n"

    def test_saving_a_system_node_is_refused(self, admin, ws: Path, code_root: Path) -> None:
        # 落到 config/nodes/ 的同名文件永远不会被加载，改完看着成功，实际什么也没变。
        _write(ws / "engine" / "system_nodes" / "sys.yaml", "id: sys\n")

        response = admin("PUT", "/nodes/sys/raw", json={"content": "id: sys\nprompt: x\n"})

        assert response.status_code == 409
        assert not (config_layout.nodes_dir(ws) / "sys.yaml").exists()

    def test_the_fingerprint_of_the_default_is_what_the_first_save_compares(
        self, admin, ws: Path, code_root: Path,
    ) -> None:
        default = _write(config_layout.default_nodes_dir() / "beta.yaml", "id: beta\nprompt: default\n")
        shown = admin("GET", "/nodes/beta/raw").json()
        assert shown["hash"] == config_layout.file_hash(default)

        response = admin("PUT", "/nodes/beta/raw", json={"content": "id: beta\n", "base_hash": shown["hash"]})

        assert response.status_code == 200, response.text

    def test_a_stale_fingerprint_is_a_conflict_and_writes_nothing(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "beta.yaml", "id: beta\nprompt: default\n")

        response = admin("PUT", "/nodes/beta/raw", json={"content": "id: beta\n", "base_hash": "0" * 64})

        assert response.status_code == 409
        assert not (config_layout.nodes_dir(ws) / "beta.yaml").exists()

    def test_once_an_override_exists_the_fingerprint_is_the_overrides(self, admin, ws: Path, code_root: Path) -> None:
        # 拿着仓库默认的指纹保存，等于把别人改过的那份覆盖掉。
        default = _write(config_layout.default_nodes_dir() / "beta.yaml", "id: beta\nprompt: default\n")
        own = _write(config_layout.nodes_dir(ws) / "beta.yaml", "id: beta\nprompt: own\n")

        response = admin(
            "PUT", "/nodes/beta/raw",
            json={"content": "id: beta\nprompt: third\n", "base_hash": config_layout.file_hash(default)},
        )

        assert response.status_code == 409
        assert own.read_text(encoding="utf-8") == "id: beta\nprompt: own\n"

    def test_creating_a_node_shadowed_by_a_system_node_names_that_file(
        self, admin, ws: Path, code_root: Path,
    ) -> None:
        _write(ws / "engine" / "system_nodes" / "sys.yaml", "id: sys\n")

        response = admin("POST", "/nodes", json={"id": "sys", "content": "id: sys\n"})

        assert response.status_code == 409
        assert "engine/system_nodes/sys.yaml" in response.json()["detail"]

    def test_a_second_save_leaves_the_previous_version_in_a_bak(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.nodes_dir(ws) / "own.yaml", "id: own\nprompt: first\n")

        admin("PUT", "/nodes/own/raw", json={"content": "id: own\nprompt: second\n"})

        backup = config_layout.nodes_dir(ws) / "own.yaml.bak"
        assert backup.read_text(encoding="utf-8") == "id: own\nprompt: first\n"

    def test_creating_a_node_whose_id_is_already_taken_names_the_file(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "beta.yaml", "id: beta\n")

        response = admin("POST", "/nodes", json={"id": "beta", "content": "id: beta\n"})

        assert response.status_code == 409
        assert "defaults/nodes/beta.yaml" in response.json()["detail"]

    def test_creating_a_node_over_an_existing_instance_file_is_refused(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.nodes_dir(ws) / "own.yaml", "id: own\n")

        assert admin("POST", "/nodes", json={"id": "own", "content": "x"}).status_code == 409

    def test_a_free_id_is_created_in_the_instance_directory(self, admin, ws: Path, code_root: Path) -> None:
        response = admin("POST", "/nodes", json={"id": "scout", "content": "id: scout\ntype: ai\n"})

        assert response.status_code == 200, response.text
        assert (config_layout.nodes_dir(ws) / "scout.yaml").is_file()

    def test_deleting_a_custom_node_removes_it_for_good(self, admin, ws: Path, code_root: Path) -> None:
        own = _write(config_layout.nodes_dir(ws) / "own.yaml", "id: own\n")

        response = admin("DELETE", "/nodes/own")

        assert response.json() == {"ok": True, "restored": False}
        assert not own.exists()

    def test_deleting_an_override_brings_the_default_back(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "beta.yaml", "id: beta\nprompt: default\n")
        _write(config_layout.nodes_dir(ws) / "beta.yaml", "id: beta\nprompt: own\n")

        response = admin("DELETE", "/nodes/beta")

        assert response.json() == {"ok": True, "restored": True}
        assert admin("GET", "/nodes/beta/raw").json()["content"] == "id: beta\nprompt: default\n"

    def test_a_default_node_that_was_never_changed_cannot_be_deleted(self, admin, ws: Path, code_root: Path) -> None:
        default = _write(config_layout.default_nodes_dir() / "beta.yaml", "id: beta\n")

        assert admin("DELETE", "/nodes/beta").status_code == 409
        assert default.is_file()

    def test_a_system_node_cannot_be_deleted(self, admin, ws: Path, code_root: Path) -> None:
        _write(ws / "engine" / "system_nodes" / "sys.yaml", "id: sys\n")

        assert admin("DELETE", "/nodes/sys").status_code == 409

    def test_deleting_a_node_that_is_nowhere_is_a_404(self, admin, ws: Path, code_root: Path) -> None:
        assert admin("DELETE", "/nodes/nope").status_code == 404


class TestTheNodeFilesEndpoint:
    def test_the_listing_merges_both_directories(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_shared.md", "default")
        _write(config_layout.default_nodes_dir() / "_orchestrator.md", "default")
        _write(config_layout.nodes_dir(ws) / "_shared.md", "own")
        _write(config_layout.nodes_dir(ws) / "_extra.md", "own only")

        rows = {row["name"]: row for row in admin("GET", "/node-files").json()}

        assert rows["_shared.md"]["source"] == "override"
        assert rows["_orchestrator.md"]["source"] == "default"
        assert rows["_extra.md"]["source"] == "custom"

    def test_an_override_identical_to_the_default_is_flagged(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_shared.md", "same")
        _write(config_layout.nodes_dir(ws) / "_shared.md", "same")

        rows = {row["name"]: row for row in admin("GET", "/node-files").json()}

        assert rows["_shared.md"]["same_as_default"] is True

    def test_reading_falls_through_to_the_bundled_file(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_shared.md", "default")

        body = admin("GET", "/node-files/_shared.md/raw").json()

        assert (body["content"], body["source"]) == ("default", "default")
        assert body["path"] == "defaults/nodes/_shared.md"

    def test_saving_a_bundled_fragment_creates_an_override(self, admin, ws: Path, code_root: Path) -> None:
        default = _write(config_layout.default_nodes_dir() / "_shared.md", "default")

        response = admin("PUT", "/node-files/_shared.md/raw", json={"content": "edited"})

        assert response.json()["path"] == "config/nodes/_shared.md"
        assert (config_layout.nodes_dir(ws) / "_shared.md").read_text(encoding="utf-8") == "edited"
        assert default.read_text(encoding="utf-8") == "default"

    def test_deleting_an_override_brings_the_bundled_fragment_back(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_shared.md", "default")
        _write(config_layout.nodes_dir(ws) / "_shared.md", "own")

        response = admin("DELETE", "/node-files/_shared.md")

        assert response.json() == {"ok": True, "restored": True}
        assert admin("GET", "/node-files/_shared.md/raw").json()["content"] == "default"

    def test_a_bundled_fragment_that_was_never_changed_cannot_be_deleted(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_shared.md", "default")

        assert admin("DELETE", "/node-files/_shared.md").status_code == 409

    def test_a_fragment_that_is_nowhere_is_a_404(self, admin, ws: Path, code_root: Path) -> None:
        assert admin("DELETE", "/node-files/_nope.md").status_code == 404

    @pytest.mark.parametrize("name", ["notes.txt", "script.py", "archive.zip"])
    def test_only_node_and_fragment_suffixes_are_editable(self, admin, ws: Path, code_root: Path, name: str) -> None:
        assert admin("GET", f"/node-files/{name}/raw").status_code == 400
        assert admin("PUT", f"/node-files/{name}/raw", json={"content": "x"}).status_code == 400


class TestTheRuntimeEndpoint:
    def test_archive_retention_saved_by_console_is_used_without_restart(self, admin, ws: Path, code_root: Path) -> None:
        from engine.context_archive import ContextArchiveStore

        _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n  context_archive:\n    retention_days: 7\n")
        archive = ContextArchiveStore(ws)
        assert archive.retention_days == 7
        raw = admin("GET", "/runtime/raw").json()
        saved = admin("PUT", "/runtime/raw", json={
            "content": "engine:\n  max_steps: 40\n  context_archive:\n    retention_days: 14\n",
            "base_hash": raw["hash"],
        })
        assert saved.status_code == 200
        assert archive.retention_days == 14
        assert clonoth_runtime.load_runtime_config(ws)["engine"]["max_steps"] == 40
        stale = admin("PUT", "/runtime/raw", json={"content": raw["content"], "base_hash": raw["hash"]})
        assert stale.status_code == 409
        assert archive.retention_days == 14

    def test_without_an_instance_file_the_bundled_runtime_is_shown(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")

        body = admin("GET", "/runtime/raw").json()

        assert (body["content"], body["source"]) == ("engine:\n  max_steps: 40\n", "default")
        assert body["path"] == "defaults/runtime.yaml"

    def test_saving_creates_the_instance_file_and_leaves_the_bundled_one(self, admin, ws: Path, code_root: Path) -> None:
        default = _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")

        admin("PUT", "/runtime/raw", json={"content": "engine:\n  max_steps: 64\n"})

        assert config_layout.runtime_path(ws).read_text(encoding="utf-8") == "engine:\n  max_steps: 64\n"
        assert default.read_text(encoding="utf-8") == "engine:\n  max_steps: 40\n"
        assert admin("GET", "/runtime/raw").json()["source"] == "override"

    def test_resetting_removes_the_instance_file(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")
        _write(config_layout.runtime_path(ws), "engine:\n  max_steps: 64\n")

        assert admin("DELETE", "/runtime/raw").json() == {"ok": True, "restored": True}
        assert not config_layout.runtime_path(ws).exists()
        assert admin("GET", "/runtime/raw").json()["source"] == "default"

    def test_resetting_what_was_never_overridden_says_so(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_runtime_path(), "engine:\n  max_steps: 40\n")

        assert admin("DELETE", "/runtime/raw").json() == {"ok": True, "restored": False}


class TestTheOtherRawEndpoints:
    def test_the_model_channels_endpoint_reads_the_new_location(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.models_path(ws), "version: 1\n")

        body = admin("GET", "/config/raw").json()

        assert (body["content"], body["path"]) == ("version: 1\n", "config/models.yaml")

    def test_saving_model_channels_writes_the_new_location(self, admin, ws: Path, code_root: Path) -> None:
        response = admin("PUT", "/config/raw", json={"content": "version: 2\n"})

        assert response.json()["path"] == "config/models.yaml"
        assert config_layout.models_path(ws).read_text(encoding="utf-8") == "version: 2\n"

    def test_the_policy_endpoint_shows_the_bundled_template_until_there_is_a_file(
        self, admin, ws: Path, code_root: Path,
    ) -> None:
        _write(config_layout.default_template(config_layout.POLICY_TEMPLATE), "version: 1\n")

        body = admin("GET", "/policy/raw").json()

        assert body["path"] == "defaults/policy.example.yaml"

    def test_saving_the_policy_writes_the_instance_file(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_template(config_layout.POLICY_TEMPLATE), "version: 1\n")

        response = admin("PUT", "/policy/raw", json={"content": "version: 1\nextra_roots: []\n"})

        assert response.json()["path"] == "config/policy.yaml"
        assert config_layout.policy_path(ws).is_file()

    def test_skills_are_listed_from_the_config_directory(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.skills_dir(ws) / "alpha" / "SKILL.md", "---\nname: alpha\n---\nbody\n")
        _write(ws / "skills" / "legacy" / "SKILL.md", "---\nname: legacy\n---\nbody\n")

        assert [row["name"] for row in admin("GET", "/skills").json()] == ["alpha"]

    def test_saving_a_skill_writes_the_config_directory(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.skills_dir(ws) / "alpha" / "SKILL.md", "---\nname: alpha\n---\nold\n")

        response = admin("PUT", "/skills/alpha/raw", json={"content": "---\nname: alpha\n---\nnew\n"})

        assert response.json()["path"] == "config/skills/alpha/SKILL.md"

    def test_the_mcp_endpoint_reads_and_writes_the_new_location(self, admin, ws: Path, code_root: Path) -> None:
        empty = admin("GET", "/mcp-clients/raw").json()
        assert empty["path"] == "config/mcp.yaml"

        response = admin("PUT", "/mcp-clients/raw", json={"content": "version: 1\nclients: {}\n"})

        assert response.json()["path"] == "config/mcp.yaml"
        assert config_layout.mcp_path(ws).is_file()

    def test_the_fingerprint_of_an_absent_mcp_file_still_saves(self, admin, ws: Path, code_root: Path) -> None:
        # 界面打开的是「还没有这份文件」，带回来的指纹是空串，保存必须当成新建而不是冲突。
        shown = admin("GET", "/mcp-clients/raw").json()

        response = admin(
            "PUT", "/mcp-clients/raw",
            json={"content": "version: 1\nclients: {}\n", "base_hash": shown["hash"]},
        )

        assert response.status_code == 200, response.text


class TestTheDrawtoolsEndpoints:
    def test_the_bundle_falls_back_to_the_templates(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_code_dir() / "settings.example.yaml", "api: {}\n")

        settings = admin("GET", "/drawtools").json()["settings"]

        assert settings["content"] == "api: {}\n"
        assert (settings["exists"], settings["using_example"]) == (False, True)
        assert settings["path"] == "config/drawtools/settings.yaml"

    def test_saving_settings_writes_the_instance_file(self, admin, ws: Path, code_root: Path) -> None:
        example = _write(config_layout.drawtools_code_dir() / "settings.example.yaml", "api: {}\n")

        response = admin("PUT", "/drawtools/settings/raw", json={"content": "api:\n  timeout_sec: 5\n"})

        assert response.json()["path"] == "config/drawtools/settings.yaml"
        assert config_layout.drawtools_file(ws, "settings.yaml").is_file()
        assert example.read_text(encoding="utf-8") == "api: {}\n"

    def test_opening_the_template_and_saving_it_straight_back_works(
        self, admin, ws: Path, code_root: Path,
    ) -> None:
        # 界面显示的是模板，带回来的指纹也是模板的；比错对象的话头一次保存就 409。
        _write(config_layout.drawtools_code_dir() / "settings.example.yaml", "api: {}\n")
        shown = admin("GET", "/drawtools/settings/raw").json()

        response = admin(
            "PUT", "/drawtools/settings/raw",
            json={"content": "api:\n  timeout_sec: 5\n", "base_hash": shown["hash"]},
        )

        assert response.status_code == 200, response.text
        assert admin("GET", "/drawtools/settings/raw").json()["using_example"] is False

    def test_a_prompt_template_is_read_and_written_under_the_instance(self, admin, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_code_dir() / "prompts" / "novelai" / "tag-guide.example.md", "guide\n")

        assert admin("GET", "/drawtools/prompts/tag-guide/raw").json()["content"] == "guide\n"

        response = admin("PUT", "/drawtools/prompts/tag-guide/raw", json={"content": "own guide\n"})

        assert response.json()["path"] == "config/drawtools/prompts/novelai/tag-guide.md"

    def test_an_unknown_prompt_name_is_a_404(self, admin, ws: Path, code_root: Path) -> None:
        assert admin("GET", "/drawtools/prompts/nope/raw").status_code == 404

    def test_init_copies_only_what_is_missing(self, admin, ws: Path, code_root: Path) -> None:
        for name in config_layout.DRAWTOOLS_FILES:
            _write(config_layout.drawtools_code_dir() / config_layout.example_name(name), f"# {name}\n")
        own = _write(config_layout.drawtools_file(ws, "settings.yaml"), "api: mine\n")

        created = admin("POST", "/drawtools/init").json()["created"]

        assert "config/drawtools/settings.yaml" not in created
        assert "config/drawtools/character_tags.yaml" in created
        assert own.read_text(encoding="utf-8") == "api: mine\n"
