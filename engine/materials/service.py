from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .process import cancel_workers, release_owner, run_worker
from .storage import MAX_FILE_BYTES, MaterialError, MaterialStore, bounded_text, dump, identity, new_id, require_row, value


class MaterialService:
    def __init__(self, workspace_root: Path):
        self.store = MaterialStore(workspace_root)
        self.workspace = self.store.workspace
        self.worker_owner = new_id("materials_")
        self._worker_lock = threading.Lock()
        self._worker_owners = {self.worker_owner}
        self.closing = False
        self._recover_jobs()

    def _work(self, operation: str, *, job_id: str = "", **kwargs) -> dict:
        owner = f"{self.worker_owner}:{job_id}" if job_id else self.worker_owner
        with self._worker_lock:
            if self.closing:
                raise MaterialError("资料服务正在停止，请稍后重试。", "interrupted", 503)
            self._worker_owners.add(owner)
        return run_worker(self.workspace, operation, owner=owner, **kwargs)

    def _recover_jobs(self) -> None:
        with self.store.transaction():
            rows = self.store.db.execute("SELECT data FROM objects WHERE kind='job' AND json_extract(data,'$.status') IN ('queued','running')").fetchall()
            for row in rows:
                job = json.loads(row[0])
                version = self.store.db.execute("SELECT data FROM objects WHERE kind='version' AND json_extract(data,'$.metadata.job_id')=? AND scope=? AND bot_scope=? AND json_extract(data,'$.artifact_id')=?", (job["id"], job["scope"], job["bot_scope"], job["artifact_id"])).fetchone()
                if version:
                    recovered = json.loads(version[0])
                    try:
                        self.store.path(recovered["blob"], recovered["sha256"])
                        for page in recovered["preview_pages"]:
                            self.store.path(page["blob"], page["sha256"])
                    except MaterialError:
                        job.update(status="failed", error="已提交的成品或预览文件缺失，请重试本地生成。", error_code="file_missing")
                    else:
                        job.update(status="preview_ready", version_id=recovered["id"], error="")
                else:
                    job.update(status="interrupted", error="服务重启中断了本地生成。可以重试，不会自动发送成品。", error_code="interrupted")
                self.store.update(job)

    def interrupt_jobs(self) -> None:
        with self._worker_lock:
            self.closing = True
            owners = list(self._worker_owners)
        for owner in owners:
            cancel_workers(owner)
        with self.store.transaction():
            rows = self.store.db.execute("SELECT data FROM objects WHERE kind='job' AND json_extract(data,'$.status') IN ('queued','running')").fetchall()
            for row in rows:
                job = json.loads(row[0])
                job.update(status="interrupted", error="资料服务停止，生成已中断。可以重试本地生成。", error_code="interrupted")
                self.store.update(job)

    def close(self) -> None:
        self.store.close()
        for owner in self._worker_owners:
            release_owner(owner)

    def capabilities(self) -> dict:
        return self._work("capabilities")

    def _attachment(self, raw: str) -> Path:
        root = (self.workspace / "data" / "attachments").resolve()
        path = (self.workspace / raw).resolve()
        if root not in path.parents or not path.is_file():
            raise MaterialError("只能注册已上传到附件目录的文件。", "invalid_path", 403)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise MaterialError("文件超过 50 MB。", "limit_exceeded", 413)
        return path

    def register_source(self, actor: Any, body: dict) -> dict:
        path = self._attachment(bounded_text(body.get("path", ""), "附件路径", 1000, required=True))
        name = bounded_text(body.get("name") or path.name, "文件名", 200, required=True)
        content = path.read_bytes()
        blob, digest = self.store.blob(content, path.suffix)
        message_id = str(body.get("message_id") or value(actor, "message_id") or "")
        with self.store.transaction():
            for existing in self.store.list("source", actor, 200):
                if existing.get("sha256") == digest and existing.get("status") != "deleted" and existing["owner"] == value(actor, "owner") and existing.get("message_id") == message_id:
                    return self.public_source(existing)
            scope, _, bot = identity(actor)
            used = self.store.db.execute("SELECT COALESCE(SUM(json_extract(data,'$.size')),0) FROM objects WHERE kind='source' AND scope=? AND bot_scope=? AND json_extract(data,'$.status')!='deleted'", (scope, bot)).fetchone()[0]
            if used + len(content) > 512 * 1024 * 1024:
                raise MaterialError("当前会话资料超过 512 MB，请先删除不用的资料。", "quota_exceeded", 413)
            row = self.store.create("source", actor, {
                "name": name, "blob": blob, "sha256": digest, "size": len(content),
                "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "message_id": message_id,
                "source_time": body.get("source_time"), "status": "registered", "spans": [], "warnings": [],
            })
        return self.public_source(row)

    @staticmethod
    def public_source(row: dict) -> dict:
        return {key: item for key, item in row.items() if key not in {"blob", "spans", "owner", "bot_scope"}}

    def source(self, actor: Any, source_id: str, *, edit: bool = False) -> dict:
        row = self.store.get(source_id, actor, kind="source", edit=edit)
        if row["status"] == "deleted":
            raise MaterialError("资料已删除。", "not_found", 404)
        return row

    def list_sources(self, actor: Any) -> list[dict]:
        return [self.public_source(row) for row in self.store.list("source", actor) if row["status"] != "deleted"]

    def parse_source(self, actor: Any, source_id: str) -> dict:
        row = self.source(actor, source_id)
        if row["status"] == "parsed":
            return self.public_source(row)
        path = self.store.path(row["blob"], row["sha256"])
        result = self._work("parse", path=str(path))
        for span in result["spans"]:
            span["id"] = f"{source_id}:{span['index']}"
            span["source_id"] = source_id
            span["source_sha256"] = row["sha256"]
        with self.store.transaction():
            current = self.source(actor, source_id)
            current.update(result)
            current["status"] = "parsed"
            current["parsed_at"] = time.time()
            current["span_count"] = len(result["spans"])
            self.store.update(current)
        return self.public_source(current)

    def search_source(self, actor: Any, source_id: str, query: str = "", page: int = 0, limit: int = 20) -> dict:
        row = self.source(actor, source_id)
        if row["status"] != "parsed":
            self.parse_source(actor, source_id)
            row = self.source(actor, source_id)
        query = bounded_text(query, "检索词", 200)
        hits = []
        for span in row.get("spans", []):
            if page and span["locator"].get("page") != page:
                continue
            text = span["text"]
            position = text.casefold().find(query.casefold()) if query else 0
            if position < 0:
                continue
            start = max(0, position - 200)
            end = min(len(text), max(position + len(query) + 400, start + 1200))
            hits.append({**span, "text": text[start:end], "char_start": start, "char_end": end,
                         "citation": f"{row['name']} · {span['locator']['label']}", "truncated": start > 0 or end < len(text)})
        return {"source": self.public_source(row), "total": len(hits), "results": hits[:min(max(limit, 1), 50)], "warnings": row.get("warnings", [])}

    def cite(self, actor: Any, source_id: str, span_id: str, quote: str) -> dict:
        row = self.source(actor, source_id)
        quote = bounded_text(quote, "引用原文", 4000, required=True)
        span = next((item for item in row.get("spans", []) if item["id"] == span_id), None)
        if span is None or quote not in span["text"]:
            raise MaterialError("引用文字与指定原文位置不匹配，不能生成引用。", "citation_mismatch", 422)
        return {"source_id": source_id, "source_sha256": row["sha256"], "span_id": span_id,
                "quote": quote, "char_start": span["text"].index(quote), "locator": span["locator"],
                "label": f"{row['name']} · {span['locator']['label']}"}

    def record_message(self, actor: Any, body: dict) -> dict:
        config = self.store.settings(actor)
        if not config["journal_enabled"]:
            return {"recorded": False, "reason": "journal_disabled"}
        scope, owner, bot = identity(actor)
        message_id = bounded_text(body.get("message_id", ""), "平台消息 ID", 160, required=True)
        if not bool(value(actor, "is_admin", False)) and str(value(actor, "message_id") or "") != message_id:
            raise MaterialError("不能代替其他平台消息写入记录。", "forbidden", 403)
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > 200000:
            raise MaterialError("原消息须为不超过 200000 字的文字。", "limit_exceeded", 413)
        timestamp = body.get("timestamp")
        if type(timestamp) not in {int, float} or not 0 < timestamp <= time.time() + 300:
            raise MaterialError("原消息时间不合法。")
        direction = body.get("direction", "inbound")
        if direction not in {"inbound", "outbound"}:
            raise MaterialError("消息方向不合法。")
        attachments = []
        for item in (body.get("attachments") or [])[:30]:
            source_id = item if isinstance(item, str) else next((item.get(key) for key in ("source_id", "material_source_id", "id") if item.get(key)), "") if isinstance(item, dict) else ""
            if source_id:
                source = self.source(actor, source_id)
                attachments.append({"source_id": source["id"], "name": source["name"], "mime_type": source["mime_type"]})
        record = {"id": new_id("j_"), "message_id": message_id, "timestamp": float(timestamp),
                  "sender_name": bounded_text(body.get("sender_name", ""), "显示名", 100),
                  "text": text, "direction": direction, "reply_ref": str(body.get("reply_ref") or "")[:160],
                  "attachments": attachments, "scope": scope, "bot_scope": bot, "owner": owner}
        if len(dump(record)) > 300000:
            raise MaterialError("消息记录超过大小限制。", "limit_exceeded", 413)
        with self.store.transaction():
            self.store.db.execute("DELETE FROM journal WHERE bot_scope=? AND scope=? AND timestamp<?", (bot, scope, time.time() - config["journal_retention_days"] * 86400))
            self.store.db.execute("INSERT OR IGNORE INTO journal VALUES(?,?,?,?,?,?,?,?)", (
                record["id"], scope, bot, owner, message_id, timestamp, text, dump(record),
            ))
            saved = self.store.db.execute("SELECT data FROM journal WHERE bot_scope=? AND scope=? AND message_id=?", (bot, scope, message_id)).fetchone()
            existing = json.loads(saved[0])
            if attachments and existing["owner"] == owner:
                merged = {item["source_id"]: item for item in existing.get("attachments", [])}
                merged.update({item["source_id"]: item for item in attachments})
                existing["attachments"] = list(merged.values())
                self.store.db.execute("UPDATE journal SET data=? WHERE id=?", (dump(existing), existing["id"]))
        return {"recorded": True, **self.public_message(existing)}

    @staticmethod
    def public_message(row: dict) -> dict:
        return {key: item for key, item in row.items() if key not in {"owner", "bot_scope"}}

    def search_messages(self, actor: Any, query: str = "", since: float = 0, until: float = 0, sender: str = "", limit: int = 30) -> dict:
        scope, _, bot = identity(actor)
        config = self.store.settings(actor)
        if not config["journal_enabled"]:
            return {"enabled": False, "results": [], "message": "当前会话尚未开启原消息记录；不能从压缩摘要还原原话。"}
        query = bounded_text(query, "检索词", 200)
        minimum = time.time() - config["journal_retention_days"] * 86400
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT data FROM journal WHERE bot_scope=? AND scope=? AND timestamp>=? AND timestamp<=? AND text LIKE ? ESCAPE '\\' ORDER BY timestamp DESC LIMIT 500",
                (bot, scope, max(float(since or 0), minimum), float(until or time.time()), pattern),
            ).fetchall()
        found = [json.loads(row[0]) for row in rows]
        if sender:
            found = [row for row in found if sender.casefold() in row["sender_name"].casefold()]
        results = []
        for row in found[:min(max(limit, 1), 50)]:
            item = self.public_message(row)
            position = item["text"].casefold().find(query.casefold()) if query else 0
            start = max(0, position - 200)
            item["text"] = item["text"][start:start + 2000]
            item["char_start"] = start
            item["truncated"] = len(row["text"]) != len(item["text"])
            results.append(item)
        return {"enabled": True, "results": results,
                "limit": min(max(limit, 1), 50), "retention_days": config["journal_retention_days"], "search_capped": len(rows) == 500}

    def get_message(self, actor: Any, reference: str) -> dict:
        scope, _, bot = identity(actor)
        config = self.store.settings(actor)
        if not config["journal_enabled"]:
            raise MaterialError("当前会话的原消息记录已关闭。", "forbidden", 403)
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT data FROM journal WHERE bot_scope=? AND scope=? AND (id=? OR message_id=?) AND timestamp>=?",
                (bot, scope, reference, reference, time.time() - config["journal_retention_days"] * 86400),
            ).fetchone()
        if not row:
            raise MaterialError("授权记录中未找到原消息，可能尚未记录、已删除或已过保留期。", "not_found", 404)
        return self.public_message(json.loads(row[0]))

    def create_extraction(self, actor: Any, body: dict) -> dict:
        source = self.source(actor, str(body.get("source_id") or ""))
        if not source["mime_type"].startswith("image/"):
            raise MaterialError("结构化截图提取需要图片资料。")
        kind = body.get("kind", "table")
        if kind not in {"table", "todos", "error"}:
            raise MaterialError("提取类型须为 table/todos/error。")
        fields = self._fields(body.get("fields"))
        return self.store.create("extraction", actor, {"source_id": source["id"], "source_sha256": source["sha256"],
            "extraction_kind": kind, "fields": fields, "revision": 1, "status": "needs_review", "revisions": [],
            "provenance": "vision_draft", "message": "这是基于截图的待校对结果；请逐项核对模糊文字、数值和日期。"})

    @staticmethod
    def _fields(raw: Any) -> list[dict]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= 500:
            raise MaterialError("请提供 1–500 个识别字段。")
        result = []
        seen = set()
        for index, field in enumerate(raw, 1):
            if not isinstance(field, dict):
                raise MaterialError("识别字段格式错误。")
            field_id = bounded_text(str(field.get("id") or f"f{index}"), "字段 ID", 80, required=True)
            if field_id in seen:
                raise MaterialError("字段 ID 不能重复。")
            seen.add(field_id)
            box = field.get("bbox")
            if box is not None and (not isinstance(box, list) or len(box) != 4 or any(type(n) not in {int, float} or not 0 <= n <= 1 for n in box) or box[0] >= box[2] or box[1] >= box[3]):
                raise MaterialError("定位框须为 0–1 范围的 [左,上,右,下]。")
            result.append({"id": field_id, "label": bounded_text(str(field.get("label") or field_id), "字段名", 120),
                           "value": bounded_text(str(field["value"]) if field.get("value") is not None else "", "识别文字", 4000),
                           "bbox": box, "uncertain": bool(field.get("uncertain", True)), "corrected": False})
            for key in ("row", "column"):
                if key in field:
                    if type(field[key]) is not int or not 1 <= field[key] <= 1000:
                        raise MaterialError("表格行列位置须在 1–1000 之间。")
                    result[-1][key] = field[key]
        return result

    def correct_extraction(self, actor: Any, extraction_id: str, body: dict) -> dict:
        with self.store.transaction():
            record = self.store.get(extraction_id, actor, kind="extraction", edit=True)
            if body.get("expected_revision") != record["revision"]:
                raise MaterialError("校对版本已变化，请刷新后再改。", "version_conflict", 409)
            updates = body.get("values", {})
            if not isinstance(updates, dict) or not set(updates).issubset({field["id"] for field in record["fields"]}):
                raise MaterialError("校对包含不存在的字段。")
            record["revisions"].append({"revision": record["revision"], "fields": record["fields"]})
            record["fields"] = [dict(field) for field in record["fields"]]
            for field in record["fields"]:
                if field["id"] in updates:
                    field.update(value=bounded_text(str(updates[field["id"]]), "校对文字", 4000), uncertain=False, corrected=True)
            record["revision"] += 1
            record["status"] = "reviewed" if body.get("confirm") is True else "needs_review"
            self.store.update(record)
        return record

    def create_job(self, actor: Any, body: dict) -> dict:
        if self.closing:
            raise MaterialError("资料服务正在停止。", "interrupted", 503)
        kind = body.get("format")
        if kind not in {"pdf", "docx", "xlsx", "pptx"} or not isinstance(body.get("spec"), dict):
            raise MaterialError("请选择 pdf/docx/xlsx/pptx 并提供结构化 spec。")
        if len(dump(body["spec"])) > 500000:
            raise MaterialError("生成规格过大。", "limit_exceeded", 413)
        with self.store.transaction():
            active = [job for job in self.store.list("job", actor, 200) if job["owner"] == str(value(actor, "owner")) and job["status"] in {"queued", "running"}]
            if len(active) >= 2:
                raise MaterialError("已有两个资料任务在处理，请完成或取消后再提交。", "busy", 429)
            if body.get("artifact_id"):
                artifact = self.store.get(str(body["artifact_id"]), actor, kind="artifact", edit=True)
                if body.get("expected_version") != artifact.get("current_version"):
                    raise MaterialError("资料版本已变化，请刷新后再生成。", "version_conflict", 409)
            else:
                artifact = self.store.create("artifact", actor, {"name": bounded_text(str(body["spec"].get("title") or "未命名资料"), "标题", 160), "current_version": "", "versions": []})
            return self.store.create("job", actor, {"status": "queued", "artifact_id": artifact["id"],
                "expected_version": artifact["current_version"], "format": kind, "spec": body["spec"], "error": ""})

    def run_job(self, actor: Any, job_id: str) -> dict:
        with self.store.transaction():
            job = self.store.get(job_id, actor, kind="job", edit=True)
            if job["status"] != "queued":
                return job
            job["status"] = "running"
            self.store.update(job)
        directory = self.store.root / "jobs" / job_id
        try:
            result = self._work("build", job_id=job_id, format=job["format"], spec=job["spec"], directory=str(directory))
            path = Path(result["path"])
            version = self._save_version(actor, job["artifact_id"], path, result["preview"],
                expected=job["expected_version"], metadata={"operation": "generate", "spec": job["spec"], "job_id": job_id}, job_id=job_id)
            job.update(status="preview_ready", version_id=version["id"])
        except MaterialError as exc:
            job.update(status="failed", error=str(exc), error_code=exc.code)
        except Exception as exc:
            job.update(status="failed", error=f"生成失败：{type(exc).__name__}", error_code="processing_failed")
        with self.store.transaction():
            current = self.store.get(job_id, actor, kind="job")
            if current["status"] in {"cancelled", "interrupted", "preview_ready"}:
                return current
            self.store.update(job)
        return job

    def _save_version(self, actor: Any, artifact_id: str, path: Path, preview: dict, *, expected: str, metadata: dict, job_id: str = "") -> dict:
        blob, digest = self.store.blob(path.read_bytes(), path.suffix)
        pages = []
        for raw in preview["pages"]:
            page_blob, page_digest = self.store.blob(Path(raw).read_bytes(), ".png")
            pages.append({"blob": page_blob, "sha256": page_digest})
        with self.store.transaction():
            if metadata.get("generation_key"):
                scope, owner, bot = identity(actor)
                existing = self.store.db.execute("SELECT data FROM objects WHERE kind='version' AND scope=? AND bot_scope=? AND owner=? AND json_extract(data,'$.metadata.generation_key')=?", (scope, bot, owner, metadata["generation_key"])).fetchone()
                if existing:
                    return json.loads(existing[0])
            artifact = self.store.get(artifact_id, actor, kind="artifact", edit=True)
            if artifact["current_version"] != expected:
                raise MaterialError("生成期间资料版本已变化，未覆盖新版本。", "version_conflict", 409)
            if job_id and self.store.get(job_id, actor, kind="job")["status"] in {"cancelled", "interrupted"}:
                raise MaterialError("任务已取消。", "cancelled", 409)
            if len(artifact["versions"]) >= 100:
                raise MaterialError("同一作品最多 100 个版本。", "limit_exceeded", 413)
            version = self.store.create("version", actor, {"artifact_id": artifact_id, "number": len(artifact["versions"]) + 1,
                "parent_version": expected, "blob": blob, "sha256": digest, "format": path.suffix.lstrip("."),
                "size": path.stat().st_size, "preview_pages": pages, "status": "preview_ready", "metadata": metadata,
                "preview_inspected": False, "approved_hash": "", "delivery_id": ""})
            artifact["versions"].append(version["id"])
            artifact["current_version"] = version["id"]
            self.store.update(artifact)
            if job_id:
                job = self.store.get(job_id, actor, kind="job")
                job.update(status="preview_ready", version_id=version["id"], error="")
                self.store.update(job)
        return version

    def register_image_version(self, actor: Any, body: dict) -> dict:
        source = self.source(actor, str(body.get("source_id") or ""))
        if not source["mime_type"].startswith("image/"):
            raise MaterialError("图像版本需要图片资料。")
        if body.get("artifact_id"):
            artifact = self.store.get(str(body["artifact_id"]), actor, kind="artifact", edit=True)
            expected = body.get("expected_version")
            if expected != artifact["current_version"]:
                raise MaterialError("图像版本已变化，请刷新。", "version_conflict", 409)
        else:
            artifact = self.store.create("artifact", actor, {"name": source["name"], "current_version": "", "versions": []})
            expected = ""
        base = str(body.get("base_version") or expected)
        if base and base not in artifact["versions"]:
            raise MaterialError("基准版本不属于当前作品。")
        path = self.store.path(source["blob"], source["sha256"])
        preview = self._work("preview", path=str(path), directory=str(self.store.root / "jobs" / new_id("preview_")))
        return self.public_version(self._save_version(actor, artifact["id"], path, preview, expected=expected,
            metadata={"operation": "image_edit", "source_id": source["id"], "base_version": base,
                      "instruction": bounded_text(str(body.get("instruction") or ""), "修改说明", 4000)}))

    def register_generated_images(self, actor: Any, body: dict) -> dict:
        from PIL import Image

        roots = {"gpt_image_2": "gpt_image", "gemini_image": "gemini_image", "nai_generate": "novelai", "nai_generate_from_plan": "novelai"}
        tool = body.get("tool_name")
        if tool not in roots or not value(actor, "task_id"):
            raise MaterialError("生成图归档必须来自实际执行任务。", "forbidden", 403)
        attachments = body.get("attachments")
        if not isinstance(attachments, list) or not 1 <= len(attachments) <= 16:
            raise MaterialError("生成图归档数量不合法。")
        image_paths = body.get("image_paths") or []
        if isinstance(image_paths, str):
            image_paths = [image_paths]
        if not isinstance(image_paths, list) or len(image_paths) > 16:
            raise MaterialError("参考图列表不合法。")
        scope, owner, bot = identity(actor)
        output_root = (self.workspace / "data" / "attachments" / roots[tool]).resolve()
        versions = []
        for item in attachments:
            if not isinstance(item, dict):
                raise MaterialError("生成图附件格式错误。")
            path = self._attachment(str(item.get("path") or ""))
            if output_root not in path.parents:
                raise MaterialError("该文件不是指定生图工具的输出。", "forbidden", 403)
            with Image.open(path) as image:
                if image.width * image.height > 40_000_000:
                    raise MaterialError("生成图像素超过限制。", "limit_exceeded", 413)
                image.verify()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            generation_key = hashlib.sha256(dump([value(actor, "task_id"), body.get("tool_call_id"), tool, digest]).encode()).hexdigest()
            with self.store.lock:
                rows = self.store.db.execute("SELECT data FROM objects WHERE kind='version' AND scope=? AND bot_scope=? AND owner=?", (scope, bot, owner)).fetchall()
            prior_versions = [json.loads(row[0]) for row in rows]
            existing = next((version for version in prior_versions if version.get("metadata", {}).get("generation_key") == generation_key), None)
            if existing:
                versions.append(self.public_version(existing))
                continue
            base_version = str(body.get("base_version") or "")
            artifact_id = str(body.get("artifact_id") or "")
            if not base_version:
                for reference in image_paths:
                    reference_path = (self.workspace / str(reference)).resolve()
                    base = next((version for version in prior_versions if (self.store.root / version["blob"]).resolve() == reference_path), None)
                    if base is not None:
                        base_version, artifact_id = base["id"], base["artifact_id"]
                        break
            if artifact_id:
                artifact = self.store.get(artifact_id, actor, kind="artifact", edit=True)
                if base_version and base_version not in artifact["versions"]:
                    raise MaterialError("参考版本不属于指定作品。")
                expected = body.get("expected_version") or base_version or artifact["current_version"]
                if expected != artifact["current_version"]:
                    raise MaterialError("生图期间作品版本已变化，未覆盖新版本；请重试归档，不必再次生图。", "version_conflict", 409)
            else:
                artifact = self.store.create("artifact", actor, {"name": bounded_text(str(item.get("name") or "生成图片"), "作品名", 160), "current_version": "", "versions": []})
                expected = ""
            source = self.register_source(actor, {"path": path.relative_to(self.workspace).as_posix(), "name": item.get("name") or path.name})
            preview = self._work("preview", path=str(path), directory=str(self.store.root / "jobs" / new_id("preview_")))
            saved = self._save_version(actor, artifact["id"], path, preview, expected=expected, metadata={
                "operation": "image_generate", "source_id": source["id"], "base_version": base_version,
                "tool": tool, "generation_key": generation_key, "task_id": value(actor, "task_id"),
                "generated_path": path.relative_to(self.workspace).as_posix(),
                "instruction": bounded_text(str(body.get("instruction") or ""), "修改说明", 4000),
                "parameters": body.get("parameters") or {}, "result_metadata": body.get("result_metadata") or {},
            })
            versions.append(self.public_version(saved))
        return {"versions": versions}

    def begin_image_operation(self, actor: Any, body: dict) -> dict:
        from .integration import IMAGE_TOOLS

        if body.get("tool_name") not in IMAGE_TOOLS or not value(actor, "task_id"):
            raise MaterialError("图片任务来源不合法。", "forbidden", 403)
        scope, owner, bot = identity(actor)
        prepared = {key: body.get(key) for key in ("tool_name", "tool_call_id", "artifact_id", "base_version", "expected_version", "instruction", "image_paths")}
        from .integration import GENERATION_FIELDS
        parameters = body.get("parameters") or {}
        if not isinstance(parameters, dict) or len(dump(parameters)) > 20000:
            raise MaterialError("图片参数超过大小限制。")
        prepared["parameters"] = {key: parameters[key] for key in GENERATION_FIELDS if key in parameters}
        references = prepared["image_paths"] or []
        if isinstance(references, str):
            references = [references]
        if not isinstance(references, list) or len(references) > 16:
            raise MaterialError("参考图列表不合法。")
        prepared["image_paths"] = references
        if not prepared["base_version"]:
            with self.store.lock:
                rows = self.store.db.execute("SELECT data FROM objects WHERE kind='version' AND scope=? AND bot_scope=? AND owner=? ORDER BY created DESC", (scope, bot, owner)).fetchall()
            paths = {(self.workspace / str(path)).resolve() for path in references}
            for raw in rows:
                version = json.loads(raw[0])
                generated = version.get("metadata", {}).get("generated_path")
                if (self.store.root / version["blob"]).resolve() in paths or generated and (self.workspace / generated).resolve() in paths:
                    prepared["base_version"], prepared["artifact_id"] = version["id"], version["artifact_id"]
                    break
        if prepared["artifact_id"]:
            artifact = self.store.get(prepared["artifact_id"], actor, kind="artifact", edit=True)
            base = prepared["base_version"] or artifact["current_version"]
            expected = prepared["expected_version"] or base
            if base not in artifact["versions"] or expected != artifact["current_version"]:
                raise MaterialError("参考版本与当前版本有变化，请刷新后再开始生图。", "version_conflict", 409)
            prepared.update(base_version=base, expected_version=expected)
        bound_actor = {key: value(actor, key) for key in ("scope", "owner", "bot_scope", "is_admin", "role", "channel", "task_id", "message_id")}
        row = self.store.create("image_operation", actor, {"status": "pending", "task_id": value(actor, "task_id"),
            "bound_actor": bound_actor, "request": prepared, "expires_at": time.time() + 3600, "versions": []})
        return {"id": row["id"], "status": row["status"]}

    def complete_image_operation(self, operation_id: str, task_id: str, body: dict) -> dict:
        with self.store.lock:
            raw = self.store.db.execute("SELECT data FROM objects WHERE id=? AND kind='image_operation'", (operation_id,)).fetchone()
        if raw is None:
            raise MaterialError("图片归档操作不存在。", "not_found", 404)
        operation = json.loads(raw[0])
        if not task_id or task_id != operation["task_id"]:
            raise MaterialError("图片归档操作不属于当前执行任务。", "forbidden", 403)
        if operation["status"] == "completed":
            return {"versions": operation["versions"]}
        if operation["expires_at"] < time.time():
            raise MaterialError("图片归档操作已过期，请由用户重试归档，勿重新生图。", "operation_expired", 409)
        actor = SimpleNamespace(**operation["bound_actor"])
        from .integration import GENERATION_FIELDS
        metadata = body.get("result_metadata") or {}
        if not isinstance(metadata, dict) or len(dump(metadata)) > 20000:
            raise MaterialError("图片结果元数据超过大小限制。")
        result = self.register_generated_images(actor, {**operation["request"], "attachments": body.get("attachments"),
            "result_metadata": {key: metadata[key] for key in GENERATION_FIELDS if key in metadata}})
        operation.update(status="completed", versions=result["versions"])
        self.store.update(operation)
        return result

    def restore_version(self, actor: Any, artifact_id: str, body: dict) -> dict:
        with self.store.transaction():
            artifact = self.store.get(artifact_id, actor, kind="artifact", edit=True)
            if body.get("expected_version") != artifact["current_version"]:
                raise MaterialError("当前版本已变化，请刷新。", "version_conflict", 409)
            original = self.store.get(str(body.get("version_id") or ""), actor, kind="version")
            if original["artifact_id"] != artifact_id:
                raise MaterialError("恢复版本不属于当前作品。")
            if len(artifact["versions"]) >= 100:
                raise MaterialError("同一作品最多 100 个版本。", "limit_exceeded", 413)
            self.store.path(original["blob"], original["sha256"])
            content = {key: item for key, item in original.items() if key not in {"id", "kind", "owner", "scope", "bot_scope", "created"}}
            content.update(number=len(artifact["versions"]) + 1, parent_version=artifact["current_version"],
                           status="preview_ready", approved_hash="", delivery_id="", preview_inspected=False,
                           metadata={"operation": "restore", "restored_from": original["id"]})
            restored = self.store.create("version", actor, content)
            artifact["current_version"] = restored["id"]
            artifact["versions"].append(restored["id"])
            self.store.update(artifact)
        return self.public_version(restored)

    @staticmethod
    def public_version(row: dict) -> dict:
        return {**{key: item for key, item in row.items() if key not in {"blob", "preview_pages", "owner", "bot_scope"}}, "page_count": len(row["preview_pages"])}

    def artifact(self, actor: Any, artifact_id: str) -> dict:
        artifact = self.store.get(artifact_id, actor, kind="artifact")
        return {**artifact, "versions": [self.public_version(self.store.get(version, actor, kind="version")) for version in artifact["versions"]]}

    def preview_file(self, actor: Any, version_id: str, page: int) -> Path:
        version = self.store.get(version_id, actor, kind="version")
        if page < 1 or page > len(version["preview_pages"]):
            raise MaterialError("预览页不存在。", "not_found", 404)
        item = version["preview_pages"][page - 1]
        return self.store.path(item["blob"], item["sha256"])

    def approve_version(self, actor: Any, version_id: str, body: dict) -> dict:
        with self.store.transaction():
            version = self.store.get(version_id, actor, kind="version", edit=True)
            artifact = self.store.get(version["artifact_id"], actor, kind="artifact")
            if artifact["current_version"] != version_id or body.get("sha256") != version["sha256"]:
                raise MaterialError("预览与当前版本不一致，请重新预览。", "version_conflict", 409)
            if body.get("preview_inspected") is not True:
                raise MaterialError("请先查看全部预览页，再确认此版本。", "preview_required", 409)
            self.store.path(version["blob"], version["sha256"])
            version.update(status="approved", approved_hash=version["sha256"], preview_inspected=True)
            self.store.update(version)
        return self.public_version(version)

    def export_version(self, actor: Any, version_id: str) -> dict:
        with self.store.transaction():
            version = self.store.get(version_id, actor, kind="version", edit=True)
            artifact = self.store.get(version["artifact_id"], actor, kind="artifact")
            if version["status"] not in {"approved", "delivered"} or version["approved_hash"] != version["sha256"] or artifact["current_version"] != version_id:
                raise MaterialError("请先预览并确认当前版本，才能发送或下载成品。", "approval_required", 409)
            path = self.store.path(version["blob"], version["sha256"])
            export = self.workspace / "data" / "attachments" / "materials" / version_id / ("document." + version["format"])
            export.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, export)
            attachment = {"type": "image" if version["format"] in {"png", "jpg", "jpeg", "webp", "gif"} else "file",
                          "path": export.relative_to(self.workspace).as_posix(), "mime_type": mimetypes.guess_type(export.name)[0] or "application/octet-stream",
                          "name": artifact["name"] + "." + version["format"]}
        return {"version_id": version_id, "sha256": version["sha256"], "attachment": attachment}

    def preview_attachments(self, actor: Any, version_id: str) -> dict:
        version = self.store.get(version_id, actor, kind="version")
        paths = []
        for index in range(1, len(version["preview_pages"]) + 1):
            source = self.preview_file(actor, version_id, index)
            target = self.workspace / "data" / "attachments" / "materials" / version_id / f"preview-{index}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            paths.append({"type": "image", "path": target.relative_to(self.workspace).as_posix(), "mime_type": "image/png", "name": f"预览第{index}页.png"})
        return {"version": self.public_version(version), "attachments": paths}

    def cancel_job(self, actor: Any, job_id: str) -> dict:
        with self.store.transaction():
            job = self.store.get(job_id, actor, kind="job", edit=True)
            if job["status"] in {"queued", "running"}:
                job["status"] = "cancelled"
                self.store.update(job)
                owner = f"{self.worker_owner}:{job_id}"
                with self._worker_lock:
                    self._worker_owners.add(owner)
                cancel_workers(owner)
        return job

    def retry_job(self, actor: Any, job_id: str) -> dict:
        original = self.store.get(job_id, actor, kind="job", edit=True)
        if original["status"] not in {"failed", "interrupted", "cancelled"}:
            raise MaterialError("只有失败或已中断的本地生成任务可以重试。", "invalid_state", 409)
        return self.create_job(actor, {"format": original["format"], "spec": original["spec"],
            "artifact_id": original["artifact_id"], "expected_version": original["expected_version"]})
