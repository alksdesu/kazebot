"""config_layout：每份配置放在哪、怎么写回去、启动时怎么从旧位置搬过来。

写入的三条底线：跨进程锁 + 同目录临时文件替换，换行和权限照旧，指纹对不上一个字节都不写。
搬家的三条底线：只复制不删，新位置已有的一概不覆盖，每个工作区只搬一次。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from config_layout import ConfigConflict  # noqa: E402

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="Windows 上没有 POSIX 权限位")


def _write(path: Path, text: str, *, newline: str = "\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def _can_symlink(tmp_path: Path) -> bool:
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时代码目录：defaults/ 和仓库工具目录都在它底下，真仓库那几份一个字节都不碰。"""
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
def no_legacy_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认不去问 git：搬家用例只关心搬了什么，仓库里当下有哪些未跟踪文件跟它无关。"""
    monkeypatch.setattr(config_layout, "_untracked_bundled_tools", lambda report: [])


class TestWhereThingsLive:
    @pytest.mark.parametrize(("fn", "rel"), [
        (config_layout.models_path, "config/models.yaml"),
        (config_layout.policy_path, "config/policy.yaml"),
        (config_layout.mcp_path, "config/mcp.yaml"),
        (config_layout.runtime_path, "config/runtime.yaml"),
        (config_layout.nodes_dir, "config/nodes"),
        (config_layout.skills_dir, "config/skills"),
        (config_layout.custom_tools_dir, "config/tools"),
        (config_layout.drawtools_dir, "config/drawtools"),
        (config_layout.platform_secrets_path, "data/platform_secrets.yaml"),
    ])
    def test_each_file_sits_where_the_layout_says(self, ws: Path, fn: Any, rel: str) -> None:
        assert fn(ws) == ws / rel

    def test_the_bundled_defaults_follow_the_code_directory(self, code_root: Path) -> None:
        assert config_layout.defaults_dir() == code_root / "defaults"
        assert config_layout.default_nodes_dir() == code_root / "defaults" / "nodes"
        assert config_layout.default_runtime_path() == code_root / "defaults" / "runtime.yaml"
        assert config_layout.default_template("policy.example.yaml") == code_root / "defaults" / "policy.example.yaml"

    def test_the_instance_tools_dir_is_reachable_from_the_workspace(self, ws: Path, code_root: Path) -> None:
        # 多开时工作区那个 tools 只是软链回代码目录，写出来的路径得相对工作区。
        assert config_layout.workspace_tools_dir(ws) == ws / "tools"
        assert config_layout.bundled_tools_dir() == code_root / "tools"

    @pytest.mark.parametrize(("name", "expected"), [
        ("settings.yaml", "settings.example.yaml"),
        ("prompts/a.md", "prompts/a.example.md"),
        ("prompts/novelai/top-system.md", "prompts/novelai/top-system.example.md"),
        ("NOTES", "NOTES.example"),
    ])
    def test_the_template_name_keeps_the_suffix_last(self, name: str, expected: str) -> None:
        assert config_layout.example_name(name) == expected

    @pytest.mark.parametrize("rel", [
        "config/models.yaml", "config/policy.yaml", "config/mcp.yaml",
        "data/platform_secrets.yaml", "config/drawtools/settings.yaml",
    ])
    def test_files_holding_plaintext_keys_are_owner_only(self, ws: Path, rel: str) -> None:
        assert config_layout.private_mode_for(ws, ws / rel) == 0o600

    @pytest.mark.parametrize("rel", ["config/runtime.yaml", "config/nodes/a.yaml", "data/events.jsonl"])
    def test_everything_else_is_left_to_the_umask(self, ws: Path, rel: str) -> None:
        assert config_layout.private_mode_for(ws, ws / rel) is None

    def test_a_path_outside_the_workspace_gets_no_mode(self, ws: Path, tmp_path: Path) -> None:
        assert config_layout.private_mode_for(ws, tmp_path / "elsewhere" / "models.yaml") is None

    def test_a_workspace_file_is_named_relative_to_the_workspace(self, ws: Path) -> None:
        assert config_layout.logical_path(ws, ws / "config" / "nodes" / "a.yaml") == "config/nodes/a.yaml"

    def test_a_bundled_default_is_named_relative_to_the_code_directory(self, ws: Path, code_root: Path) -> None:
        # 多开实例的工作区里根本没有 defaults/，按代码目录里的相对路径叫，policy 也认这个名字。
        path = config_layout.default_nodes_dir() / "chat.intent.yaml"

        assert config_layout.logical_path(ws, path) == "defaults/nodes/chat.intent.yaml"

    def test_a_path_in_neither_place_keeps_its_own_spelling(self, ws: Path, code_root: Path, tmp_path: Path) -> None:
        path = tmp_path / "outside" / "x.yaml"

        assert config_layout.logical_path(ws, path) == path.as_posix()

    def test_nodes_are_looked_up_system_then_instance_then_default(self, ws: Path, code_root: Path) -> None:
        assert config_layout.node_search_dirs(ws) == (
            ws / "engine" / "system_nodes", ws / "config" / "nodes", code_root / "defaults" / "nodes",
        )

    def test_runtime_has_the_default_underneath_and_the_instance_on_top(self, ws: Path, code_root: Path) -> None:
        assert config_layout.runtime_layers(ws) == (
            code_root / "defaults" / "runtime.yaml", ws / "config" / "runtime.yaml",
        )

    def test_editing_a_bundled_node_lands_in_the_instance_directory(self, ws: Path, code_root: Path) -> None:
        default = config_layout.default_nodes_dir() / "chat.intent.yaml"

        assert config_layout.is_default_node(default) is True
        assert config_layout.node_edit_target(ws, default) == ws / "config" / "nodes" / "chat.intent.yaml"

    def test_editing_an_instance_node_stays_where_it_is(self, ws: Path, code_root: Path) -> None:
        own = config_layout.nodes_dir(ws) / "own.yaml"

        assert config_layout.is_default_node(own) is False
        assert config_layout.node_edit_target(ws, own) == own


class TestFragmentLookup:
    """节点里 {{include:名字}} 引到哪一份。"""

    def test_the_instance_fragment_wins(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.nodes_dir(ws) / "_shared.md", "own")
        _write(config_layout.default_nodes_dir() / "_shared.md", "default")

        assert config_layout.fragment_file(ws, "_shared.md").read_text(encoding="utf-8") == "own"

    def test_the_bundled_default_is_used_when_the_instance_has_none(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_shared.md", "default")

        assert config_layout.fragment_file(ws, "_shared.md") == config_layout.default_nodes_dir() / "_shared.md"

    def test_the_instance_template_comes_before_the_bundled_template(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.nodes_dir(ws) / "_persona.example.md", "own template")
        _write(config_layout.default_nodes_dir() / "_persona.example.md", "default template")

        found = config_layout.fragment_file(ws, "_persona.md")

        assert found == config_layout.nodes_dir(ws) / "_persona.example.md"

    def test_the_bundled_template_is_the_last_resort(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.default_nodes_dir() / "_persona.example.md", "default template")

        assert config_layout.fragment_file(ws, "_persona.md") == config_layout.default_nodes_dir() / "_persona.example.md"

    def test_nothing_anywhere_is_none(self, ws: Path, code_root: Path) -> None:
        assert config_layout.fragment_file(ws, "_missing.md") is None

    def test_a_name_climbing_out_of_the_directory_is_refused(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.config_dir(ws) / "secrets.md", "leak")
        config_layout.nodes_dir(ws).mkdir(parents=True, exist_ok=True)

        assert config_layout.fragment_file(ws, "../secrets.md") is None

    def test_a_symlink_pointing_out_of_the_directory_is_ignored(self, ws: Path, code_root: Path, tmp_path: Path) -> None:
        if not _can_symlink(tmp_path):
            pytest.skip("本机不允许建软链")
        outside = _write(tmp_path / "outside" / "secrets.md", "leak")
        config_layout.nodes_dir(ws).mkdir(parents=True, exist_ok=True)
        (config_layout.nodes_dir(ws) / "_shared.md").symlink_to(outside)

        assert config_layout.fragment_file(ws, "_shared.md") is None


class TestDrawtoolsLookup:
    def test_the_instance_file_wins_over_the_bundled_template(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_file(ws, "settings.yaml"), "own: 1\n")
        _write(config_layout.drawtools_code_dir() / "settings.example.yaml", "default: 1\n")

        assert config_layout.drawtools_effective(ws, "settings.yaml") == config_layout.drawtools_file(ws, "settings.yaml")

    def test_the_template_is_read_when_the_instance_has_none(self, ws: Path, code_root: Path) -> None:
        example = _write(config_layout.drawtools_code_dir() / "settings.example.yaml", "default: 1\n")

        assert config_layout.drawtools_effective(ws, "settings.yaml") == example

    def test_with_neither_it_points_at_where_the_instance_file_would_go(self, ws: Path, code_root: Path) -> None:
        assert config_layout.drawtools_effective(ws, "settings.yaml") == config_layout.drawtools_file(ws, "settings.yaml")

    def test_missing_configs_are_copied_from_the_templates(self, ws: Path, code_root: Path) -> None:
        for name in config_layout.DRAWTOOLS_FILES:
            _write(config_layout.drawtools_code_dir() / config_layout.example_name(name), f"# {name}\n")

        created = config_layout.ensure_drawtools_configs(ws)

        assert created == [f"config/drawtools/{name}" for name in config_layout.DRAWTOOLS_FILES]
        assert config_layout.drawtools_file(ws, "settings.yaml").read_text(encoding="utf-8") == "# settings.yaml\n"

    def test_an_existing_config_is_never_overwritten(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_code_dir() / "settings.example.yaml", "from: template\n")
        own = _write(config_layout.drawtools_file(ws, "settings.yaml"), "from: operator\n")

        created = config_layout.ensure_drawtools_configs(ws)

        assert created == []
        assert own.read_text(encoding="utf-8") == "from: operator\n"

    @_POSIX_ONLY
    def test_the_copied_settings_file_is_owner_only(self, ws: Path, code_root: Path) -> None:
        # 里面是 NovelAI 的 api key，同机其他用户没有理由读到。
        _write(config_layout.drawtools_code_dir() / "settings.example.yaml", "api: {}\n")

        config_layout.ensure_drawtools_configs(ws)

        assert config_layout.drawtools_file(ws, "settings.yaml").stat().st_mode & 0o777 == 0o600


class TestWritingAConfig:
    def test_a_new_file_is_written_and_its_fingerprint_returned(self, ws: Path) -> None:
        target = ws / "config" / "models.yaml"

        digest = config_layout.write_config_text(target, "version: 1\n")

        assert target.read_bytes() == b"version: 1\n"
        assert digest == config_layout.file_hash(target)
        assert digest == config_layout.bytes_hash(b"version: 1\n")

    def test_a_crlf_file_stays_crlf(self, ws: Path) -> None:
        # 只改一行却让整份文件换行全变，diff 里看不出人到底改了什么。
        target = _write(ws / "config" / "models.yaml", "version: 1\nkeep: me\n", newline="\r\n")

        config_layout.write_config_text(target, "version: 2\nkeep: me\n")

        assert target.read_bytes() == b"version: 2\r\nkeep: me\r\n"

    def test_an_lf_file_stays_lf_even_when_crlf_is_handed_in(self, ws: Path) -> None:
        target = _write(ws / "config" / "models.yaml", "version: 1\n")

        config_layout.write_config_text(target, "version: 2\r\nkeep: me\r\n")

        assert target.read_bytes() == b"version: 2\nkeep: me\n"

    def test_a_stale_fingerprint_writes_nothing(self, ws: Path) -> None:
        target = _write(ws / "config" / "models.yaml", "version: 1\n")

        with pytest.raises(ConfigConflict):
            config_layout.write_config_text(target, "version: 2\n", base_hash="0" * 64)

        assert target.read_bytes() == b"version: 1\n"

    def test_the_fingerprint_taken_when_the_editor_opened_still_writes(self, ws: Path) -> None:
        target = _write(ws / "config" / "models.yaml", "version: 1\n")

        config_layout.write_config_text(target, "version: 2\n", base_hash=config_layout.file_hash(target))

        assert target.read_bytes() == b"version: 2\n"

    def test_an_empty_fingerprint_means_the_file_must_not_exist_yet(self, ws: Path) -> None:
        target = _write(ws / "config" / "models.yaml", "version: 1\n")

        with pytest.raises(ConfigConflict):
            config_layout.write_config_text(target, "version: 2\n", base_hash="")

        assert target.read_bytes() == b"version: 1\n"

    def test_an_empty_fingerprint_creates_a_file_that_is_not_there(self, ws: Path) -> None:
        target = ws / "config" / "models.yaml"

        config_layout.write_config_text(target, "version: 1\n", base_hash="")

        assert target.read_bytes() == b"version: 1\n"

    def test_a_backup_holds_the_bytes_that_were_overwritten(self, ws: Path) -> None:
        target = _write(ws / "config" / "models.yaml", "version: 1\n", newline="\r\n")

        config_layout.write_config_text(target, "version: 2\n", backup=True)

        assert target.with_name("models.yaml.bak").read_bytes() == b"version: 1\r\n"

    def test_a_first_write_leaves_no_backup(self, ws: Path) -> None:
        target = ws / "config" / "models.yaml"

        config_layout.write_config_text(target, "version: 1\n", backup=True)

        assert not target.with_name("models.yaml.bak").exists()

    def test_no_temporary_file_is_left_behind(self, ws: Path) -> None:
        target = ws / "config" / "models.yaml"

        config_layout.write_config_text(target, "version: 1\n")

        assert sorted(p.name for p in target.parent.iterdir()) == [".models.yaml.lock", "models.yaml"]

    def test_the_replace_is_atomic(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # bot 随时在读，直接覆写会让它读到半份配置 —— 那等于整份配置塌掉。
        replaced: list[str] = []
        real_replace = os.replace

        def spy(src: Any, dst: Any) -> Any:
            replaced.append(str(dst))
            return real_replace(src, dst)

        monkeypatch.setattr(config_layout.os, "replace", spy)
        target = ws / "config" / "models.yaml"

        config_layout.write_config_text(target, "version: 1\n")

        assert replaced == [str(target)]

    def test_the_lock_file_sits_beside_the_config(self, ws: Path) -> None:
        # 共享组里几个实例改同一份文件，拿的必须是同一把锁。
        target = ws / "config" / "models.yaml"

        config_layout.write_config_text(target, "version: 1\n")

        assert (target.parent / ".models.yaml.lock").is_file()

    def test_a_caller_already_holding_the_lock_uses_write_locked(self, ws: Path) -> None:
        # 同一线程再取一次同一把锁会死锁，所以持锁的调用方有自己的入口。
        target = ws / "config" / "models.yaml"

        with config_layout.locked(target):
            digest = config_layout.write_locked(target, "version: 1\n")

        assert target.read_bytes() == b"version: 1\n"
        assert digest == config_layout.file_hash(target)

    @_POSIX_ONLY
    def test_a_new_secret_file_gets_the_mode_it_was_given(self, ws: Path) -> None:
        target = ws / "config" / "models.yaml"

        config_layout.write_config_text(target, "version: 1\n", mode=0o600)

        assert target.stat().st_mode & 0o777 == 0o600

    @_POSIX_ONLY
    def test_an_existing_file_keeps_the_mode_the_operator_set(self, ws: Path) -> None:
        target = _write(ws / "config" / "models.yaml", "version: 1\n")
        os.chmod(target, 0o640)

        config_layout.write_config_text(target, "version: 2\n", mode=0o600)

        assert target.stat().st_mode & 0o777 == 0o640

    def test_a_missing_file_hash_is_the_empty_string(self, ws: Path) -> None:
        assert config_layout.file_hash(ws / "config" / "nope.yaml") == ""


class TestRemovingAConfig:
    def test_removing_reports_that_it_removed(self, ws: Path) -> None:
        target = _write(ws / "config" / "nodes" / "own.yaml", "id: own\n")

        assert config_layout.remove_config_file(target) is True
        assert not target.exists()

    def test_removing_something_that_is_not_there_is_not_an_error(self, ws: Path) -> None:
        assert config_layout.remove_config_file(ws / "config" / "nodes" / "nope.yaml") is False

    def test_a_stale_fingerprint_keeps_the_file(self, ws: Path) -> None:
        target = _write(ws / "config" / "nodes" / "own.yaml", "id: own\n")

        with pytest.raises(ConfigConflict):
            config_layout.remove_config_file(target, base_hash="0" * 64)

        assert target.is_file()


class TestSplittingOutPlatformSecrets:
    def test_a_document_without_the_section_comes_back_untouched(self) -> None:
        text = "version: 1\nopenai:\n  api_key: k\n"

        assert config_layout.split_platform_secrets(text) == (text, None)

    def test_the_section_is_lifted_out_and_the_rest_kept_verbatim(self) -> None:
        text = (
            "version: 1\n"
            "# 模型渠道\n"
            "openai:\n"
            "  api_key: k\n"
            "\n"
            "platforms:\n"
            "  qq:\n"
            "    access_token: t\n"
            "\n"
            "# 下一段\n"
            "other:\n"
            "  a: 1\n"
        )

        remaining, section = config_layout.split_platform_secrets(text)

        assert section == {"qq": {"access_token": "t"}}
        assert remaining == (
            "version: 1\n"
            "# 模型渠道\n"
            "openai:\n"
            "  api_key: k\n"
            "\n"
            "# 下一段\n"
            "other:\n"
            "  a: 1\n"
        )

    def test_the_comment_written_for_the_next_section_stays_with_it(self) -> None:
        text = "platforms:\n  qq:\n    access_token: t\n# 给 openai 的说明\nopenai:\n  api_key: k\n"

        remaining, _section = config_layout.split_platform_secrets(text)

        assert remaining == "# 给 openai 的说明\nopenai:\n  api_key: k\n"

    def test_the_section_at_the_end_of_the_file_is_removed_whole(self) -> None:
        text = "version: 1\nplatforms:\n  qq:\n    access_token: t\n"

        remaining, section = config_layout.split_platform_secrets(text)

        assert remaining == "version: 1\n"
        assert section == {"qq": {"access_token": "t"}}

    def test_a_section_that_is_not_a_mapping_yields_nothing_to_move(self) -> None:
        remaining, section = config_layout.split_platform_secrets("version: 1\nplatforms: []\n")

        assert section is None
        assert yaml.safe_load(remaining) == {"version": 1}

    def test_broken_yaml_is_handed_back_as_is(self) -> None:
        text = "platforms: [oops\n"

        assert config_layout.split_platform_secrets(text) == (text, None)

    def test_a_document_that_no_longer_reparses_falls_back_to_re_serializing(self) -> None:
        # 同一个顶层键写了两遍：按行摘掉第一段之后，剩下的原文和摘之前少一段对不上。
        text = (
            "platforms:\n  qq:\n    access_token: first\n"
            "other: 1\n"
            "platforms:\n  qq:\n    access_token: second\n"
        )

        remaining, section = config_layout.split_platform_secrets(text)

        assert section == {"qq": {"access_token": "second"}}
        assert yaml.safe_load(remaining) == {"other": 1}
        assert "platforms" not in remaining


@pytest.mark.usefixtures("no_legacy_tools")
class TestTheOneTimeMigration:
    def _legacy(self, ws: Path, *, models: str = "", policy: str = "", mcp: str = "") -> None:
        (ws / "data").mkdir(parents=True, exist_ok=True)
        if models:
            _write(ws / config_layout.LEGACY_MODELS_FILE, models)
        if policy:
            _write(ws / config_layout.LEGACY_POLICY_FILE, policy)
        if mcp:
            _write(ws / config_layout.LEGACY_MCP_FILE, mcp)

    def test_a_fresh_workspace_is_marked_as_migrated(self, ws: Path, code_root: Path) -> None:
        report = config_layout.ensure_layout(ws)

        marker = json.loads((ws / "data" / ".config_layout.json").read_text(encoding="utf-8"))
        assert report.skipped is False
        assert marker["version"] == config_layout.LAYOUT_VERSION
        assert marker["migrated_at"]
        assert config_layout.config_dir(ws).is_dir()

    def test_the_second_start_does_not_migrate_again(self, ws: Path, code_root: Path) -> None:
        config_layout.ensure_layout(ws)
        before = (ws / "data" / ".config_layout.json").read_bytes()

        report = config_layout.ensure_layout(ws)

        assert report.skipped is True
        assert (ws / "data" / ".config_layout.json").read_bytes() == before

    def test_a_marker_from_a_newer_version_is_left_alone(self, ws: Path, code_root: Path) -> None:
        _write(ws / "data" / ".config_layout.json", json.dumps({"version": config_layout.LAYOUT_VERSION + 1}))
        self._legacy(ws, policy="version: 1\n")

        report = config_layout.ensure_layout(ws)

        assert report.skipped is True
        assert not config_layout.policy_path(ws).exists()

    def test_the_three_yaml_files_move_and_the_old_ones_stay(self, ws: Path, code_root: Path) -> None:
        # 只复制不删：退回旧版本时旧文件还得在。
        self._legacy(ws, models="version: 1\n", policy="version: 1\n", mcp="clients: {}\n")

        config_layout.ensure_layout(ws)

        assert config_layout.models_path(ws).read_text(encoding="utf-8") == "version: 1\n"
        assert config_layout.policy_path(ws).read_text(encoding="utf-8") == "version: 1\n"
        assert config_layout.mcp_path(ws).read_text(encoding="utf-8") == "clients: {}\n"
        assert (ws / config_layout.LEGACY_MODELS_FILE).is_file()

    def test_a_file_already_in_the_new_place_is_never_overwritten(self, ws: Path, code_root: Path) -> None:
        self._legacy(ws, models="from: legacy\n")
        _write(config_layout.models_path(ws), "from: new\n")

        report = config_layout.ensure_layout(ws)

        assert config_layout.models_path(ws).read_text(encoding="utf-8") == "from: new\n"
        assert report.copied == []

    def test_the_crlf_of_the_old_file_survives_the_move(self, ws: Path, code_root: Path) -> None:
        # 摘 platforms 段是按行做的，CRLF 文件在这里最容易被折成 LF。
        _write(
            ws / config_layout.LEGACY_MODELS_FILE,
            "version: 1\nopenai:\n  api_key: k\nplatforms:\n  qq:\n    access_token: t\n",
            newline="\r\n",
        )

        config_layout.ensure_layout(ws)

        assert config_layout.models_path(ws).read_bytes() == b"version: 1\r\nopenai:\r\n  api_key: k\r\n"

    def test_the_platforms_section_lands_in_its_own_file(self, ws: Path, code_root: Path) -> None:
        self._legacy(ws, models="version: 1\nplatforms:\n  qq:\n    access_token: t\n")

        config_layout.ensure_layout(ws)

        assert yaml.safe_load(config_layout.models_path(ws).read_text(encoding="utf-8")) == {"version": 1}
        secrets = yaml.safe_load(config_layout.platform_secrets_path(ws).read_text(encoding="utf-8"))
        assert secrets == {"qq": {"access_token": "t"}}

    @_POSIX_ONLY
    def test_both_moved_secret_files_are_owner_only(self, ws: Path, code_root: Path) -> None:
        self._legacy(ws, models="platforms:\n  qq:\n    access_token: t\n")

        config_layout.ensure_layout(ws)

        assert config_layout.models_path(ws).stat().st_mode & 0o777 == 0o600
        assert config_layout.platform_secrets_path(ws).stat().st_mode & 0o777 == 0o600

    def test_an_existing_secrets_file_is_reported_instead_of_overwritten(self, ws: Path, code_root: Path) -> None:
        self._legacy(ws, models="platforms:\n  qq:\n    access_token: from-legacy\n")
        _write(config_layout.platform_secrets_path(ws), "qq:\n  access_token: already-here\n")

        report = config_layout.ensure_layout(ws)

        assert config_layout.platform_secrets_path(ws).read_text(encoding="utf-8") == "qq:\n  access_token: already-here\n"
        assert any(config_layout.PLATFORM_SECRETS_FILE in line for line in report.warnings)
        assert config_layout.layout_status(ws)["warnings"] == report.warnings

    def test_the_skills_tree_moves_file_by_file(self, ws: Path, code_root: Path) -> None:
        _write(ws / "skills" / "alpha" / "SKILL.md", "---\nname: alpha\n---\nbody\n")
        _write(ws / "skills" / "alpha" / "notes" / "extra.md", "extra\n")
        _write(ws / "skills" / "__pycache__" / "junk.pyc", "x")

        config_layout.ensure_layout(ws)

        assert (config_layout.skills_dir(ws) / "alpha" / "SKILL.md").is_file()
        assert (config_layout.skills_dir(ws) / "alpha" / "notes" / "extra.md").is_file()
        assert not (config_layout.skills_dir(ws) / "__pycache__").exists()

    def test_the_drawtools_runtime_files_come_from_the_code_directory(self, ws: Path, code_root: Path) -> None:
        _write(config_layout.drawtools_code_dir() / "settings.yaml", "api: {}\n")
        _write(config_layout.drawtools_code_dir() / "prompts" / "novelai" / "tag-guide.md", "guide\n")

        config_layout.ensure_layout(ws)

        assert config_layout.drawtools_file(ws, "settings.yaml").read_text(encoding="utf-8") == "api: {}\n"
        assert config_layout.drawtools_file(ws, "prompts/novelai/tag-guide.md").read_text(encoding="utf-8") == "guide\n"

    def test_the_bundled_templates_are_not_mistaken_for_runtime_files(self, ws: Path, code_root: Path) -> None:
        # 仓库只发 .example，搬家搬的是同名的实际文件，没有就什么也不搬。
        _write(config_layout.drawtools_code_dir() / "settings.example.yaml", "api: {}\n")

        config_layout.ensure_layout(ws)

        assert not config_layout.drawtools_file(ws, "settings.yaml").exists()


class TestCustomToolsLeftInTheCodeDirectory:
    def _found(self, monkeypatch: pytest.MonkeyPatch, *rels: str) -> None:
        def fake(report: Any) -> list[Path]:
            return [config_layout.bundled_tools_dir() / rel for rel in rels]

        monkeypatch.setattr(config_layout, "_untracked_bundled_tools", fake)

    def test_a_workspace_from_before_the_move_inherits_them(
        self, ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(config_layout.bundled_tools_dir() / "own_tool.py", "SPEC = {}\n")
        _write(ws / config_layout.LEGACY_POLICY_FILE, "version: 1\n")
        self._found(monkeypatch, "own_tool.py")

        config_layout.ensure_layout(ws)

        assert (config_layout.custom_tools_dir(ws) / "own_tool.py").read_text(encoding="utf-8") == "SPEC = {}\n"

    def test_a_workspace_created_afterwards_starts_isolated(
        self, ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 新实例不继承别人加的工具，但仍要记下它们，免得被当成仓库自带的。
        _write(config_layout.bundled_tools_dir() / "own_tool.py", "SPEC = {}\n")
        self._found(monkeypatch, "own_tool.py")

        config_layout.ensure_layout(ws)

        assert not (config_layout.custom_tools_dir(ws) / "own_tool.py").exists()
        assert config_layout.legacy_tool_rels(ws) == frozenset({"own_tool.py"})

    def test_they_are_recorded_in_the_marker_with_their_relative_path(
        self, ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for rel in ("own_tool.py", "pack/entry.py"):
            _write(config_layout.bundled_tools_dir() / rel, "SPEC = {}\n")
        self._found(monkeypatch, "own_tool.py", "pack/entry.py")

        report = config_layout.ensure_layout(ws)

        assert report.legacy_tools == ["own_tool.py", "pack/entry.py"]
        assert config_layout.legacy_tool_rels(ws) == frozenset({"own_tool.py", "pack/entry.py"})

    def test_a_workspace_that_never_migrated_knows_of_none(self, ws: Path, code_root: Path) -> None:
        assert config_layout.legacy_tool_rels(ws) == frozenset()

    def test_a_git_that_will_not_answer_is_reported_and_nothing_is_copied(
        self, ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(config_layout.bundled_tools_dir() / "own_tool.py", "SPEC = {}\n")
        _write(ws / config_layout.LEGACY_POLICY_FILE, "version: 1\n")
        monkeypatch.setattr(
            config_layout.subprocess, "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args, 128, b"", b"not a git repository"),
        )

        report = config_layout.ensure_layout(ws)

        assert any("not a git repository" in line for line in report.warnings)
        assert not config_layout.custom_tools_dir(ws).exists()
        assert report.legacy_tools == []

    def test_git_blowing_up_is_reported_too(
        self, ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("git not found")

        monkeypatch.setattr(config_layout.subprocess, "run", boom)

        report = config_layout.ensure_layout(ws)

        assert any("git not found" in line for line in report.warnings)

    def test_only_real_tool_entry_scripts_count(
        self, ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for rel in ("own_tool.py", "__init__.py", "pack/__init__.py", "notes.txt"):
            _write(config_layout.bundled_tools_dir() / rel, "x\n")
        listing = b"\0".join(
            f"tools/{rel}".encode("utf-8") for rel in ("own_tool.py", "__init__.py", "pack/__init__.py", "notes.txt")
        )
        monkeypatch.setattr(
            config_layout.subprocess, "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, listing, b""),
        )

        report = config_layout.ensure_layout(ws)

        assert report.legacy_tools == ["own_tool.py"]


@pytest.mark.usefixtures("no_legacy_tools")
class TestLayoutStatus:
    def test_a_workspace_that_never_started_reads_as_not_migrated(self, ws: Path, code_root: Path) -> None:
        status = config_layout.layout_status(ws)

        assert status["migrated"] is False
        assert status["stale_legacy"] == []

    def test_after_the_migration_it_reads_as_migrated(self, ws: Path, code_root: Path) -> None:
        config_layout.ensure_layout(ws)

        status = config_layout.layout_status(ws)

        assert status["migrated"] is True
        assert status["migrated_at"]

    def test_an_old_file_touched_afterwards_is_called_out(self, ws: Path, code_root: Path) -> None:
        # 旧位置比新位置新，多半是退回旧版本那阵子改的，那些改动现在不生效。
        legacy = _write(ws / config_layout.LEGACY_POLICY_FILE, "version: 1\n")
        config_layout.ensure_layout(ws)
        later = time.time() + 120
        os.utime(legacy, (later, later))

        assert config_layout.layout_status(ws)["stale_legacy"] == ["data/policy.yaml 比 config/policy.yaml 新"]

    def test_a_one_second_difference_is_not_worth_reporting(self, ws: Path, code_root: Path) -> None:
        # 复制本身就会让两份文件差出零点几秒。
        legacy = _write(ws / config_layout.LEGACY_POLICY_FILE, "version: 1\n")
        config_layout.ensure_layout(ws)
        stat = config_layout.policy_path(ws).stat()
        os.utime(legacy, (stat.st_atime + 0.5, stat.st_mtime + 0.5))

        assert config_layout.layout_status(ws)["stale_legacy"] == []
