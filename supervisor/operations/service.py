from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from providers import registry
from clonoth_runtime import qq_config_path, resolve_env_ref
from supervisor.config_store import ConfigStore
from supervisor.instances import load_instances, url_prefix
from supervisor.log_access import LogReader, redact


def _read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class OperationsService:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.root = Path(state.workspace_root)
        self.reports = self.root / "data" / "diagnostics"
        self.qq_file = qq_config_path(self.root)
        self._probe_lock = asyncio.Lock()

    def _configured_secrets(self) -> tuple[str, ...]:
        values: set[str] = set()

        def collect(item: Any, sensitive: bool = False) -> None:
            if isinstance(item, dict):
                for key, value in item.items():
                    collect(value, sensitive or bool(re.search(r"token|secret|password|api.?key|authorization", str(key), re.I)))
            elif isinstance(item, list):
                for value in item:
                    collect(value, sensitive)
            elif sensitive and isinstance(item, str):
                resolved = resolve_env_ref(item)
                if resolved:
                    values.add(resolved)

        for path in (self.root / "data/config.yaml", self.qq_file):
            try:
                collect(yaml.safe_load(path.read_text(encoding="utf-8")))
            except (OSError, yaml.YAMLError):
                continue
        for key, value in os.environ.items():
            if re.search(r"(?:TOKEN|SECRET|PASSWORD|API_?KEY)$", key, re.I) and value:
                values.add(value)
        return tuple(sorted(values, key=len, reverse=True))

    def _clean(self, value: Any, secrets: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            return {
                self._clean(str(key), secrets): ("[已隐藏]" if re.search(r"token|secret|password|api.?key|authorization", str(key), re.I)
                      else self._clean(item, secrets))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._clean(item, secrets) for item in value]
        if isinstance(value, str):
            text = value.replace(str(self.root), "<workspace>")
            for secret in secrets:
                if secret:
                    text = text.replace(secret, "[已隐藏]")
            text = re.sub(r"(?i)\b(https?://)[^/\s@]+@", r"\1[已隐藏]@", text)
            return redact(text)
        return value

    @staticmethod
    def _probe_options(provider_class: Any) -> dict[str, Any]:
        options = {spec.key: spec for spec in getattr(provider_class, "OPTIONS", ())}
        for key in ("max_completion_tokens", "max_output_tokens", "max_tokens"):
            if key not in options:
                continue
            spec = options[key]
            limit = max(32, int(spec.minimum or 0))
            if spec.maximum is not None:
                limit = min(limit, int(spec.maximum))
            if not 1 <= limit <= 1024:
                break
            return {key: limit}
        raise ValueError("当前渠道未声明可用的低额度输出上限，未发起诊断模型请求")

    def status(self) -> dict[str, Any]:
        live = _read_json(self.root / "data" / "qq_live_state.json")
        runtime = live.get("runtime") if isinstance(live.get("runtime"), dict) else {}
        try:
            published = float(live.get("published_at") or 0)
        except (ValueError, TypeError):
            published = 0
        age = time.time() - published
        fresh = bool(published and math.isfinite(age) and 0 <= age < 90)
        reported_path = str(live.get("config_path") or "")
        path_mismatch = bool(reported_path and Path(reported_path).resolve() != self.qq_file.resolve())
        manager = getattr(self.state, "feature_process_manager", None)
        workers = manager.worker_health() if manager is not None else {}
        workers = {name: {key: value for key, value in worker.items() if key != "last_log"} for name, worker in workers.items()}
        with self.state._lock:
            active_tasks = sum(not self.state._task_terminal(task) for task in self.state.tasks.values())
        try:
            stat = self.qq_file.stat()
            fingerprint = {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        except OSError:
            fingerprint = {"exists": False, "mtime_ns": 0, "size": 0}
        return {
            "instance_prefix": url_prefix(), "supervisor": "online",
            "adapter_online": fresh,
            "onebot_connected": fresh and runtime.get("onebot_connected") is True,
            "heartbeat_age": round(age, 1) if published and math.isfinite(age) and age >= 0 else None,
            "workers": self._clean(workers),
            "active_tasks": active_tasks,
            "queue_pending": int((runtime.get("volatile") or {}).get("queue_pending") or 0),
            "config_applied": fresh and not path_mismatch and live.get("loaded") == fingerprint,
            "config_path_mismatch": path_mismatch,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def instances(self) -> list[dict[str, Any]]:
        rows = load_instances(self.root)
        if not rows:
            rows = [{"uin": "current", "label": "当前实例", "path": url_prefix(), "idx": 0}]
        return [{**row, "current": row["path"] == url_prefix()} for row in rows]

    async def diagnose(self, *, test_model: bool = False, include_logs: bool = False) -> dict[str, Any]:
        if self._probe_lock.locked():
            raise ValueError("诊断正在进行，请等待当前检查完成")
        async with self._probe_lock:
            report_id = uuid.uuid4().hex
            checks: list[dict[str, Any]] = []
            facts = self.status()

            def add(name: str, status: str, detail: str, action: str = "", **extra: Any) -> None:
                checks.append({"name": name, "status": status, "detail": detail, "action": action, **extra})

            add("Supervisor", "passed", "管理服务正常响应")
            add("QQ适配器", "passed" if facts["adapter_online"] else "failed",
                "心跳正常" if facts["adapter_online"] else "未收到新鲜心跳", "检查Bot进程和Supervisor地址")
            add("OneBot连接", "passed" if facts["onebot_connected"] else "failed",
                "已连接" if facts["onebot_connected"] else "尚未确认连接", "检查NapCat登录与反向WebSocket地址")
            if facts["config_path_mismatch"]:
                add("QQ配置路径", "failed", "Supervisor与Bot使用的QQ配置文件不同", "统一两个进程的CLONOTH_QQ_CONFIG_PATH后重新检查")
            workers = facts["workers"]
            add("Engine", "passed" if workers and all(w.get("alive") for w in workers.values()) else "warning",
                f"已登记 {len(workers)} 个worker" if workers else "当前进程未提供worker管理信息", "查看运行页面")
            disk = shutil.disk_usage(self.root)
            add("存储空间", "passed" if disk.free > 512 * 1024 * 1024 else "warning",
                f"可用 {disk.free // (1024 * 1024)} MiB")
            store = getattr(self.state, "feature_config_store", None) or ConfigStore(path=self.root / "data" / "config.yaml")
            secret = store.get_openai_secret()
            protected = (*self._configured_secrets(), secret.api_key)
            provider_class = registry.get(secret.provider)
            configured = bool(provider_class and secret.model and secret.api_key)
            add("模型配置", "passed" if configured else "failed",
                f"渠道 {secret.provider}，模型 {secret.model or '未配置'}",
                "检查渠道模型与密钥配置" if not configured else "")
            if test_model and configured:
                started = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=20, trust_env=False) as http:
                        provider = provider_class(
                            http=http, api_key=secret.api_key, base_url=secret.base_url, model=secret.model,
                            provider_options=self._probe_options(provider_class),
                        )
                        response = await asyncio.wait_for(provider.chat(
                            messages=[{"role": "user", "content": "Connection check. Reply OK."}], tools=None,
                        ), timeout=20)
                        if not response.ok or not str(response.text or "").strip():
                            raise RuntimeError(str(response.error or "模型未返回有效文本"))
                    add("模型响应", "passed", "最小请求已完成", latency_ms=round((time.monotonic() - started) * 1000))
                except Exception as error:
                    add("模型响应", "failed", self._clean(str(error), protected), "检查渠道地址、认证、配额和模型可用性")
            else:
                add("模型响应", "skipped", "未执行付费模型请求" if not test_model else "模型配置不完整")
            materials = getattr(self.state, "materials", None)
            if materials is not None and hasattr(materials, "capabilities"):
                try:
                    capabilities = await asyncio.to_thread(materials.capabilities)
                    modules = capabilities.get("modules") or {}
                    ready = bool(modules and all(modules.values()) and capabilities.get("office_renderer"))
                    add("文档处理", "passed" if ready else "warning",
                        "解析与渲染依赖可用" if ready else "部分解析或Office渲染依赖未就绪",
                        "在资料工作区查看缺失依赖，按部署说明安装", capabilities=capabilities)
                except Exception as error:
                    add("文档处理", "failed", self._clean(str(error), protected), "检查资料处理依赖与工作进程")
            report = {
                "id": report_id, "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "failed" if any(c["status"] == "failed" for c in checks) else (
                    "warning" if any(c["status"] == "warning" for c in checks) else "passed"
                ),
                "instance_prefix": url_prefix(), "checks": checks,
            }
            if include_logs:
                reader = LogReader(self.root / "data" / "logs")
                report["logs"] = {
                    entry.name: (reader.tail(entry.name, lines=80) or ("", False))[0]
                    for entry in reader.list()[:5]
                }
            report = self._clean(report, protected)
            _atomic(self.reports / f"{report_id}.json", json.dumps(report, ensure_ascii=False, indent=2))
            for expired in sorted(self.reports.glob("*.json"), key=lambda path: path.stat().st_mtime)[:-50]:
                expired.unlink(missing_ok=True)
            return report

    def get_report(self, report_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", report_id):
            raise ValueError("报告不存在")
        report = _read_json(self.reports / f"{report_id}.json")
        if not report:
            raise ValueError("报告不存在")
        return report
