from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from engine.materials import MaterialError, MaterialService
from engine.materials.storage import MAX_FILE_BYTES, identity
from .feature_auth import resolve_actor
from .admin_api import verify_admin_token
from .conversation_labels import describe_namespaces, memory_namespace, scoped_conversation_keys

logger = logging.getLogger(__name__)


class MaterialRuntime:
    def __init__(self, service: MaterialService):
        self.service = service
        self.slots = asyncio.Semaphore(2)
        self.inflight: set[asyncio.Task] = set()
        self.background: set[asyncio.Task] = set()
        self.closing = False
        self.closed = False
        self.close_lock = asyncio.Lock()

    async def call(self, function, *args, **kwargs):
        if self.closing:
            raise HTTPException(503, detail="资料服务正在停止，请稍后重试。")
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.inflight.add(task)

        def finished(completed):
            self.inflight.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except MaterialError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

    def launch(self, actor, job: dict) -> None:
        async def execute():
            async with self.slots:
                if not self.closing:
                    await self.call(self.service.run_job, actor, job["id"])

        task = asyncio.create_task(execute())
        self.background.add(task)

        def finished(completed):
            self.background.discard(completed)
            if not completed.cancelled() and completed.exception():
                logger.error("Material job failed: %s", completed.exception())

        task.add_done_callback(finished)

    async def close(self) -> None:
        async with self.close_lock:
            if self.closed:
                return
            self.closing = True
            try:
                await asyncio.to_thread(self.service.interrupt_jobs)
            finally:
                await asyncio.gather(*list(self.background | self.inflight), return_exceptions=True)
                await asyncio.to_thread(self.service.close)
                self.closed = True


def create_router(state) -> APIRouter:
    router = APIRouter(prefix="/v1/materials", tags=["materials"])
    if not getattr(state, "materials", None):
        state.materials = MaterialService(state.workspace_root)
    service = state.materials
    runtime = MaterialRuntime(service)
    state.materials_runtime = runtime
    slots = runtime.slots
    call = runtime.call

    def actor(request: Request):
        result = resolve_actor(request, state)
        if result.is_admin and result.channel == "web" and not result.task_id and "bot_scope" in request.query_params:
            result = replace(result, bot_scope=request.query_params["bot_scope"])
        return result

    @router.get("/capabilities")
    async def capabilities(request: Request):
        resolve_actor(request, state)
        return await call(service.capabilities)

    @router.get("/scopes")
    async def scopes(request: Request):
        current = actor(request)
        if current.task_id or not current.is_admin:
            return [{"scope": current.scope, "bot_scope": current.bot_scope}]
        with service.store.lock:
            rows = service.store.db.execute("SELECT scope,bot_scope FROM objects UNION SELECT scope,bot_scope FROM settings UNION SELECT scope,bot_scope FROM journal").fetchall()
        if current.channel != "web" or not current.interactive:
            return [dict(row) for row in rows]
        labels = describe_namespaces(state.workspace_root, conversation_keys={row["scope"] for row in rows})
        current_keys = scoped_conversation_keys(state.workspace_root)
        return [{
            **dict(row),
            "owner": labels.get(memory_namespace(row["scope"])),
            "current_account": (
                current_keys is None or not row["scope"].startswith(("qq_group:", "qq_private:"))
                or row["scope"] in current_keys
            ),
        } for row in rows]

    @router.get("/settings")
    async def settings(request: Request):
        return await call(service.store.settings, actor(request))

    @router.put("/settings")
    async def update_settings(request: Request, body: dict = Body(...)):
        current = actor(request)
        current.require_interaction()
        current.require_manager(current.scope)
        return await call(service.store.save_settings, current, body)

    @router.get("/sources")
    async def sources(request: Request):
        return await call(service.list_sources, actor(request))

    @router.post("/sources/upload")
    async def upload_source(request: Request, file: UploadFile = File(...)):
        current = actor(request)
        await call(identity, current)
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > MAX_FILE_BYTES:
                raise HTTPException(413, detail="文件超过 50 MB。")
        name = Path(file.filename or "source.bin").name
        suffix = Path(name).suffix.lower()
        if suffix not in {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".tsv", ".json", ".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            raise HTTPException(415, detail="不支持的资料类型。")
        directory = state.workspace_root / "data" / "attachments" / "materials_upload"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(content).hexdigest() + suffix)
        await asyncio.to_thread(path.write_bytes, content)
        return await call(service.register_source, current, {"path": path.relative_to(state.workspace_root).as_posix(), "name": name})

    @router.post("/sources/register")
    async def register_source(request: Request, body: dict = Body(...)):
        current = actor(request)
        if current.task_id:
            with state._lock:
                task = state.tasks.get(current.task_id)
                attachments = list(task.input.get("attachments") or []) if task else []
                context = task.input.get("task_context") or {} if task else {}
                attachments.extend(context.get("attachments") or [])
            allowed = {str(item.get("path") or "").replace("\\", "/") for item in attachments if isinstance(item, dict)}
            if str(body.get("path") or "").replace("\\", "/") not in allowed:
                raise HTTPException(403, detail="该路径不属于本任务实际收到的附件，请由用户重新上传。")
        return await call(service.register_source, current, body)

    @router.post("/sources/{source_id}/parse")
    async def parse_source(source_id: str, request: Request):
        async with slots:
            return await call(service.parse_source, actor(request), source_id)

    @router.get("/sources/{source_id}/spans")
    async def spans(source_id: str, request: Request, q: str = "", page: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
        async with slots:
            return await call(service.search_source, actor(request), source_id, q, page, limit)

    @router.post("/sources/{source_id}/cite")
    async def cite(source_id: str, request: Request, body: dict = Body(...)):
        return await call(service.cite, actor(request), source_id, str(body.get("span_id") or ""), body.get("quote", ""))

    @router.get("/sources/{source_id}/file")
    async def source_file(source_id: str, request: Request):
        source = await call(service.source, actor(request), source_id)
        path = await call(service.store.path, source["blob"], source["sha256"])
        return FileResponse(path, media_type=source["mime_type"], filename=source["name"])

    @router.post("/journal/messages")
    async def record_message(request: Request, body: dict = Body(...)):
        current = actor(request)
        if current.task_id:
            raise HTTPException(403, detail="模型不能把生成文本写成平台原消息。")
        return await call(service.record_message, current, body)

    @router.get("/journal/search")
    async def search_messages(request: Request, q: str = "", since: float = 0, until: float = 0, sender: str = "", limit: int = Query(30, ge=1, le=50)):
        return await call(service.search_messages, actor(request), q, since, until, sender, limit)

    @router.get("/journal/messages/{reference}")
    async def get_message(reference: str, request: Request):
        return await call(service.get_message, actor(request), reference)

    @router.post("/extractions")
    async def create_extraction(request: Request, body: dict = Body(...)):
        return await call(service.create_extraction, actor(request), body)

    @router.get("/extractions")
    async def extractions(request: Request):
        return await call(service.store.list, "extraction", actor(request))

    @router.patch("/extractions/{extraction_id}")
    async def correct_extraction(extraction_id: str, request: Request, body: dict = Body(...)):
        current = actor(request)
        if body.get("confirm") is True:
            current.require_interaction()
        return await call(service.correct_extraction, current, extraction_id, body)

    @router.get("/artifacts")
    async def artifacts(request: Request):
        return await call(service.store.list, "artifact", actor(request))

    @router.get("/artifacts/{artifact_id}")
    async def artifact(artifact_id: str, request: Request):
        return await call(service.artifact, actor(request), artifact_id)

    @router.post("/artifacts/generate")
    async def generate(request: Request, body: dict = Body(...)):
        current = actor(request)
        job = await call(service.create_job, current, body)
        runtime.launch(current, job)
        return job

    @router.post("/artifacts/images")
    async def image_version(request: Request, body: dict = Body(...)):
        async with slots:
            return await call(service.register_image_version, actor(request), body)

    @router.post("/image-operations")
    async def begin_image_operation(request: Request, body: dict = Body(...)):
        return await call(service.begin_image_operation, actor(request), body)

    @router.post("/image-operations/{operation_id}/complete")
    async def complete_image_operation(operation_id: str, request: Request, body: dict = Body(...)):
        verify_admin_token(request)
        task_id = request.headers.get("X-Clonoth-Task-Id", "")
        with state._lock:
            task = state.tasks.get(task_id)
            if task is None or task.cancel_requested or task.input.get("_session_reset"):
                raise HTTPException(403, detail="图片所属任务已取消或会话已清空。")
        async with slots:
            return await call(service.complete_image_operation, operation_id, task_id, body)

    @router.post("/artifacts/{artifact_id}/restore")
    async def restore(artifact_id: str, request: Request, body: dict = Body(...)):
        current = actor(request)
        return await call(service.restore_version, current, artifact_id, body)

    @router.get("/versions/{version_id}/reference")
    async def image_reference(version_id: str, request: Request):
        current = actor(request)
        version = await call(service.store.get, version_id, current, kind="version")
        if version["format"] not in {"png", "jpg", "jpeg", "webp", "gif"}:
            raise HTTPException(422, detail="该版本不是图片。")
        path = await call(service.store.path, version["blob"], version["sha256"])
        artifact = await call(service.store.get, version["artifact_id"], current, kind="artifact")
        return {"version_id": version_id, "artifact_id": artifact["id"], "expected_version": artifact["current_version"],
                "path": path.relative_to(state.workspace_root).as_posix(), "sha256": version["sha256"]}

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        return await call(service.store.get, job_id, actor(request), kind="job")

    @router.get("/jobs")
    async def list_jobs(request: Request):
        return await call(service.store.list, "job", actor(request))

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request):
        return await call(service.cancel_job, actor(request), job_id)

    @router.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str, request: Request):
        current = actor(request)
        job = await call(service.retry_job, current, job_id)
        runtime.launch(current, job)
        return job

    @router.get("/versions/{version_id}/preview/{page}")
    async def preview(version_id: str, page: int, request: Request):
        path = await call(service.preview_file, actor(request), version_id, page)
        return FileResponse(path, media_type="image/png")

    @router.get("/versions/{version_id}/preview")
    async def preview_attachments(version_id: str, request: Request):
        return await call(service.preview_attachments, actor(request), version_id)

    @router.post("/versions/{version_id}/approve")
    async def approve(version_id: str, request: Request, body: dict = Body(...)):
        current = actor(request)
        current.require_interaction()
        return await call(service.approve_version, current, version_id, body)

    @router.get("/versions/{version_id}/export")
    async def export(version_id: str, request: Request):
        current = actor(request)
        return await call(service.export_version, current, version_id)

    @router.get("/versions/{version_id}/download")
    async def download(version_id: str, request: Request):
        current = actor(request)
        result = await call(service.export_version, current, version_id)
        attachment = result["attachment"]
        return FileResponse(state.workspace_root / attachment["path"], media_type=attachment["mime_type"], filename=attachment["name"])

    @router.post("/versions/{version_id}/send")
    async def send(version_id: str, request: Request):
        current = actor(request)
        current.require_interaction()
        result = await call(service.export_version, current, version_id)
        delivery_id = f"materials:{version_id}:{result['sha256']}"
        with state._lock:
            session_id = state.conversation_map.get(current.scope)
            if not session_id:
                raise HTTPException(409, detail="当前会话没有可用的发送路由，可先下载成品。")
            event = state.eventlog.find_outbound_delivery(delivery_id)
            if event is None:
                event = state.eventlog.append(session_id=session_id, component="supervisor", type_="outbound_message", payload={
                    "text": "", "conversation_key": current.scope, "attachments": [result["attachment"]],
                    "delivery_id": delivery_id, "artifact_version_id": version_id,
                })
        return {"status": "delivery_requested", "version_id": version_id, "event_id": event["event_id"], "message": "已提交发送；实际投递由平台确认。"}

    return router
