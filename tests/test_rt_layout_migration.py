"""旧布局搬家断在半路也能接着搬：平台令牌先落盘，复制走临时文件，做不成的那一步记成待办，下次只做它。

已经有 version=1 标记的老工作区算搬完了，不再重搬。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402

_LEGACY_MODELS = (
    "version: 1\n"
    "provider: openai\n"
    "openai:\n"
    "  api_key: sk-test\n"
    "platforms:\n"
    "  qq:\n"
    "    access_token: secret-token\n"
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _marker(ws: Path) -> dict[str, Any]:
    return json.loads((ws / "data" / ".config_layout.json").read_text(encoding="utf-8"))


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
def git_answers(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.CompletedProcess[bytes]]:
    """排队的 git 回答，问一次取一个；问完了就当没有自己加的工具。"""
    queue: list[subprocess.CompletedProcess[bytes]] = []

    def run(args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return queue.pop(0) if queue else subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(config_layout.subprocess, "run", run)
    return queue


def _git_fails(detail: str = "fatal: unable to read index") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(["git"], 128, b"", detail.encode("utf-8"))


class TestPlatformTokensMoveFirst:
    def test_the_secrets_file_is_written_before_models(
        self, ws: Path, code_root: Path, git_answers: list, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # models.yaml 一在，下次启动就不会回来摘 platforms 段：两步之间断掉的话令牌再也迁不出来。
        _write(ws / config_layout.LEGACY_MODELS_FILE, _LEGACY_MODELS)
        order: list[str] = []
        real = config_layout.write_config_text

        def spy(path: Path, *args: Any, **kwargs: Any) -> str:
            order.append(Path(path).name)
            return real(path, *args, **kwargs)

        monkeypatch.setattr(config_layout, "write_config_text", spy)

        config_layout.ensure_layout(ws)

        assert order.index("platform_secrets.yaml") < order.index("models.yaml")

    def test_a_crash_between_the_two_writes_is_picked_up_on_the_next_start(
        self, ws: Path, code_root: Path, git_answers: list, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(ws / config_layout.LEGACY_MODELS_FILE, _LEGACY_MODELS)
        real = config_layout.write_config_text

        def crash_on_models(path: Path, *args: Any, **kwargs: Any) -> str:
            if Path(path).name == "models.yaml":
                raise OSError("power cut")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(config_layout, "write_config_text", crash_on_models)
        with pytest.raises(OSError):
            config_layout.ensure_layout(ws)
        assert config_layout.platform_secrets_path(ws).is_file()
        assert not (ws / "data" / ".config_layout.json").exists()

        monkeypatch.setattr(config_layout, "write_config_text", real)
        report = config_layout.ensure_layout(ws)

        # 上一次写下的令牌和这次要写的一样，接着搬不算撞车，不该冒出「请手动核对」。
        assert report.warnings == []
        assert yaml.safe_load(config_layout.platform_secrets_path(ws).read_text(encoding="utf-8")) == {
            "qq": {"access_token": "secret-token"},
        }
        assert "platforms" not in yaml.safe_load(config_layout.models_path(ws).read_text(encoding="utf-8"))

    def test_a_different_secrets_file_already_there_is_not_overwritten(
        self, ws: Path, code_root: Path, git_answers: list,
    ) -> None:
        _write(ws / config_layout.LEGACY_MODELS_FILE, _LEGACY_MODELS)
        existing = _write(config_layout.platform_secrets_path(ws), "qq:\n  access_token: newer-token\n")

        report = config_layout.ensure_layout(ws)

        assert existing.read_text(encoding="utf-8") == "qq:\n  access_token: newer-token\n"
        assert any(config_layout.PLATFORM_SECRETS_FILE in line for line in report.warnings)


class TestCopiesAreAtomic:
    def test_a_copy_that_fails_leaves_neither_a_half_file_nor_a_temp_file(
        self, ws: Path, code_root: Path, git_answers: list, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 直接往新位置抄的话，断在半路的半份会被下次启动当成已经搬过，新位置已有的一概不覆盖。
        _write(ws / config_layout.LEGACY_POLICY_FILE, "version: 1\n")
        real = config_layout.os.replace

        def refuse(src: Any, dst: Any, **kwargs: Any) -> None:
            if Path(dst).name == "policy.yaml":
                raise OSError("disk full")
            return real(src, dst, **kwargs)

        monkeypatch.setattr(config_layout.os, "replace", refuse)

        with pytest.raises(OSError):
            config_layout.ensure_layout(ws)

        target = config_layout.policy_path(ws)
        assert not target.exists()
        assert not [entry.name for entry in target.parent.iterdir() if entry.name.endswith(".tmp")]


class TestPendingSteps:
    def test_a_git_checkout_that_cannot_answer_leaves_the_step_pending(
        self, ws: Path, code_root: Path, git_answers: list,
    ) -> None:
        (code_root / ".git").mkdir()
        git_answers.append(_git_fails("fatal: index.lock exists"))

        report = config_layout.ensure_layout(ws)

        assert "custom_tools" in _marker(ws)["pending"]
        assert any("index.lock" in line for line in report.warnings)
        assert config_layout.layout_status(ws)["pending"] == ["custom_tools"]

    def test_the_next_start_only_redoes_the_pending_step(
        self, ws: Path, code_root: Path, git_answers: list,
    ) -> None:
        (code_root / ".git").mkdir()
        git_answers.append(_git_fails("fatal: index.lock exists"))
        config_layout.ensure_layout(ws)
        # 已经做完的步骤不再做：这时候才冒出来的旧 policy 不会被搬。
        _write(ws / config_layout.LEGACY_POLICY_FILE, "version: 1\n")
        _write(code_root / config_layout.LEGACY_TOOLS_DIR / "own_tool.py", "SPEC = {}\n")
        git_answers.append(subprocess.CompletedProcess(["git"], 0, b"tools/own_tool.py\0", b""))

        report = config_layout.ensure_layout(ws)

        assert report.skipped is False
        assert _marker(ws)["pending"] == {}
        assert not any("index.lock" in line for line in _marker(ws)["warnings"])
        assert _marker(ws)["legacy_tools"] == ["own_tool.py"]
        assert not config_layout.policy_path(ws).exists()
        assert config_layout.ensure_layout(ws).skipped is True

    def test_a_tree_that_is_not_a_git_checkout_is_warned_about_once(
        self, ws: Path, code_root: Path, git_answers: list,
    ) -> None:
        # 不是检出的再问也问不出来：记成待办的话每次启动都重试，健康检查一直挂着这条。
        git_answers.append(_git_fails("fatal: not a git repository"))

        report = config_layout.ensure_layout(ws)

        assert any("not a git repository" in line for line in report.warnings)
        assert _marker(ws)["pending"] == {}
        assert config_layout.ensure_layout(ws).skipped is True

    def test_a_resumed_workspace_keeps_its_legacy_status(
        self, ws: Path, code_root: Path, git_answers: list,
    ) -> None:
        # 接着做的时候 data/events.jsonl 早就有了，不能再现判是不是改造前的工作区。
        (code_root / ".git").mkdir()
        git_answers.append(_git_fails())
        config_layout.ensure_layout(ws)
        assert _marker(ws)["legacy_workspace"] is False
        _write(ws / "data" / "events.jsonl", "{}\n")
        _write(code_root / config_layout.LEGACY_TOOLS_DIR / "own_tool.py", "SPEC = {}\n")
        git_answers.append(subprocess.CompletedProcess(["git"], 0, b"tools/own_tool.py\0", b""))

        config_layout.ensure_layout(ws)

        # 新建的实例不继承别人加的工具，只记下它们。
        assert not (config_layout.custom_tools_dir(ws) / "own_tool.py").exists()
        assert _marker(ws)["legacy_tools"] == ["own_tool.py"]


class TestMarkersFromBefore:
    def test_a_version_one_marker_without_pending_counts_as_done(self, ws: Path, code_root: Path) -> None:
        # 改造之后头一版写的标记没有 pending：那时候没有分步记账，写了标记就是搬完了。
        _write(ws / "data" / ".config_layout.json", json.dumps({
            "version": 1, "migrated_at": "2026-01-01T00:00:00+00:00", "copied": [], "warnings": ["old note"],
        }))
        _write(ws / config_layout.LEGACY_POLICY_FILE, "version: 1\n")

        assert config_layout.ensure_layout(ws).skipped is True
        assert not config_layout.policy_path(ws).exists()

    def test_a_pending_step_this_version_does_not_know_is_ignored(self, ws: Path, code_root: Path) -> None:
        _write(ws / "data" / ".config_layout.json", json.dumps({"version": 1, "pending": {"from_the_future": "x"}}))

        assert config_layout.ensure_layout(ws).skipped is True

    def test_a_marker_that_is_not_json_is_migrated_afresh(self, ws: Path, code_root: Path, git_answers: list) -> None:
        _write(ws / "data" / ".config_layout.json", "{ not json")

        report = config_layout.ensure_layout(ws)

        assert report.skipped is False
        assert _marker(ws)["version"] == config_layout.LAYOUT_VERSION
