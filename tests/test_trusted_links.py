"""工作区里通到外面去的链接：哪几个算数，经过它们的路径在工作区里叫什么名字。

多开实例的 adapters/engine/… 链回代码目录，表情包库和共享组的 config/ 链到数据根下。判路径时这几处要算工作区里面，
否则 policy 把自家配置当外部路径、表情包一张都发不出去；反过来，工作区里随手建一个链接就能把任意目录变成工作区，
所以只认链接名对得上、指向也对得上的那几个。
"""
from __future__ import annotations

import base64
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
import engine.attachments as attachments  # noqa: E402
from _onebot_harness import body_attr, load_runtime  # noqa: E402
from platforms import protection  # noqa: E402
from supervisor import share_groups  # noqa: E402
from supervisor.policy import PolicyEngine, SafetyLevel  # noqa: E402
from supervisor.share_groups import _make_dir_link  # noqa: E402
from toolbox.builtins.search_in_files import _collect_files  # noqa: E402

# 1x1 的真 PNG：附件出口会真的解码压一遍图，随手编的字节过不去。
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _link(link: Path, target: Path) -> None:
    """建一个目录链接。Windows 上普通账号建不了软链，退到目录联接，判定和读写都一样。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    _make_dir_link(link, target)


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """多开部署的数据根：清单文件在这儿，共享表情包库和共享组都在它下面。"""
    root = tmp_path / "kazebot-data"
    root.mkdir()
    monkeypatch.setenv(config_layout.INSTANCES_FILE_ENV, str(root / "instances.yaml"))
    return root


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "code"
    for name in config_layout.CODE_LINKS:
        (root / name).mkdir(parents=True)
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def ws(data_root: Path) -> Path:
    root = data_root / "alpha"
    (root / "data").mkdir(parents=True)
    (root / "config").mkdir()
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    root = tmp_path / "outside"
    root.mkdir()
    (root / "x.py").write_text("x = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def group_config(data_root: Path, ws: Path) -> Path:
    """本实例的 config/ 换成指向共享组 team 的链接。"""
    target = data_root / config_layout.SHARED_DIR / "team" / config_layout.CONFIG_DIR
    target.mkdir(parents=True)
    (ws / "config").rmdir()
    _link(ws / "config", target)
    return target


class TestIsDirLink:
    def test_a_real_directory_is_not_a_link(self, ws: Path) -> None:
        assert config_layout.is_dir_link(ws / "data") is False

    def test_a_missing_path_is_not_a_link(self, ws: Path) -> None:
        assert config_layout.is_dir_link(ws / "nope") is False

    def test_a_regular_file_is_not_a_link(self, ws: Path) -> None:
        (ws / "data" / "f.txt").write_text("x", encoding="utf-8")

        assert config_layout.is_dir_link(ws / "data" / "f.txt") is False

    def test_layout_checks_work_without_windows_reparse_constants(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        file = ws / "data" / "f.txt"
        file.write_text("x", encoding="utf-8")
        monkeypatch.setattr(config_layout, "stat", SimpleNamespace(S_ISLNK=stat.S_ISLNK))

        assert config_layout.is_dir_link(ws / "config") is False
        assert config_layout.is_dir_link(file) is False
        config_layout.check_config_dir(ws)

    @pytest.mark.parametrize("tag, expected", [(0xA0000003, True), (0, False), (None, False)])
    def test_only_the_junction_reparse_tag_marks_a_directory_link(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch, tag: int | None, expected: bool,
    ) -> None:
        info = SimpleNamespace(st_mode=stat.S_IFDIR)
        if tag is not None:
            info.st_reparse_tag = tag
        monkeypatch.setattr(config_layout, "os", SimpleNamespace(lstat=lambda path: info))
        monkeypatch.setattr(config_layout, "stat", SimpleNamespace(
            S_ISLNK=stat.S_ISLNK, IO_REPARSE_TAG_MOUNT_POINT=0xA0000003,
        ))

        assert config_layout.is_dir_link(ws / "config") is expected

    def test_a_directory_link_is_one(self, ws: Path, outside: Path) -> None:
        _link(ws / "data" / "mine", outside)

        assert config_layout.is_dir_link(ws / "data" / "mine") is True

    def test_a_link_whose_target_is_gone_is_still_a_link(self, ws: Path, tmp_path: Path) -> None:
        # 断链要认得出来，才能停下报错而不是在链接旁边另起一份空配置。
        gone = tmp_path / "gone"
        gone.mkdir()
        _link(ws / "data" / "dangling", gone)
        gone.rmdir()

        assert config_layout.is_dir_link(ws / "data" / "dangling") is True


class TestTrustedLinks:
    def test_the_code_directories_and_the_sticker_library_are_trusted(
        self, ws: Path, code_root: Path,
    ) -> None:
        names = [name for name, _ in config_layout.trusted_links(ws)]

        assert names == [*config_layout.CODE_LINKS, config_layout.STICKERS_LINK]

    def test_each_code_link_must_point_at_the_directory_of_the_same_name(
        self, ws: Path, code_root: Path,
    ) -> None:
        expected = dict(config_layout.trusted_links(ws))

        assert expected["engine"] == code_root / "engine"
        assert expected["defaults"] == code_root / "defaults"

    def test_a_single_instance_deployment_has_no_sticker_link(
        self, tmp_path: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(config_layout.INSTANCES_FILE_ENV, raising=False)
        root = tmp_path / "solo"
        (root / "config").mkdir(parents=True)

        names = [name for name, _ in config_layout.trusted_links(root)]

        assert names == list(config_layout.CODE_LINKS)

    def test_the_sticker_library_sits_under_the_data_root(
        self, ws: Path, code_root: Path, data_root: Path,
    ) -> None:
        expected = dict(config_layout.trusted_links(ws))

        assert expected[config_layout.STICKERS_LINK] == data_root / "stickers"

    def test_a_grouped_config_adds_the_group_directory(
        self, ws: Path, code_root: Path, group_config: Path,
    ) -> None:
        expected = dict(config_layout.trusted_links(ws))

        assert expected[config_layout.CONFIG_DIR] == group_config


class TestWhatAPathIsCalledInTheWorkspace:
    def test_a_plain_file_keeps_its_relative_name(self, ws: Path, code_root: Path) -> None:
        assert config_layout.workspace_logical_path(ws, ws / "data" / "a.png") == "data/a.png"

    def test_the_workspace_root_itself_is_the_current_directory(self, ws: Path, code_root: Path) -> None:
        assert config_layout.workspace_logical_path(ws, ws.resolve()) == "."

    def test_a_file_reached_through_a_code_link_keeps_the_link_name(
        self, ws: Path, code_root: Path,
    ) -> None:
        _link(ws / "engine", code_root / "engine")

        assert config_layout.workspace_logical_path(ws, code_root / "engine" / "runner.py") == "engine/runner.py"

    def test_the_link_target_itself_is_named_after_the_link(self, ws: Path, code_root: Path) -> None:
        _link(ws / "tools", code_root / "tools")

        assert config_layout.workspace_logical_path(ws, (code_root / "tools").resolve()) == "tools"

    def test_a_code_link_pointing_somewhere_else_is_not_trusted(
        self, ws: Path, code_root: Path, outside: Path,
    ) -> None:
        # 改一个链接的指向就能让任意目录变成 engine/，判定必须连指向一起对。
        _link(ws / "engine", outside)

        assert config_layout.workspace_logical_path(ws, outside / "x.py") is None

    def test_a_link_nobody_declared_is_not_trusted(self, ws: Path, code_root: Path, outside: Path) -> None:
        _link(ws / "data" / "mine", outside)

        assert config_layout.workspace_logical_path(ws, outside / "x.py") is None

    def test_a_sticker_reached_through_the_shared_library_stays_under_data(
        self, ws: Path, code_root: Path, data_root: Path,
    ) -> None:
        library = data_root / "stickers"
        library.mkdir()
        _link(ws / config_layout.STICKERS_LINK, library)

        assert config_layout.workspace_logical_path(ws, library / "a.png") == "data/stickers/a.png"

    def test_a_grouped_config_file_is_still_called_config(
        self, ws: Path, code_root: Path, group_config: Path,
    ) -> None:
        assert config_layout.workspace_logical_path(ws, group_config / "models.yaml") == "config/models.yaml"

    def test_a_path_in_neither_place_has_no_name(self, ws: Path, code_root: Path, outside: Path) -> None:
        assert config_layout.workspace_logical_path(ws, outside / "x.py") is None


class TestWhichGroupTheConfigBelongsTo:
    def test_a_real_config_directory_is_in_no_group(self, ws: Path) -> None:
        assert config_layout.config_share_group(ws) is None

    def test_a_link_into_the_shared_directory_names_the_group(self, ws: Path, group_config: Path) -> None:
        assert config_layout.config_share_group(ws) == "team"

    def test_a_link_somewhere_else_names_no_group(self, ws: Path, outside: Path) -> None:
        (ws / "config").rmdir()
        _link(ws / "config", outside)

        assert config_layout.config_share_group(ws) is None

    def test_a_link_to_the_group_directory_itself_names_no_group(self, ws: Path, data_root: Path) -> None:
        # 组目录里还并排放着 group.json，链到它等于把建组信息也当成配置。
        target = data_root / config_layout.SHARED_DIR / "team"
        (target / config_layout.CONFIG_DIR).mkdir(parents=True)
        (ws / "config").rmdir()
        _link(ws / "config", target)

        assert config_layout.config_share_group(ws) is None

    def test_a_single_instance_deployment_is_in_no_group(
        self, ws: Path, group_config: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(config_layout.INSTANCES_FILE_ENV, raising=False)

        assert config_layout.config_share_group(ws) is None


class TestCheckingTheConfigDirectory:
    def test_a_real_directory_passes(self, ws: Path) -> None:
        config_layout.check_config_dir(ws)

    def test_a_missing_directory_passes(self, tmp_path: Path) -> None:
        config_layout.check_config_dir(tmp_path / "fresh")

    def test_a_live_group_link_passes(self, ws: Path, group_config: Path) -> None:
        config_layout.check_config_dir(ws)

    def test_a_dangling_group_link_stops_everything(self, ws: Path, group_config: Path) -> None:
        # 接着跑会在链接旁边另起一份空配置，整个组的设置看上去就全没了。
        group_config.rmdir()

        with pytest.raises(config_layout.ConfigLayoutError) as caught:
            config_layout.check_config_dir(ws)

        # 报错要说清怎么救回来，否则第一反应就是把链接删了重建一份空的。
        assert share_groups.ARCHIVE_PREFIX in str(caught.value)

    def test_a_plain_file_in_the_way_stops_everything(self, tmp_path: Path) -> None:
        root = tmp_path / "ws2"
        root.mkdir()
        (root / "config").write_text("oops", encoding="utf-8")

        with pytest.raises(config_layout.ConfigLayoutError):
            config_layout.check_config_dir(root)

    def test_the_startup_migration_refuses_to_run_on_a_dangling_link(
        self, ws: Path, group_config: Path,
    ) -> None:
        group_config.rmdir()

        with pytest.raises(config_layout.ConfigLayoutError):
            config_layout.ensure_layout(ws)


class TestPolicySeesThroughLinks:
    def test_a_tool_script_in_an_instance_workspace_is_internal(self, ws: Path, code_root: Path) -> None:
        _link(ws / "tools", code_root / "tools")
        (code_root / "tools" / "x.py").write_text("x = 1\n", encoding="utf-8")

        resolved, rel, external = clonoth_runtime.classify_path(ws, [], "tools/x.py")

        assert resolved is not None
        assert (rel, external) == ("tools/x.py", False)

    def test_a_grouped_config_file_is_internal_and_keeps_its_name(
        self, ws: Path, code_root: Path, group_config: Path,
    ) -> None:
        (group_config / "models.yaml").write_text("version: 1\n", encoding="utf-8")

        _, rel, external = clonoth_runtime.classify_path(ws, [], "config/models.yaml")

        assert (rel, external) == ("config/models.yaml", False)

    def test_a_link_nobody_declared_still_escapes(self, ws: Path, code_root: Path, outside: Path) -> None:
        _link(ws / "data" / "mine", outside)

        resolved, rel, external = clonoth_runtime.classify_path(ws, [], str(outside / "x.py"))

        assert external is True
        assert resolved is not None and rel == resolved.as_posix()

    def test_a_relative_path_through_an_undeclared_link_is_refused(
        self, ws: Path, code_root: Path, outside: Path,
    ) -> None:
        # 工作区里随手建一个链接就能把任意目录写成相对路径，判定必须在解析之后。
        _link(ws / "data" / "mine", outside)

        assert clonoth_runtime.classify_path(ws, [], "data/mine/x.py") == (
            None, "path escapes workspace root", False,
        )

    def test_a_relative_path_climbing_out_is_still_refused(self, ws: Path, code_root: Path) -> None:
        assert clonoth_runtime.classify_path(ws, [], "../secrets.env") == (
            None, "path escapes workspace root", False,
        )

    @pytest.mark.parametrize("evaluate", ["evaluate_read_file", "evaluate_write_file"])
    def test_a_grouped_models_file_is_not_waved_through(
        self, ws: Path, code_root: Path, group_config: Path, evaluate: str,
    ) -> None:
        # 经链接读到的还是 config/models.yaml，policy 的地板和规则照旧要挡住它。
        (group_config / "models.yaml").write_text("version: 1\n", encoding="utf-8")
        policy = PolicyEngine(workspace_root=ws)

        decision = getattr(policy, evaluate)(path="config/models.yaml")

        assert decision.safety_level is SafetyLevel.approval_required
        # 命中的是写给 config/models.yaml 的那条规则，不是没人管时的默认档。
        assert decision.reason != "default policy" and decision.sensitive is True

    def test_a_file_no_rule_mentions_still_falls_to_the_default(
        self, ws: Path, code_root: Path, group_config: Path,
    ) -> None:
        # 有这一条对照，上面那两条才证明得了规则是真的匹配上了。
        decision = PolicyEngine(workspace_root=ws).evaluate_read_file(path="notes.md")

        assert (decision.reason, decision.sensitive) == ("default policy", False)


class TestWhereAnEnvMovedFileLands:
    def test_a_file_inside_the_workspace_is_relative(self, ws: Path, code_root: Path) -> None:
        assert protection._moved_to("data/other.yaml", ws) == "data/other.yaml"

    def test_a_file_in_a_grouped_config_keeps_the_config_name(
        self, ws: Path, code_root: Path, group_config: Path,
    ) -> None:
        # 判定用的名字和 policy 那边对不上的话，env 一指过去防护就整条落空。
        assert protection._moved_to("config/mcp.yaml", ws) == "config/mcp.yaml"

    def test_a_file_truly_outside_stays_absolute(self, ws: Path, code_root: Path, outside: Path) -> None:
        assert protection._moved_to(str(outside / "x.py"), ws) == (outside / "x.py").resolve().as_posix()


class TestAttachmentsThroughLinks:
    def test_a_sticker_in_the_shared_library_can_be_sent(
        self, ws: Path, code_root: Path, data_root: Path,
    ) -> None:
        library = data_root / "stickers"
        library.mkdir()
        (library / "a.png").write_bytes(_PNG)
        _link(ws / config_layout.STICKERS_LINK, library)

        assert attachments._resolve_file_url("file://data/stickers/a.png", ws) is not None

    def test_a_link_under_data_pointing_at_the_config_is_refused(self, ws: Path, code_root: Path) -> None:
        # 只比名字的话 data/… 这个前缀就够了，解析到底才看得见它其实是 config/models.yaml。
        (ws / "config" / "models.yaml").write_text("version: 1\n", encoding="utf-8")
        _link(ws / "data" / "peek", ws / "config")

        assert attachments._resolve_file_url("file://data/peek/models.yaml", ws) is None

    def test_a_link_under_data_pointing_outside_is_refused(
        self, ws: Path, code_root: Path, outside: Path,
    ) -> None:
        (outside / "a.png").write_bytes(_PNG)
        _link(ws / "data" / "mine", outside)

        assert attachments._resolve_file_url("file://data/mine/a.png", ws) is None


class TestSendingAWorkspaceFileOverQQ:
    @pytest.fixture
    def runtime(self, ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch):
        # 插件实例按工作区目录名进 sys.modules，上个用例留下的同名子模块会被复用，工作区就串过去了。
        stale = f"_onebot_runtime_{ws.name}"
        for name in [key for key in sys.modules if key == stale or key.startswith(f"{stale}.")]:
            del sys.modules[name]
        return load_runtime(monkeypatch, ws)

    def test_a_plain_workspace_file_is_named_relative_to_the_workspace(self, runtime, ws: Path) -> None:
        (ws / "data" / "note.txt").write_text("hi", encoding="utf-8")

        found = body_attr(runtime, "_workspace_file")("data/note.txt")

        assert found is not None and found[1] == "data/note.txt"

    def test_a_file_behind_a_code_link_keeps_the_link_name(self, runtime, ws: Path, code_root: Path) -> None:
        _link(ws / "tools", code_root / "tools")
        (code_root / "tools" / "x.py").write_text("x = 1\n", encoding="utf-8")

        found = body_attr(runtime, "_workspace_file")(str(code_root / "tools" / "x.py"))

        assert found is not None and found[1] == "tools/x.py"

    def test_a_path_outside_the_workspace_is_refused(self, runtime, outside: Path) -> None:
        assert body_attr(runtime, "_workspace_file")(str(outside / "x.py")) is None

    def test_an_empty_path_is_refused(self, runtime) -> None:
        assert body_attr(runtime, "_workspace_file")("  ") is None

    def test_the_send_command_reports_the_logical_path(self, runtime, ws: Path) -> None:
        (ws / "data" / "note.txt").write_text("hi", encoding="utf-8")

        body, error = body_attr(runtime, "_parse_file_send_body")("data/note.txt 说明.txt")

        assert error == ""
        assert body == {"type": "file", "path": "data/note.txt", "name": "说明.txt"}

    def test_a_grouped_secret_file_is_blocked_by_its_logical_name(
        self, runtime, ws: Path, group_config: Path,
    ) -> None:
        # 黑名单按工作区里的名字判，拿真实位置去比的话共享组里的密钥就直接发出去了。
        (group_config / "secrets.env").write_text("ALPHA_KEY=1\n", encoding="utf-8")

        files, errors = body_attr(runtime, "_forward_bridge_resolve_files")(["config/secrets.env"])

        assert files == []
        assert errors and config_layout.SHARED_SECRETS_FILE in errors[0]

    def test_a_grouped_node_file_still_goes_out(self, runtime, ws: Path, group_config: Path) -> None:
        (group_config / "nodes").mkdir()
        (group_config / "nodes" / "main.yaml").write_text("id: main\n", encoding="utf-8")

        files, errors = body_attr(runtime, "_forward_bridge_resolve_files")(["config/nodes/main.yaml"])

        assert errors == []
        assert files == [{"type": "file", "path": "config/nodes/main.yaml", "name": "main.yaml"}]


class TestSearchResultsNameFilesTheWayTheWorkspaceDoes:
    def test_a_file_under_the_workspace_is_relative(self, ws: Path, code_root: Path) -> None:
        (ws / "data" / "a.txt").write_text("needle\n", encoding="utf-8")

        found = list(_collect_files((ws / "data").resolve(), "*.txt", 1 << 20, ws))

        assert [rel for _, rel in found] == ["data/a.txt"]

    def test_a_file_behind_a_code_link_keeps_the_link_name(self, ws: Path, code_root: Path) -> None:
        _link(ws / "tools", code_root / "tools")
        (code_root / "tools" / "x.py").write_text("needle\n", encoding="utf-8")

        found = list(_collect_files((ws / "tools").resolve(), "*.py", 1 << 20, ws))

        assert [rel for _, rel in found] == ["tools/x.py"]

    def test_a_grouped_config_file_keeps_the_config_name(
        self, ws: Path, code_root: Path, group_config: Path,
    ) -> None:
        (group_config / "runtime.yaml").write_text("needle\n", encoding="utf-8")

        found = list(_collect_files((ws / "config").resolve(), "*.yaml", 1 << 20, ws))

        assert [rel for _, rel in found] == ["config/runtime.yaml"]

    def test_a_single_file_root_is_named_too(self, ws: Path, code_root: Path) -> None:
        _link(ws / "tools", code_root / "tools")
        target = code_root / "tools" / "x.py"
        target.write_text("needle\n", encoding="utf-8")

        found = list(_collect_files(target.resolve(), "*", 1 << 20, ws))

        assert [rel for _, rel in found] == ["tools/x.py"]

    def test_a_root_outside_the_workspace_keeps_its_own_spelling(
        self, ws: Path, code_root: Path, outside: Path,
    ) -> None:
        found = list(_collect_files(outside.resolve(), "*.py", 1 << 20, ws))

        assert [rel for _, rel in found] == [(outside / "x.py").resolve().as_posix()]
