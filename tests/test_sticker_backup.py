"""表情包库的备份与恢复。

两条要紧的：备份包来自不可信来源，越界落点和撑爆磁盘的声明必须在落地前拦下；
校验不过就一张图都不动，绝不允许导到一半留下半个库。
"""
from __future__ import annotations

import dataclasses
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
from stickers import store as ss


class Library:
    """一个工作区：库文件、图片目录、备份目录都按线上布局摆。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.out_dir = sb.backup_dir(root)
        self.store = ss.StickerStore(sc.store_path(root))

    def close(self) -> None:
        self.store.close()

    def export(self, **kwargs):
        return sb.export_library(self.root, **kwargs)

    def restore(self, archive: Path, **kwargs):
        return sb.import_library(archive, self.root, **kwargs)

    def files(self) -> dict[str, bytes]:
        media = sc.sticker_root(self.root)
        return {
            path.relative_to(media).as_posix(): path.read_bytes()
            for folder in (sc.PENDING_DIR, sc.LIBRARY_DIR)
            for path in sorted((media / folder).rglob("*")) if path.is_file()
        }


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


def _seed(lib: Library, payload: bytes, *, name: str) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    path = sc.sticker_root(lib.root) / sc.PENDING_DIR / f"{digest}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    lib.store.add(
        sha256=digest,
        rel_path=path.relative_to(lib.root).as_posix(),
        source=ss.SOURCE_GROUP,
        name=name,
        width=64,
        height=48,
        animated=False,
        fmt="png",
        size=len(payload),
        from_group="group1",
        from_user="user1",
        origin="https://example.invalid/a.png",
    )
    return digest


def _populate(lib: Library) -> dict[str, str]:
    """一套覆盖三种状态的库，字段尽量填满，好让逐字段比对有意义。"""
    kept = _seed(lib, b"kept-image-bytes", name="开心")
    lib.store.accept(kept)
    lib.store.set_auto_tags(kept, ["开心", "猫"], version=ss.CAPTION_PROMPT_VERSION)
    lib.store.set_manual_tags(kept, ["招财"], override=True)
    lib.store.record_sent("conv1", kept)

    waiting = _seed(lib, b"waiting-image-bytes", name="难过")
    lib.store.mark_caption(waiting, ss.CAPTION_FAILED, error="模型超时")

    dropped = _seed(lib, b"dropped-image-bytes", name="丢掉")
    (lib.root / lib.store.get(dropped).rel_path).unlink()
    lib.store.discard(dropped)
    return {"kept": kept, "waiting": waiting, "dropped": dropped}


def _manifest_of(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as zf:
        return json.loads(zf.read(sb.MANIFEST_NAME).decode("utf-8"))


def _rebuild(original: Path, forged: Path, *, manifest=None, extra=(), mutate=None) -> Path:
    """照抄一个真备份，只换掉指定部分。恶意包也得先长得像个合法包才测得到防线。"""
    with zipfile.ZipFile(original) as src:
        payload = {info.filename: src.read(info.filename) for info in src.infolist()}
    if manifest is not None:
        payload[sb.MANIFEST_NAME] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(forged, "w") as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
        for name, data in extra:
            zf.writestr(name, data)
        if mutate is not None:
            mutate(zf)
    return forged


def _repack(original: Path, forged: Path, *, replace: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(original) as src:
        payload = {info.filename: src.read(info.filename) for info in src.infolist()}
    payload.update(replace)
    with zipfile.ZipFile(forged, "w") as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    return forged


def _tree(base: Path) -> set[str]:
    return {path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file()}


# ── 往返 ──

def test_every_field_survives_the_round_trip(source: Library, target: Library) -> None:
    digests = _populate(source)
    target.restore(source.export().path)
    for digest in digests.values():
        before = source.store.get(digest)
        after = target.store.get(digest)
        assert after is not None
        for spec in dataclasses.fields(ss.Sticker):
            assert getattr(after, spec.name) == getattr(before, spec.name), spec.name


def test_image_bytes_survive_the_round_trip(source: Library, target: Library) -> None:
    _populate(source)
    target.restore(source.export().path)
    assert target.files() == source.files()


def test_send_history_survives_the_round_trip(source: Library, target: Library) -> None:
    digests = _populate(source)
    target.restore(source.export().path)
    assert target.store.recently_sent("conv1", within_sec=3600) == {digests["kept"]}


def test_discarded_rows_come_back_without_a_file(source: Library, target: Library) -> None:
    # 丢弃过的哈希是黑名单，恢复后必须照样挡住重新入库。
    digests = _populate(source)
    target.restore(source.export().path)
    assert target.store.state_of(digests["dropped"]) == ss.STATE_DISCARDED
    assert target.store.get(digests["dropped"]).rel_path == ""


def test_empty_library_round_trips(source: Library, target: Library) -> None:
    result = target.restore(source.export().path)
    assert result.imported == 0 and result.skipped == 0
    assert target.store.counts()[ss.STATE_LIBRARY] == 0


def test_export_lands_in_the_library_backup_folder(source: Library) -> None:
    _populate(source)
    result = source.export()
    assert result.stickers == 3 and result.images == 2
    assert result.path.parent == sc.sticker_root(source.root) / "backups"
    assert [path.name for path in source.out_dir.iterdir()] == [result.path.name]


# ── 合并与替换 ──

def test_merge_keeps_the_existing_item(source: Library, target: Library) -> None:
    digests = _populate(source)
    archive = source.export().path
    _seed(target, b"kept-image-bytes", name="本地名")
    target.store.set_manual_tags(digests["kept"], ["本地标签"])

    result = target.restore(archive, mode=sb.MODE_MERGE)

    row = target.store.get(digests["kept"])
    assert row.name == "本地名" and row.tags == ["本地标签"]
    assert result.skipped == 1 and result.imported == 2


def test_replace_clears_what_the_backup_does_not_have(source: Library, target: Library) -> None:
    digests = _populate(source)
    archive = source.export().path
    stranger = _seed(target, b"stranger-image-bytes", name="外来")
    stranger_file = target.store.get(stranger).rel_path

    result = target.restore(archive, mode=sb.MODE_REPLACE)

    assert result.removed == 1 and result.imported == 3
    assert not target.store.known(stranger)
    assert not (target.root / stranger_file).exists()
    assert target.store.get(digests["kept"]) is not None


def test_replace_rewrites_a_diverged_row(source: Library, target: Library) -> None:
    digests = _populate(source)
    archive = source.export().path
    _seed(target, b"kept-image-bytes", name="本地名")

    target.restore(archive, mode=sb.MODE_REPLACE)

    assert target.store.get(digests["kept"]).name == "开心"


def test_merge_renames_a_colliding_name(source: Library, target: Library) -> None:
    # 名字撞了不能让整包失败，模型靠名字点图，重名才是真的点歪。
    _seed(source, b"kept-image-bytes", name="开心")
    archive = source.export().path
    other = _seed(target, b"other-image-bytes", name="开心")

    result = target.restore(archive, mode=sb.MODE_MERGE)

    assert result.renamed == 1
    assert target.store.get(other).name == "开心"
    assert {row.name for row in target.store.browse()} == {"开心", "开心2"}


# ── 恶意包 ──

def _refuses(target: Library, archive: Path) -> None:
    scope = target.root.parent
    before = _tree(scope)
    counts = target.store.counts()
    with pytest.raises(sb.BackupError):
        target.restore(archive)
    assert _tree(scope) == before
    assert target.store.counts() == counts


def test_parent_traversal_entry_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    archive = _rebuild(
        source.export().path, tmp_path / "evil.zip",
        extra=[("../../etc/passwd", b"root:x:0:0")],
    )
    _refuses(target, archive)
    assert not (tmp_path / "etc").exists()


def test_absolute_path_entry_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    archive = _rebuild(
        source.export().path, tmp_path / "evil.zip", extra=[("/etc/passwd", b"root:x:0:0")],
    )
    _refuses(target, archive)


def test_windows_separator_entry_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    archive = _rebuild(
        source.export().path, tmp_path / "evil.zip", extra=[("..\\..\\evil.png", b"x")],
    )
    _refuses(target, archive)


def test_symlink_entry_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    def mutate(zf: zipfile.ZipFile) -> None:
        info = zipfile.ZipInfo("images/link.png")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")

    _populate(source)
    archive = _rebuild(source.export().path, tmp_path / "evil.zip", mutate=mutate)
    _refuses(target, archive)


def test_traversing_rel_path_in_the_manifest_is_refused(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    # 成员名干净不代表落点干净：真正写盘用的是 manifest 里的 rel_path。
    _populate(source)
    exported = source.export().path
    manifest = _manifest_of(exported)
    manifest["stickers"][0]["row"]["rel_path"] = "../../../evil.png"
    _refuses(target, _rebuild(exported, tmp_path / "evil.zip", manifest=manifest))
    assert not (tmp_path.parent / "evil.png").exists()


def test_a_rel_path_outside_the_image_folders_is_refused(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    # 落点没越出工作区也可能是灾难：库文件就在图片目录的上一层。
    _populate(source)
    exported = source.export().path
    manifest = _manifest_of(exported)
    manifest["stickers"][0]["row"]["rel_path"] = (
        sc.store_path(target.root).relative_to(target.root).as_posix()
    )
    _refuses(target, _rebuild(exported, tmp_path / "evil.zip", manifest=manifest))
    assert target.store.counts()[ss.STATE_PENDING] == 0


def test_hijacking_an_occupied_path_is_refused(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    # 落点指到在库的某张图上，写下去就是把那张图悄悄换掉。
    _seed(source, b"attacker-image-bytes", name="伪装")
    exported = source.export().path
    _seed(target, b"local-image-bytes", name="本地")
    occupied = target.store.browse()[0].rel_path
    manifest = _manifest_of(exported)
    manifest["stickers"][0]["row"]["rel_path"] = occupied

    _refuses(target, _rebuild(exported, tmp_path / "hijack.zip", manifest=manifest))
    assert (target.root / occupied).read_bytes() == b"local-image-bytes"


def test_oversized_declared_size_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    def mutate(zf: zipfile.ZipFile) -> None:
        zf.writestr("images/pad.bin", b"x")
        zf.filelist[-1].file_size = 3 * 1024 * 1024 * 1024

    _populate(source)
    archive = _rebuild(source.export().path, tmp_path / "bomb.zip", mutate=mutate)
    _refuses(target, archive)


def test_too_many_entries_are_refused(source: Library, target: Library) -> None:
    _populate(source)
    archive = source.export().path
    with pytest.raises(sb.BackupError):
        target.restore(archive, max_entries=2)


def test_a_tight_size_budget_is_enforced(source: Library, target: Library) -> None:
    _populate(source)
    archive = source.export().path
    with pytest.raises(sb.BackupError):
        target.restore(archive, max_total_bytes=4)
    assert target.store.counts()[ss.STATE_PENDING] == 0


def test_manifest_version_mismatch_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    exported = source.export().path
    manifest = _manifest_of(exported)
    manifest["schema"] = sb.SCHEMA_VERSION + 1
    _refuses(target, _rebuild(exported, tmp_path / "old.zip", manifest=manifest))


def test_foreign_archive_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    exported = source.export().path
    manifest = _manifest_of(exported)
    manifest["kind"] = "something-else"
    _refuses(target, _rebuild(exported, tmp_path / "foreign.zip", manifest=manifest))


def test_unknown_columns_are_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    exported = source.export().path
    manifest = _manifest_of(exported)
    manifest["columns"] = [*manifest["columns"], "surprise"]
    _refuses(target, _rebuild(exported, tmp_path / "drifted.zip", manifest=manifest))


def test_missing_manifest_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    exported = source.export().path
    archive = tmp_path / "headless.zip"
    with zipfile.ZipFile(exported) as src, zipfile.ZipFile(archive, "w") as zf:
        for info in src.infolist():
            if info.filename != sb.MANIFEST_NAME:
                zf.writestr(info.filename, src.read(info.filename))
    _refuses(target, archive)


def test_truncated_archive_is_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    raw = source.export().path.read_bytes()
    archive = tmp_path / "cut.zip"
    archive.write_bytes(raw[: len(raw) // 2])
    _refuses(target, archive)


def test_tampered_image_bytes_are_refused(source: Library, target: Library, tmp_path: Path) -> None:
    _populate(source)
    exported = source.export().path
    member = next(item["image"] for item in _manifest_of(exported)["stickers"] if item["image"])
    archive = _repack(
        exported, tmp_path / "tampered.zip", replace={member: b"not-the-original-bytes"},
    )
    _refuses(target, archive)


def test_a_refused_import_leaves_a_populated_library_alone(
    source: Library, target: Library, tmp_path: Path,
) -> None:
    _populate(source)
    exported = source.export().path
    _seed(target, b"local-image-bytes", name="本地")
    manifest = _manifest_of(exported)
    manifest["stickers"][0]["row"]["rel_path"] = "../../../evil.png"
    archive = _rebuild(exported, tmp_path / "evil.zip", manifest=manifest)

    before = target.files()
    with pytest.raises(sb.BackupError):
        target.restore(archive, mode=sb.MODE_REPLACE)
    assert target.files() == before
    assert target.store.counts()[ss.STATE_PENDING] == 1


# ── 轮转 ──

def test_rotation_keeps_only_the_newest(source: Library) -> None:
    _populate(source)
    made = [source.export(keep=2).path for _ in range(4)]
    assert set(sb.list_backups(source.out_dir)) == set(made[-2:])


def test_rotation_reports_what_it_deleted(source: Library) -> None:
    source.export(keep=1)
    result = source.export(keep=1)
    assert len(result.pruned) == 1
    assert not result.pruned[0].exists()


def test_rotation_is_off_when_keep_is_zero(source: Library) -> None:
    # 配置写成 0 该是"不轮转"，把历史备份清空是灾难。
    for _ in range(3):
        source.export(keep=0)
    assert len(sb.list_backups(source.out_dir)) == 3


def test_rotation_ignores_foreign_files(source: Library) -> None:
    source.out_dir.mkdir(parents=True, exist_ok=True)
    stranger = source.out_dir / "notes.txt"
    stranger.write_text("keep me", encoding="utf-8")
    source.export(keep=1)
    source.export(keep=1)
    assert stranger.exists()


# ── 进度回调 ──

def test_progress_is_reported_for_both_directions(source: Library, target: Library) -> None:
    _populate(source)
    seen: list[tuple[str, int, int]] = []
    archive = source.export(progress=lambda *args: seen.append(args)).path
    assert seen == [("export", 1, 3), ("export", 2, 3), ("export", 3, 3)]

    seen.clear()
    target.restore(archive, progress=lambda *args: seen.append(args))
    assert [phase for phase, _, _ in seen] == ["verify"] * 3 + ["install"] * 3
    assert [done for _, done, _ in seen] == [1, 2, 3, 1, 2, 3]


def test_unknown_mode_is_refused(source: Library, target: Library) -> None:
    archive = source.export().path
    with pytest.raises(sb.BackupError):
        target.restore(archive, mode="bogus")
