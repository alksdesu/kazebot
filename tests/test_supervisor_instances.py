"""控制台账号清单的契约：单实例部署必须什么都不显示。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.instances import instances_file, load_instances, url_prefix  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLONOTH_URL_PREFIX", "CLONOTH_INSTANCES_FILE"):
        monkeypatch.delenv(name, raising=False)


def _write(root: Path, body: str) -> Path:
    target = root / "config" / "instances.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


# ── URL 前缀 ───────────────────────────────

def test_no_prefix_by_default() -> None:
    assert url_prefix() == ""


@pytest.mark.parametrize("raw", ["i/123", "/i/123", "i/123/", "  /i/123/  "])
def test_prefix_is_normalised(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", raw)
    assert url_prefix() == "/i/123"


@pytest.mark.parametrize("blank", ["", "   ", "/"])
def test_blank_prefix_means_root(blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", blank)
    assert url_prefix() == ""


# ── 清单 ───────────────────────────────────

def test_missing_file_is_a_single_instance_deployment(tmp_path: Path) -> None:
    assert load_instances(tmp_path) == []


def test_broken_yaml_does_not_raise(tmp_path: Path) -> None:
    _write(tmp_path, "instances: [ this is not: valid: yaml")
    assert load_instances(tmp_path) == []


def test_reads_entries(tmp_path: Path) -> None:
    _write(tmp_path, """
instances:
  - uin: "1000000001"
    label: Rong
    path: ""
  - uin: "1000000002"
    label: koki
    path: /i/1000000002
""")
    # idx 是后加的：根实例推得出是 0，带前缀的老条目推不出来，标 -1 交给端口探测。
    assert load_instances(tmp_path) == [
        {"uin": "1000000001", "label": "Rong", "path": "", "idx": 0},
        {"uin": "1000000002", "label": "koki", "path": "/i/1000000002", "idx": -1},
    ]


def test_label_falls_back_to_uin(tmp_path: Path) -> None:
    _write(tmp_path, 'instances:\n  - uin: "123"\n')
    assert load_instances(tmp_path)[0]["label"] == "123"


def test_path_is_normalised(tmp_path: Path) -> None:
    _write(tmp_path, 'instances:\n  - uin: "123"\n    path: "i/123/"\n')
    assert load_instances(tmp_path)[0]["path"] == "/i/123"


def test_entries_without_uin_are_dropped(tmp_path: Path) -> None:
    _write(tmp_path, 'instances:\n  - label: nameless\n  - uin: "123"\n')
    assert [row["uin"] for row in load_instances(tmp_path)] == ["123"]


def test_duplicate_uin_keeps_the_first(tmp_path: Path) -> None:
    # 重复项在切换器里是一个永远点不到的选项。
    _write(tmp_path, 'instances:\n  - uin: "1"\n    label: A\n  - uin: "1"\n    label: B\n')
    assert [row["label"] for row in load_instances(tmp_path)] == ["A"]


def test_env_overrides_the_file_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "shared.yaml"
    shared.write_text('instances:\n  - uin: "999"\n', encoding="utf-8")
    monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(shared))
    assert instances_file(tmp_path) == shared
    assert [row["uin"] for row in load_instances(tmp_path)] == ["999"]
