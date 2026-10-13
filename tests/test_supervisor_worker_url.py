from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.main import _worker_supervisor_url  # noqa: E402


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [(None, "http://127.0.0.1:8765"), ("i/miaomiao", "http://127.0.0.1:8775/i/miaomiao")],
)
def test_worker_url_matches_the_instance_mount(
    prefix: str | None, expected: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if prefix is None:
        monkeypatch.delenv("CLONOTH_URL_PREFIX", raising=False)
    else:
        monkeypatch.setenv("CLONOTH_URL_PREFIX", prefix)

    assert _worker_supervisor_url("127.0.0.1", 8775 if prefix else 8765) == expected
