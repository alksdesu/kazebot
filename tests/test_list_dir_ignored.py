"""list_dir：依赖、缓存和构建目录只列名字、标注内容已省略，递归时不进去；include_ignored=true 时照常列出，写了认不出的值就拒。"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_runtime import switch_refusal  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.ignore_rules import DEFAULT_IGNORED_DIRS  # noqa: E402

# 包的 __init__ 把 list_dir 这个名字换成了函数，模块得从 sys.modules 里拿
list_dir_module = importlib.import_module("toolbox.builtins.list_dir")


class _Ctx(ToolContext):
    def __init__(self, workspace_root: Path) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-list",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            task_id="task-1",
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def _list(root: Path, **args: Any) -> dict[str, Any]:
    return asyncio.run(list_dir_module.list_dir(args, _Ctx(root)))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for rel in ("main.py", "src/app.py", "src/__pycache__/app.cpython-311.pyc", "node_modules/left-pad/index.js",
                ".git/HEAD", "build/out.bin"):
        _touch(tmp_path / rel)
    return tmp_path


class TestFlatListing:
    def test_ignored_directories_are_named_and_marked(self, project: Path) -> None:
        result = _list(project, paths=["."])

        entries = result["data"]["results"][0]["entries"]
        assert entries == [
            {"name": ".git/", "type": "directory", "ignored": True},
            {"name": "build/", "type": "directory", "ignored": True},
            {"name": "node_modules/", "type": "directory", "ignored": True},
            {"name": "src/", "type": "directory"},
            {"name": "main.py", "type": "file"},
        ]
        text = result["data"]["result"]
        assert f"📁 node_modules/ {list_dir_module.IGNORED_NOTE}" in text
        assert "📁 src/\n" in text
        assert "3 个依赖、缓存或构建目录只列了名字" in text
        assert "include_ignored=true" in text

    def test_ignored_directories_still_count_as_directories(self, project: Path) -> None:
        section = _list(project, paths=["."])["data"]["results"][0]

        assert (section["fileCount"], section["dirCount"]) == (1, 4)

    def test_include_ignored_lists_them_plainly(self, project: Path) -> None:
        result = _list(project, paths=["."], include_ignored=True)

        entries = result["data"]["results"][0]["entries"]
        assert all("ignored" not in entry for entry in entries)
        assert "只列了名字" not in result["data"]["result"]

    def test_listing_an_ignored_directory_directly_shows_its_contents(self, project: Path) -> None:
        entries = _list(project, paths=["node_modules"])["data"]["results"][0]["entries"]

        assert entries == [{"name": "left-pad/", "type": "directory"}]


class TestRecursiveListing:
    def test_ignored_directories_are_named_but_not_entered(self, project: Path) -> None:
        section = _list(project, paths=["."], recursive=True)["data"]["results"][0]

        assert section["entries"] == [
            {"name": ".git/", "type": "directory", "ignored": True},
            {"name": "build/", "type": "directory", "ignored": True},
            {"name": "node_modules/", "type": "directory", "ignored": True},
            {"name": "src/", "type": "directory"},
            {"name": "src/__pycache__/", "type": "directory", "ignored": True},
            {"name": "main.py", "type": "file"},
            {"name": "src/app.py", "type": "file"},
        ]
        assert (section["fileCount"], section["dirCount"]) == (2, 5)

    @pytest.mark.parametrize("flag", [True, "true", "yes", 1])
    def test_include_ignored_walks_into_them(self, project: Path, flag: Any) -> None:
        section = _list(project, paths=["."], recursive=True, include_ignored=flag)["data"]["results"][0]

        names = [entry["name"] for entry in section["entries"]]
        assert "node_modules/left-pad/index.js" in names
        assert "src/__pycache__/app.cpython-311.pyc" in names
        assert all("ignored" not in entry for entry in section["entries"])

    @pytest.mark.parametrize("flag", [False, "false", "no", 0, None, "", "  "])
    def test_false_or_unset_keeps_the_default(self, project: Path, flag: Any) -> None:
        section = _list(project, paths=["."], recursive=True, include_ignored=flag)["data"]["results"][0]

        assert "node_modules/left-pad/index.js" not in [entry["name"] for entry in section["entries"]]

    @pytest.mark.parametrize("flag", ["garbage", "全部", 2, ["true"]])
    def test_an_unrecognised_value_is_refused_instead_of_listing(self, project: Path, flag: Any) -> None:
        result = _list(project, paths=["."], recursive=True, include_ignored=flag)

        assert result["ok"] is False
        assert result["error"] == switch_refusal("include_ignored", flag)
        assert result["data"]["results"] == []


@pytest.mark.parametrize("name", sorted(DEFAULT_IGNORED_DIRS))
def test_every_default_ignored_name_is_marked(tmp_path: Path, name: str) -> None:
    _touch(tmp_path / name / "inside.txt")

    section = _list(tmp_path, paths=["."], recursive=True)["data"]["results"][0]

    assert section["entries"] == [{"name": f"{name}/", "type": "directory", "ignored": True}]
