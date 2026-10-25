"""控制台的表情包接口：预览只读待审和在库两个目录，上传和目录导入走同一个落盘函数。

预览原先的边界是整个 data/stickers，库文件和备份包也能被当图读走；上传和目录导入
各自写盘，竞败时删掉的可能是赢家的文件。
"""
from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.sticker_api as sticker_api  # noqa: E402
from stickers import files as sf  # noqa: E402
from stickers import store as ss  # noqa: E402
from supervisor.admin_api import verify_admin_token  # noqa: E402


def _png(color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path: Path):
    app = FastAPI()
    app.include_router(sticker_api.create_sticker_router(tmp_path), prefix="/stickers")
    app.dependency_overrides[verify_admin_token] = lambda: None
    with TestClient(app) as test_client:
        yield test_client
    sticker_api.close_stores()


def _pretend_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # 别的进程恰好在 known() 和 add() 之间登记了同一张图。
    monkeypatch.setattr(ss.StickerStore, "known", lambda self, sha256: False)


def test_the_preview_never_serves_the_library_database(client: TestClient, tmp_path: Path) -> None:
    store = sticker_api.library_store(tmp_path)
    digest = "e" * 64
    store.add(
        sha256=digest, rel_path="data/stickers/stickers.sqlite3",
        source=ss.SOURCE_IMPORT, state=ss.STATE_LIBRARY, fmt="png",
    )
    assert sf.store_path(tmp_path).is_file()

    response = client.get(f"/stickers/{digest}/image")

    assert response.status_code == 404


def test_an_upload_lands_in_the_library_and_previews(client: TestClient, tmp_path: Path) -> None:
    data = _png()
    digest = hashlib.sha256(data).hexdigest()

    uploaded = client.post("/stickers/upload", files={"file": ("a.png", data, "image/png")})

    assert uploaded.status_code == 200
    row = sticker_api.library_store(tmp_path).get(digest)
    assert row is not None and row.rel_path == f"data/stickers/library/{digest}.png"
    preview = client.get(f"/stickers/{digest}/image")
    assert preview.status_code == 200
    assert preview.content == data


def test_an_upload_beaten_to_another_folder_leaves_the_winners_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _png()
    digest = hashlib.sha256(data).hexdigest()
    winner = sf.sticker_root(tmp_path) / "pending" / f"{digest}.png"
    winner.parent.mkdir(parents=True)
    winner.write_bytes(data)
    sticker_api.library_store(tmp_path).add(
        sha256=digest, rel_path=f"data/stickers/pending/{digest}.png",
        source=ss.SOURCE_GROUP, state=ss.STATE_PENDING, fmt="png",
    )
    _pretend_unknown(monkeypatch)

    response = client.post("/stickers/upload", files={"file": ("a.png", data, "image/png")})

    assert response.status_code == 409
    assert winner.read_bytes() == data
    assert not (sf.sticker_root(tmp_path) / "library" / f"{digest}.png").exists()


def test_an_upload_beaten_to_the_same_path_keeps_the_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _png()
    digest = hashlib.sha256(data).hexdigest()
    target = sf.sticker_root(tmp_path) / "library" / f"{digest}.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    sticker_api.library_store(tmp_path).add(
        sha256=digest, rel_path=f"data/stickers/library/{digest}.png",
        source=ss.SOURCE_GROUP, state=ss.STATE_LIBRARY, fmt="png",
    )
    _pretend_unknown(monkeypatch)

    response = client.post("/stickers/upload", files={"file": ("a.png", data, "image/png")})

    assert response.status_code == 409
    assert target.read_bytes() == data


def test_a_directory_import_beaten_to_the_same_path_keeps_the_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _png((0, 0, 255))
    digest = hashlib.sha256(data).hexdigest()
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "a.png").write_bytes(data)
    target = sf.sticker_root(tmp_path) / "pending" / f"{digest}.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    sticker_api.library_store(tmp_path).add(
        sha256=digest, rel_path=f"data/stickers/pending/{digest}.png",
        source=ss.SOURCE_GROUP, state=ss.STATE_PENDING, fmt="png",
    )
    _pretend_unknown(monkeypatch)

    response = client.post("/stickers/import", json={"path": "incoming", "accept": False})

    assert response.status_code == 200
    assert response.json()["known"] == 1
    assert target.read_bytes() == data


def test_a_directory_import_leaves_no_temp_file_behind(client: TestClient, tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "a.png").write_bytes(_png((0, 255, 0)))

    response = client.post("/stickers/import", json={"path": "incoming", "accept": True})

    assert response.json()["added"] == 1
    library = sf.sticker_root(tmp_path) / "library"
    assert [path.name for path in library.iterdir() if path.name.endswith(".tmp")] == []
