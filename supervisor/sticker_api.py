"""控制台的表情包库接口。

库文件被适配器进程同时读写，靠 SQLite 的 WAL 而不是进程内协调。
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from stickers.collect import (
    LIBRARY_DIR,
    PENDING_DIR,
    accept_pending,
    drop_sticker_file,
    sticker_root,
    store_path,
)
from stickers.filter import read_shape
from stickers.store import (
    STATE_LIBRARY,
    STATE_PENDING,
    STATES,
    Sticker,
    StickerStore,
)

from .admin_api import verify_admin_token

logger = logging.getLogger("clonoth.supervisor")

_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
_EXT_BY_FMT = {"png": ".png", "gif": ".gif", "jpeg": ".jpg", "webp": ".webp", "bmp": ".bmp"}
_MIME_BY_FMT = {
    "png": "image/png", "gif": "image/gif", "jpeg": "image/jpeg",
    "webp": "image/webp", "bmp": "image/bmp",
}

_STORES: dict[str, StickerStore] = {}
_STORES_GUARD = threading.Lock()


def _store(workspace_root: Path) -> StickerStore:
    key = str(store_path(workspace_root))
    with _STORES_GUARD:
        handle = _STORES.get(key)
        if handle is None:
            handle = StickerStore(Path(key))
            _STORES[key] = handle
        return handle


def close_stores() -> None:
    with _STORES_GUARD:
        for handle in _STORES.values():
            handle.close()
        _STORES.clear()


def _as_json(row: Sticker) -> dict[str, Any]:
    return {
        "sha256": row.sha256,
        "name": row.name,
        "state": row.state,
        "source": row.source,
        "tags": row.tags,
        "auto_tags": row.auto_tags,
        "manual_tags": row.manual_tags,
        "manual_override": row.manual_override,
        "caption_state": row.caption_state,
        "caption_error": row.caption_error,
        "width": row.width,
        "height": row.height,
        "animated": row.animated,
        "size": row.size,
        "from_group": row.from_group,
        "from_user": row.from_user,
        "sent_count": row.sent_count,
        "last_sent_at": row.last_sent_at,
        "created_at": row.created_at,
        "usable": row.usable,
    }


def create_sticker_router(workspace_root: Path) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    def _row(sha256: str) -> tuple[StickerStore, Sticker]:
        store = _store(workspace_root)
        row = store.get(str(sha256 or "").strip())
        if row is None:
            raise HTTPException(status_code=404, detail="没有这张图")
        return store, row

    @router.get("")
    async def list_stickers(
        state: str = Query(default=""),
        search: str = Query(default=""),
        limit: int = Query(default=60, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        if state and state not in STATES:
            raise HTTPException(status_code=400, detail="未知状态")
        store = _store(workspace_root)
        rows = store.browse(state=state, search=search, limit=limit, offset=offset)
        return {"items": [_as_json(row) for row in rows], "counts": store.counts()}

    @router.get("/counts")
    async def sticker_counts() -> dict[str, int]:
        return _store(workspace_root).counts()

    @router.get("/{sha256}/image")
    async def sticker_image(sha256: str) -> FileResponse:
        """预览图。不走通用文件端点：那个只开放 data/attachments，不该为图库放宽。"""
        _store_handle, row = _row(sha256)
        if not row.rel_path:
            raise HTTPException(status_code=404, detail="这张图已经没有文件了")
        root = sticker_root(workspace_root).resolve()
        target = (workspace_root / row.rel_path).resolve()
        # resolve 之后再比：'..'、绝对路径和软链都会在这一步现形。
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="文件不在库里")
        return FileResponse(target, media_type=_MIME_BY_FMT.get(row.fmt, "image/png"))

    @router.patch("/{sha256}")
    async def update_sticker(sha256: str, request: Request) -> dict[str, Any]:
        store, row = _row(sha256)
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体要是对象")
        if "name" in body:
            store.rename(row.sha256, str(body.get("name") or ""))
        if "manual_tags" in body or "manual_override" in body:
            raw = body.get("manual_tags", row.manual_tags)
            if not isinstance(raw, list):
                raise HTTPException(status_code=400, detail="manual_tags 要是数组")
            override = body.get("manual_override")
            store.set_manual_tags(
                row.sha256,
                [str(item).strip() for item in raw if str(item or "").strip()],
                override=None if override is None else bool(override),
            )
        refreshed = store.get(row.sha256)
        return _as_json(refreshed) if refreshed else {}

    @router.post("/{sha256}/accept")
    async def accept_sticker(sha256: str) -> dict[str, Any]:
        store, row = _row(sha256)
        if not accept_pending(workspace_root, store, row.sha256):
            raise HTTPException(status_code=400, detail="只有待审的图能转正")
        refreshed = store.get(row.sha256)
        return _as_json(refreshed) if refreshed else {}

    @router.post("/{sha256}/discard")
    async def discard_sticker(sha256: str) -> dict[str, Any]:
        """丢弃并记住哈希，同一张图不会再被收第二次。"""
        store, row = _row(sha256)
        drop_sticker_file(workspace_root, row.rel_path)
        store.discard(row.sha256)
        return {"ok": True}

    @router.delete("/{sha256}")
    async def forget_sticker(sha256: str) -> dict[str, Any]:
        """彻底删记录。之后这张图重新出现在群里还会被收。"""
        store, row = _row(sha256)
        drop_sticker_file(workspace_root, row.rel_path)
        store.forget(row.sha256)
        return {"ok": True}

    @router.post("/recaption")
    async def recaption(request: Request) -> dict[str, int]:
        body = await request.json() if await request.body() else {}
        only_failed = bool(body.get("only_failed")) if isinstance(body, dict) else False
        return {"queued": _store(workspace_root).reset_captions(only_failed=only_failed)}

    @router.post("/upload")
    async def upload_sticker(
        file: UploadFile = File(...),
        name: str = Query(default=""),
        accept: bool = Query(default=True),
    ) -> dict[str, Any]:
        """手动传一张。默认直接进库 —— 人工传的不必再走一遍待审。"""
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="空文件")
        if len(content) > _UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail="图太大了")
        shape = read_shape(content[: 64 * 1024])
        if shape is None:
            raise HTTPException(status_code=415, detail="认不出这是什么图片格式")

        store = _store(workspace_root)
        digest = hashlib.sha256(content).hexdigest()
        if store.known(digest):
            raise HTTPException(status_code=409, detail="这张图库里已经有了")

        state = STATE_LIBRARY if accept else STATE_PENDING
        target_dir = sticker_root(workspace_root) / (
            LIBRARY_DIR if accept else PENDING_DIR
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{digest}{_EXT_BY_FMT.get(shape.fmt, '.bin')}"
        target.write_bytes(content)

        row = store.add(
            sha256=digest,
            rel_path=target.relative_to(workspace_root).as_posix(),
            source="upload",
            state=state,
            name=name or Path(file.filename or "").stem,
            width=shape.width,
            height=shape.height,
            animated=shape.animated,
            fmt=shape.fmt,
            size=len(content),
            origin=str(file.filename or ""),
        )
        if row is None:
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail="这张图库里已经有了")
        return _as_json(row)

    @router.post("/import")
    async def import_directory(request: Request) -> dict[str, int]:
        """从服务器上的一个目录批量导入。目录必须在工作区内，不接受任意路径。"""
        body = await request.json() if await request.body() else {}
        raw = str(body.get("path") or "").strip() if isinstance(body, dict) else ""
        accept = bool(body.get("accept")) if isinstance(body, dict) else False
        if not raw:
            raise HTTPException(status_code=400, detail="要给一个目录")
        source = (workspace_root / raw).resolve()
        if workspace_root.resolve() not in source.parents or not source.is_dir():
            raise HTTPException(status_code=400, detail="目录必须在工作区内")

        store = _store(workspace_root)
        state = STATE_LIBRARY if accept else STATE_PENDING
        target_dir = sticker_root(workspace_root) / (LIBRARY_DIR if accept else PENDING_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        stats = {"scanned": 0, "added": 0, "known": 0, "skipped": 0}
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            stats["scanned"] += 1
            try:
                content = path.read_bytes()
            except OSError:
                stats["skipped"] += 1
                continue
            if not content or len(content) > _UPLOAD_MAX_BYTES:
                stats["skipped"] += 1
                continue
            shape = read_shape(content[: 64 * 1024])
            if shape is None:
                stats["skipped"] += 1
                continue
            digest = hashlib.sha256(content).hexdigest()
            if store.known(digest):
                stats["known"] += 1
                continue
            target = target_dir / f"{digest}{_EXT_BY_FMT.get(shape.fmt, '.bin')}"
            shutil.copyfile(path, target)
            row = store.add(
                sha256=digest,
                rel_path=target.relative_to(workspace_root).as_posix(),
                source="import",
                state=state,
                name=path.stem,
                width=shape.width,
                height=shape.height,
                animated=shape.animated,
                fmt=shape.fmt,
                size=len(content),
                origin=str(path.relative_to(source)),
            )
            if row is None:
                target.unlink(missing_ok=True)
                stats["known"] += 1
            else:
                stats["added"] += 1
        return stats

    return router
