from __future__ import annotations

import asyncio
import json
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

import config_layout
from providers import registry
from plugin_runtime import plugin_scope
from clonoth_runtime import load_platform_secrets, platform_secret, resolve_env_ref, stray_platforms_section
from platforms.descriptor import PlatformDescriptor
from platforms.registry import all_platforms, current_platform, platform_config_path
from supervisor.config_store import ConfigStore
from supervisor.instances import load_instances, url_prefix
from supervisor.log_access import LogReader, redact
from supervisor.platform_api import platform_health

# 平台没声明自己的排查提示时用的说法。
_CONNECTION_HINT = "检查平台账号登录和适配进程的连接配置"


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


def _mismatch_action(platform: PlatformDescriptor) -> str:
    if platform.config_env:
        return f"统一两个进程的 {platform.config_env} 后重新检查"
    return "确认两个进程用的是同一个工作区后重新检查"


class OperationsService:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.root = Path(state.workspace_root)
        self.reports = self.root / "data" / "diagnostics"
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

        # 搬家后旧位置那几份还留着给退回旧版本用，里面可能是没轮换掉的旧密钥，一并抹。
        config_files = [
            config_layout.models_path(self.root),
            config_layout.platform_secrets_path(self.root),
            config_layout.mcp_path(self.root),
            config_layout.drawtools_file(self.root, "settings.yaml"),
            self.root / config_layout.LEGACY_MODELS_FILE,
            self.root / config_layout.LEGACY_MCP_FILE,
        ]
        for platform in all_platforms():
            path = platform_config_path(platform, self.root)
            if path is not None and path not in config_files:
                config_files.append(path)
        for path in config_files:
            try:
                collect(yaml.safe_load(path.read_text(encoding="utf-8")))
            except (OSError, yaml.YAMLError):
                continue
        # 平台声明的密钥不一定叫 token/secret，按声明再收一遍；被密钥文件盖住的那个 env 值也收。
        # 自动生成的密钥文件不收：代码从不打印它们，里面也可能只是模式标记，抹掉反而丢排障线索。
        platform_secrets = load_platform_secrets(self.root)
        for platform in all_platforms():
            for spec in platform.secrets:
                values.add(platform_secret(self.root, platform, spec.name, secrets=platform_secrets).value)
                if spec.env:
                    values.add(os.environ.get(spec.env, "").strip())
        # 共享密钥文件不灌进进程环境，得单独收一遍。
        shared = config_layout.shared_secrets(self.root)
        for source in (os.environ, shared):
            for key, value in source.items():
                if re.search(r"(?:TOKEN|SECRET|PASSWORD|API_?KEY)$", key, re.I) and value:
                    values.add(value)
        values.discard("")
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

    def _platform_health(self) -> list[tuple[PlatformDescriptor, dict[str, Any]]]:
        platform = current_platform()
        return [(platform, platform_health(self.root, platform))]

    def status(self) -> dict[str, Any]:
        manager = getattr(self.state, "feature_process_manager", None)
        workers = manager.worker_health() if manager is not None else {}
        workers = {name: {key: value for key, value in worker.items() if key != "last_log"} for name, worker in workers.items()}
        with self.state._lock:
            active_tasks = sum(not self.state._task_terminal(task) for task in self.state.tasks.values())
        return {
            "instance_prefix": url_prefix(), "supervisor": "online",
            "platforms": [health for _, health in self._platform_health()],
            "workers": self._clean(workers),
            "active_tasks": active_tasks,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _platform_checks(add: Any, platform: PlatformDescriptor, health: dict[str, Any]) -> None:
        name = platform.display_name
        adapter = f"{name} 适配进程"
        if not health["published"]:
            # 一个实例只跑这一个平台，它从没公布过状态就是 bot 进程没起来或连不上 Supervisor。
            add(adapter, "failed", "还没收到过它公布的状态", "检查 bot 进程是否在跑、连的是不是这个 Supervisor 的地址")
            return
        online = health["adapter_online"]
        add(adapter, "passed" if online else "failed", "心跳正常" if online else "心跳中断", "检查 bot 进程和 Supervisor 地址")
        if not online:
            add(f"{name} 连接", "skipped", "适配进程没在跑，无从判断")
        else:
            connected = health["connected"]
            add(f"{name} 连接", "passed" if connected else "failed", "已连接" if connected else "尚未确认连接",
                platform.connection_hint or _CONNECTION_HINT)
        if health["config_path_mismatch"]:
            add(f"{name} 配置路径", "failed", "Supervisor 与 bot 用的配置文件不同", _mismatch_action(platform))
        if health["parse_error"]:
            add(f"{name} 配置文件", "failed", "配置文件读不懂，bot 还在用上一份能读的", "到平台页修正 YAML 语法后重新检查")

    def _layout_check(self, add: Any) -> None:
        layout = config_layout.layout_status(self.root)
        if not layout["migrated"]:
            add("配置布局", "failed", "配置还没搬进 config/", "重启 Supervisor，按日志里的报错修好后再检查")
            return
        notes = [*layout["warnings"], *layout["stale_legacy"]]
        if stray_platforms_section(self.root):
            notes.append(
                f"{config_layout.MODELS_FILE} 里还写着 platforms 段，现在没人读它，"
                f"平台令牌要挪到 {config_layout.PLATFORM_SECRETS_FILE}",
            )
        if notes:
            # 旧位置比新位置新，多半是退回旧版本那阵子改的：那些改动现在不生效。
            add("配置布局", "warning", "；".join(notes), "旧位置的改动不会生效，要保留的话手动挪进 config/ 里对应的文件")
            return
        group = config_layout.config_share_group(self.root)
        add("配置布局", "passed", f"配置用的是共享组 {group}" if group else "配置都在 config/ 下")

    def instances(self) -> list[dict[str, Any]]:
        rows = load_instances(self.root)
        prefix = url_prefix()
        if not rows:
            # 和读清单时一样：序号 0 占站点根，带前缀的推不出序号，标 -1。
            rows = [{
                "id": "current", "label": "当前实例", "path": prefix, "idx": 0 if not prefix else -1,
                "platform": current_platform().id, "account": "",
            }]
        return [{**row, "current": row["path"] == prefix} for row in rows]

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
            for platform, health in self._platform_health():
                self._platform_checks(add, platform, health)
            workers = facts["workers"]
            add("Engine", "passed" if workers and all(w.get("alive") for w in workers.values()) else "warning",
                f"已登记 {len(workers)} 个worker" if workers else "当前进程未提供worker管理信息", "查看运行页面")
            disk = shutil.disk_usage(self.root)
            add("存储空间", "passed" if disk.free > 512 * 1024 * 1024 else "warning",
                f"可用 {disk.free // (1024 * 1024)} MiB")
            self._layout_check(add)
            store = getattr(self.state, "feature_config_store", None) or ConfigStore(
                path=config_layout.models_path(self.root), workspace_root=self.root,
            )
            if store.load_error:
                # 文件写坏了不影响运行：一直用着上一份能用的配置，但之后在控制台改渠道会被拒。
                add("模型配置文件", "warning", f"{store.load_error}，现在用的是上一份能用的配置",
                    f"在原文编辑器里修好 {config_layout.MODELS_FILE}")
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
                        with plugin_scope(workspace_root=self.root, purpose="diagnostic", channel=getattr(secret, "channel_name", "") or secret.provider):
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
