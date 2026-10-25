"""表情包 rel_path 的落点约束：只认待审和在库两个目录。

库里登记的路径会被清理拿去删、被转正拿去搬、被打标读出来发给看图渠道、被备份打进包里，
所以每个消费方都走同一个解析函数；导入时有图无图的每一条都先过这一关，有一条越界整包不认。
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stickers import backup as sb
from stickers import collect as sc
from stickers import files as sf
from stickers import store as ss
from stickers import tagger as st


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("这台机器建不了软链")


# ── 解析 ──

@pytest.mark.parametrize("rel", [
    "data/stickers/pending/a.png",
    "data/stickers/library/a.png",
    "data/stickers/pending/ab/a.png",
])
def test_paths_inside_the_image_folders_resolve(tmp_path: Path, rel: str) -> None:
    found = sf.sticker_file(tmp_path, rel)
    assert found == (tmp_path / rel).resolve()


@pytest.mark.parametrize("rel", [
    "",
    "../evil.png",
    "data/stickers/../../evil.png",
    "data/stickers/pending/../library/a.png",
    "data/stickers/pending/./a.png",
    "data/stickers//pending/a.png",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "data\\stickers\\pending\\a.png",
    " data/stickers/pending/a.png",
    "data/stickers/pending/",
    "data/stickers/pending",
    "data/stickers/stickers.sqlite3",
    "data/stickers/stickers.sqlite3.tagging.lock",
    "data/stickers/backups/stickers-20261025-000000-000000.zip",
    "data/stickers/a.png",
    "config/models.yaml",
    ".env",
])
def test_everything_else_is_refused(tmp_path: Path, rel: str) -> None:
    assert sf.sticker_file(tmp_path, rel) is None


def test_a_link_inside_the_library_that_points_out_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    library = sf.sticker_root(tmp_path) / sf.LIBRARY_DIR
    library.mkdir(parents=True)
    _link(library / "evil.png", secret)

    assert sf.sticker_file(tmp_path, "data/stickers/library/evil.png") is None


def test_a_shared_library_behind_a_link_still_counts_as_inside(tmp_path: Path) -> None:
    shared = tmp_path / "shared" / "stickers"
    (shared / sf.PENDING_DIR).mkdir(parents=True)
    image = shared / sf.PENDING_DIR / "a.png"
    image.write_bytes(b"image")
    workspace = tmp_path / "instance"
    (workspace / "data").mkdir(parents=True)
    _link(workspace / "data" / "stickers", shared)

    assert sf.sticker_file(workspace, "data/stickers/pending/a.png") == image.resolve()


# ── 消费方 ──

@pytest.fixture()
def store(tmp_path: Path):
    handle = ss.StickerStore(sf.store_path(tmp_path))
    yield handle
    handle.close()


def _victim(root: Path, rel: str = "config/models.yaml", data: bytes = b"api_key: secret") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_dropping_a_row_that_points_outside_never_deletes_that_file(tmp_path: Path, store) -> None:
    victim = _victim(tmp_path)
    sc.drop_sticker_file(tmp_path, "config/models.yaml")
    sc.drop_sticker_file(tmp_path, "data/stickers/stickers.sqlite3")

    assert victim.read_bytes() == b"api_key: secret"
    assert sf.store_path(tmp_path).is_file()


def test_accepting_a_row_that_points_outside_moves_nothing(tmp_path: Path, store) -> None:
    victim = _victim(tmp_path, "config/a.png", b"not a sticker")
    digest = "d" * 64
    store.add(sha256=digest, rel_path="config/a.png", source=ss.SOURCE_IMPORT, state=ss.STATE_PENDING)

    assert not sc.accept_pending(tmp_path, store, digest)
    assert victim.read_bytes() == b"not a sticker"
    assert not (sf.sticker_root(tmp_path) / sf.LIBRARY_DIR / "a.png").exists()
    assert store.get(digest).state == ss.STATE_PENDING


def test_expiring_a_row_that_points_outside_never_deletes_that_file(tmp_path: Path, store) -> None:
    victim = _victim(tmp_path)
    digest = "c" * 64
    store.add(sha256=digest, rel_path="config/models.yaml", source=ss.SOURCE_IMPORT, state=ss.STATE_PENDING)
    store._db.execute("UPDATE stickers SET created_at=0")
    config = sc.CollectConfig(enabled=True, pending_ttl_sec=60)
    collector = sc.StickerCollector(tmp_path, config=lambda: config, open_store=lambda: store)
    collector._last_sweep = float("-inf")

    collector._sweep(store, config)

    assert store.state_of(digest) == ss.STATE_DISCARDED
    assert victim.read_bytes() == b"api_key: secret"


@pytest.mark.asyncio
async def test_the_tagger_never_reads_a_file_outside_the_library(
    tmp_path: Path, store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _victim(tmp_path, "secret.png", b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    digest = "f" * 64
    store.add(sha256=digest, rel_path="secret.png", source=ss.SOURCE_IMPORT, state=ss.STATE_LIBRARY, name="越界")
    config = st.TaggerConfig(min_interval=0.0)
    tagger = st.StickerTagger(tmp_path, config=lambda: config, open_store=lambda: store)
    described: list[Path] = []

    async def describe(_vision, path):
        described.append(path)
        return ["表情包", "开心", "猫"]

    monkeypatch.setattr(tagger, "_describe", describe)
    claim = store.claim_caption()
    assert claim is not None

    assert await tagger._caption(store, object(), config, claim) is False
    assert described == []
    row = store.get(digest)
    assert row.caption_state == ss.CAPTION_FAILED
    assert row.caption_error == "文件不在库里"


def test_export_never_packs_a_file_outside_the_library(tmp_path: Path, store) -> None:
    _victim(tmp_path, "config/secret.png", b"top-secret-bytes")
    digest = "e" * 64
    store.add(sha256=digest, rel_path="config/secret.png", source=ss.SOURCE_IMPORT, state=ss.STATE_LIBRARY)

    archive = sb.export_library(tmp_path).path

    with zipfile.ZipFile(archive) as zf:
        assert all(b"top-secret-bytes" not in zf.read(name) for name in zf.namelist())
        manifest = json.loads(zf.read(sb.MANIFEST_NAME).decode("utf-8"))
    entry = manifest["stickers"][0]
    assert entry["image"] == ""
    assert entry["row"]["rel_path"] == "config/secret.png"


# ── 导入：每一条都过落点校验 ──

class Library:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = ss.StickerStore(sf.store_path(root))

    def close(self) -> None:
        self.store.close()

    def seed(self, payload: bytes, *, name: str) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        path = sf.sticker_root(self.root) / sf.PENDING_DIR / f"{digest}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        self.store.add(
            sha256=digest, rel_path=path.relative_to(self.root).as_posix(),
            source=ss.SOURCE_GROUP, name=name, fmt="png", size=len(payload),
        )
        return digest

    def rel(self, digest: str) -> str:
        return self.store.get(digest).rel_path

    def snapshot(self) -> tuple[dict[str, int], dict[str, bytes]]:
        files = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file() and not path.name.startswith("stickers.sqlite3")
        }
        return self.store.counts(), files


@pytest.fixture()
def source(tmp_path: Path):
    handle = Library(tmp_path / "source")
    yield handle
    handle.close()


@pytest.fixture()
def target(tmp_path: Path):
    handle = Library(tmp_path / "target")
    yield handle
    handle.close()


def _forge(original: Path, forged: Path, mutate) -> Path:
    """照抄一个真备份，只改 manifest。恶意包得先长得像合法包，才测得到落点这道关。"""
    with zipfile.ZipFile(original) as src:
        payload = {info.filename: src.read(info.filename) for info in src.infolist()}
    manifest = json.loads(payload[sb.MANIFEST_NAME].decode("utf-8"))
    mutate({item["row"]["sha256"]: item for item in manifest["stickers"]})
    payload[sb.MANIFEST_NAME] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(forged, "w") as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    return forged


def _without_image(entry: dict, rel_path: str) -> None:
    entry["image"] = ""
    entry["row"]["rel_path"] = rel_path


def _refused(target: Library, archive: Path, **kwargs) -> sb.BackupError:
    before = target.snapshot()
    with pytest.raises(sb.BackupError) as caught:
        sb.import_library(archive, target.root, **kwargs)
    assert target.snapshot() == before
    return caught.value


def test_an_imageless_entry_landing_outside_the_library_refuses_the_whole_package(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    kept = source.seed(b"kept-image-bytes", name="开心")
    forged = source.seed(b"forged-image-bytes", name="伪装")
    exported = sb.export_library(source.root).path
    secret = _victim(target.root)
    archive = _forge(exported, tmp_path / "evil.zip", lambda rows: _without_image(rows[forged], "config/models.yaml"))

    error = _refused(target, archive)

    assert [(item.sha256, item.rel_path) for item in error.rejected] == [(forged, "config/models.yaml")]
    assert error.rejected[0].reason
    assert "config/models.yaml" in str(error)
    assert not target.store.known(kept)
    assert secret.read_bytes() == b"api_key: secret"


def test_an_imageless_entry_pointing_at_the_library_file_is_refused(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    forged = source.seed(b"forged-image-bytes", name="伪装")
    exported = sb.export_library(source.root).path
    rel = sf.store_path(target.root).relative_to(target.root).as_posix()
    archive = _forge(exported, tmp_path / "evil.zip", lambda rows: _without_image(rows[forged], rel))

    error = _refused(target, archive)

    assert [(item.sha256, item.rel_path) for item in error.rejected] == [(forged, rel)]


def test_an_imageless_entry_aliasing_a_local_sticker_is_refused(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    # 指到在库别的图上：日后清理或丢弃这一条，会把那张图的文件一起删掉。
    forged = source.seed(b"forged-image-bytes", name="伪装")
    exported = sb.export_library(source.root).path
    local = target.seed(b"local-image-bytes", name="本地")
    occupied = target.rel(local)
    archive = _forge(exported, tmp_path / "alias.zip", lambda rows: _without_image(rows[forged], occupied))

    error = _refused(target, archive)

    assert [(item.sha256, item.rel_path) for item in error.rejected] == [(forged, occupied)]
    assert local[:12] in error.rejected[0].reason
    assert (target.root / occupied).read_bytes() == b"local-image-bytes"


def test_two_entries_sharing_one_landing_are_refused(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    first = source.seed(b"first-image-bytes", name="甲")
    second = source.seed(b"second-image-bytes", name="乙")
    exported = sb.export_library(source.root).path
    shared = source.rel(first)
    archive = _forge(exported, tmp_path / "shared.zip", lambda rows: _without_image(rows[second], shared))

    error = _refused(target, archive)

    assert len(error.rejected) == 1
    assert error.rejected[0].rel_path == shared
    assert error.rejected[0].sha256 in {first, second}


def test_every_offending_entry_is_reported(source: Library, target: Library, tmp_path: Path) -> None:
    good = source.seed(b"good-image-bytes", name="好")
    outside = source.seed(b"outside-image-bytes", name="越界")
    traversal = source.seed(b"traversal-image-bytes", name="穿越")
    exported = sb.export_library(source.root).path

    def mutate(rows: dict) -> None:
        _without_image(rows[outside], "config/models.yaml")
        rows[traversal]["row"]["rel_path"] = "../../evil.png"

    error = _refused(target, _forge(exported, tmp_path / "many.zip", mutate))

    assert sorted((item.sha256, item.rel_path) for item in error.rejected) == sorted([
        (outside, "config/models.yaml"), (traversal, "../../evil.png"),
    ])
    assert not target.store.known(good)
    assert not (tmp_path / "evil.png").exists()


def test_replace_mode_refuses_an_imageless_entry_outside_the_library(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    forged = source.seed(b"forged-image-bytes", name="伪装")
    exported = sb.export_library(source.root).path
    local = target.seed(b"local-image-bytes", name="本地")
    archive = _forge(exported, tmp_path / "evil.zip", lambda rows: _without_image(rows[forged], "config/models.yaml"))

    error = _refused(target, archive, mode=sb.MODE_REPLACE)

    assert [item.sha256 for item in error.rejected] == [forged]
    assert target.store.known(local)


def test_replace_mode_refuses_an_imageless_entry_adopting_another_images_file(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    # replace 会清掉本地的行，但文件留在原地；无图条目指过去就把别人的图认成了自己的。
    forged = source.seed(b"forged-image-bytes", name="伪装")
    exported = sb.export_library(source.root).path
    local = target.seed(b"local-image-bytes", name="本地")
    occupied = target.rel(local)
    archive = _forge(exported, tmp_path / "adopt.zip", lambda rows: _without_image(rows[forged], occupied))

    error = _refused(target, archive, mode=sb.MODE_REPLACE)

    assert [(item.sha256, item.rel_path) for item in error.rejected] == [(forged, occupied)]
    assert target.store.known(local)


def test_replace_mode_lets_an_image_overwrite_a_local_file_it_replaces(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    incoming = source.seed(b"incoming-image-bytes", name="新")
    exported = sb.export_library(source.root).path
    local = target.seed(b"local-image-bytes", name="旧")
    occupied = target.rel(local)

    def mutate(rows: dict) -> None:
        rows[incoming]["row"]["rel_path"] = occupied

    result = sb.import_library(_forge(exported, tmp_path / "swap.zip", mutate), target.root, mode=sb.MODE_REPLACE)

    assert result.imported == 1 and result.removed == 1
    assert not target.store.known(local)
    assert target.rel(incoming) == occupied
    assert (target.root / occupied).read_bytes() == b"incoming-image-bytes"


def test_a_clean_imageless_entry_still_imports(source: Library, target: Library, tmp_path: Path) -> None:
    # 文件导出时就不在了的行照样能恢复：落点在库里、没撞别人，就原样进库。
    missing = source.seed(b"missing-image-bytes", name="缺图")
    rel = source.rel(missing)
    exported = sb.export_library(source.root).path
    archive = _forge(exported, tmp_path / "missing.zip", lambda rows: _without_image(rows[missing], rel))

    result = sb.import_library(archive, target.root)

    assert result.imported == 1
    assert target.rel(missing) == rel
    assert not (target.root / rel).exists()


def test_a_long_rejection_list_is_shortened_in_the_message_but_kept_whole_on_the_error() -> None:
    rejected = [sb.Rejection("a" * 64, f"config/{index}.png", "不在待审或在库目录下") for index in range(15)]
    with pytest.raises(sb.BackupError) as caught:
        sb._refuse(rejected)
    assert caught.value.rejected == tuple(rejected)
    assert "等 15 条" in str(caught.value)
    assert "config/14.png" not in str(caught.value)
    assert sb.BackupError("别的错").rejected == ()
