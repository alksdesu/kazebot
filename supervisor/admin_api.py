from __future__ import annotations

import contextlib
import json
import os
import shutil
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import qq_trigger
from .tool_catalog import ToolEntry, tool_catalog, tool_names

class RawContent(BaseModel):
    content: str

class NodeCreate(BaseModel):
    id: str
    content: str

class FragmentCreate(BaseModel):
    content: str


# ---------------------------------------------------------------------------
#  Admin Token 认证
# ---------------------------------------------------------------------------

_admin_token: str = ""


def _token_file(workspace_root: Path) -> Path:
    return workspace_root / "data" / ".admin_token"


def _harden(path: Path) -> None:
    # 这个令牌能改 policy、编辑节点、重启引擎，同机其他用户没有理由读到它。
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _read_token(path: Path) -> str:
    # 空文件和只剩空白的文件都当作没有：拿空串当令牌等于把 admin 面敞开。
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _persist_token(path: Path, token: str) -> bool:
    """落盘失败不阻断启动：令牌在内存里，控制台照常能进，只有适配器读不到。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同目录 tmp + replace：适配器每次请求都重读这个文件，读到写了一半的就是一次 401。
        tmp.write_text(token, encoding="utf-8")
        _harden(tmp)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        print(f"[admin] 令牌写入 {path} 失败：{exc}", flush=True)
        print("[admin] engine 与各适配器将读不到令牌，它们发起的请求会返回 401", flush=True)
        return False


def init_admin_token(workspace_root: Path, *, console_url: str = "") -> str:
    """确定本进程的管理令牌并落盘，engine 与各适配器都从这个文件读。

    取值优先级：CLONOTH_ADMIN_TOKEN > data/.admin_token 已有值 > 新生成。
    回读那一级不能省：少了它，没配环境变量的部署每次重启都换一个令牌，
    浏览器里存的和适配器启动时读进内存的会一起失效。
    """
    global _admin_token
    path = _token_file(workspace_root)

    env_token = os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip()
    if env_token:
        _admin_token = env_token
        _persist_token(path, env_token)
        print("[admin] 管理令牌取自 CLONOTH_ADMIN_TOKEN（内容不输出）", flush=True)
        return _admin_token

    existing = _read_token(path)
    if existing:
        _admin_token = existing
        _harden(path)
        print(f"[admin] 管理令牌沿用 {path}（内容不输出）", flush=True)
        return _admin_token

    token = secrets.token_urlsafe(24)
    _admin_token = token
    if _persist_token(path, token):
        print(f"[admin] 尚未设置管理令牌，已生成并写入 {path}", flush=True)
    if console_url:
        print("[admin] 首次登录请打开：", flush=True)
        print(f"        {console_url}?token={token}", flush=True)
        print("[admin] 设置 CLONOTH_ADMIN_TOKEN 后本行不再输出", flush=True)
    return _admin_token


def get_admin_token() -> str:
    global _admin_token
    if _admin_token:
        return _admin_token
    # 没走 init_admin_token 的调用方也必须拿到非空值，否则校验形同虚设。
    _admin_token = os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip() or secrets.token_urlsafe(24)
    return _admin_token


# 认证失败退避。前几次留给手滑，之后按 IP 指数退避 —— 没有这一层的话，本机上
# 一个脚本可以不受限地枚举 token。
_AUTH_FREE_ATTEMPTS = 3
_AUTH_BACKOFF_BASE_SEC = 1.0
_AUTH_BACKOFF_MAX_SEC = 60.0
# 记录条数封顶，否则源 IP 随机的探测会把这张表本身变成内存泄漏。
_AUTH_TRACK_MAX_CLIENTS = 1024
_auth_failures: "OrderedDict[str, tuple[int, float]]" = OrderedDict()


def _auth_client_key(request: Request) -> str:
    """退避按哪个键计。

    刻意不读 X-Forwarded-For：那个头谁都能伪造，认它等于让退避形同虚设。反代后面
    所有请求会共用一个键，但成功一次就清零，正常使用碰不到这条线。
    """
    client = request.client
    return (client.host if client else "") or "unknown"


def _auth_backoff_remaining(key: str, now: float) -> float:
    entry = _auth_failures.get(key)
    if entry is None:
        return 0.0
    return max(0.0, entry[1] - now)


def _record_auth_failure(key: str, now: float) -> None:
    failures = _auth_failures.get(key, (0, 0.0))[0] + 1
    extra = failures - _AUTH_FREE_ATTEMPTS
    delay = 0.0
    if extra > 0:
        delay = min(_AUTH_BACKOFF_BASE_SEC * (2 ** (extra - 1)), _AUTH_BACKOFF_MAX_SEC)
    _auth_failures[key] = (failures, now + delay)
    _auth_failures.move_to_end(key)
    while len(_auth_failures) > _AUTH_TRACK_MAX_CLIENTS:
        _auth_failures.popitem(last=False)


def verify_admin_token(request: Request) -> None:
    token = get_admin_token()
    key = _auth_client_key(request)
    now = time.time()
    blocked = _auth_backoff_remaining(key, now)
    if blocked > 0:
        raise HTTPException(
            status_code=429,
            detail=f"认证失败次数过多，请等待 {blocked:.0f} 秒后重试",
            headers={"Retry-After": str(max(1, int(blocked + 0.5)))},
        )

    auth = request.headers.get("Authorization", "")
    presented = auth[7:].strip() if auth.startswith("Bearer ") else (request.query_params.get("token") or "")
    # compare_digest 而不是 ==：后者会在第一个不同字节就返回，逐字节试探能问出 token。
    if presented and secrets.compare_digest(presented, token):
        _auth_failures.pop(key, None)
        return

    _record_auth_failure(key, now)
    raise HTTPException(status_code=401, detail="Unauthorized")


def create_admin_router(workspace_root: Path) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    def _safe_path(base_dir: Path, rel_path: str, suffix: str = "") -> Path:
        name = rel_path if not suffix or rel_path.endswith(suffix) else rel_path + suffix
        p = (base_dir / name).resolve()
        if not str(p).startswith(str(base_dir.resolve())):
            raise HTTPException(status_code=400, detail="Invalid path")
        return p

    def _read_yaml(p: Path) -> dict[str, Any]:
        if not p.exists():
            return {}
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _read_text(p: Path) -> dict[str, str]:
        if not p.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return {"content": p.read_text(encoding="utf-8")}

    def _write_text(p: Path, content: str) -> dict[str, Any]:
        """整份覆盖写入，覆盖前留一份 .bak。

        这一层不解析、不校验，上游表单出一次 bug 就能把 163 行的 runtime.yaml 截成三行，
        而覆盖不可逆。备份失败就不写：没有退路的覆盖比保存失败危险得多。
        十二个 raw 配置端点共用这个函数，所以保险加在这里而不是逐个端点。
        """
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            try:
                shutil.copy2(p, p.with_name(f"{p.name}.bak"))
            except OSError as exc:
                raise HTTPException(
                    status_code=500, detail=f"备份 {p.name} 失败，未写入：{exc}",
                ) from exc
        p.write_text(content, encoding="utf-8")
        return {"ok": True}

    def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        if not text.startswith("---\n"):
            return {}, text
        end = text.find("\n---\n", 4)
        if end < 0:
            return {}, text
        head = text[4:end]
        body = text[end + 5:]
        try:
            meta = yaml.safe_load(head) or {}
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return meta, body

    # ----- Nodes -----
    @router.get("/nodes")
    def list_nodes() -> list[dict[str, Any]]:
        # 系统节点目录分离：扫描 engine/system_nodes/ 和 config/nodes/ 两个目录，
        # engine 内建目录优先，同 id 节点只保留首次出现的。
        dirs = [
            workspace_root / "engine" / "system_nodes",
            workspace_root / "config" / "nodes",
        ]
        res = []
        seen_ids: set[str] = set()
        for nodes_dir in dirs:
            if not nodes_dir.exists():
                continue
            for f in nodes_dir.glob("*.yaml"):
                data = _read_yaml(f)
                nid = data.get("id", f.stem)
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)
                ta_raw = data.get("tool_access", {})
                if isinstance(ta_raw, str):
                    ta_raw = {"mode": ta_raw}
                elif not isinstance(ta_raw, dict):
                    ta_raw = {"mode": "none"}
                res.append({
                    "id": nid,
                    "name": data.get("name", ""),
                    "type": data.get("type", ""),
                    "model": data.get("model", ""),
                    "tool_access": ta_raw,
                    "skills": data.get("skills", {}),
                    "description": data.get("description", ""),
                    "delegate_targets": list(data.get("delegate_targets") or []),
                })
        return res

    @router.get("/nodes/{node_id}/raw")
    def get_node_raw(node_id: str) -> dict[str, str]:
        p = _safe_path(workspace_root / "config" / "nodes", node_id, ".yaml")
        return _read_text(p)

    @router.put("/nodes/{node_id}/raw")
    def update_node_raw(node_id: str, payload: RawContent) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "nodes", node_id, ".yaml")
        return _write_text(p, payload.content)

    @router.post("/nodes")
    def create_node(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "nodes", payload.id, ".yaml")
        if p.exists():
            raise HTTPException(status_code=409, detail="Node already exists")
        return _write_text(p, payload.content)

    @router.delete("/nodes/{node_id}")
    def delete_node(node_id: str) -> dict[str, Any]:
        p = _safe_path(workspace_root / "config" / "nodes", node_id, ".yaml")
        if p.exists():
            p.unlink()
        return {"ok": True}

    # ----- Node files: YAML nodes and Markdown fragments under config/nodes/ -----
    @router.get("/node-files")
    def list_node_files() -> list[dict[str, Any]]:
        nodes_dir = workspace_root / "config" / "nodes"
        if not nodes_dir.exists():
            return []
        res: list[dict[str, Any]] = []
        for f in sorted([*nodes_dir.glob("*.yaml"), *nodes_dir.glob("*.yml"), *nodes_dir.glob("*.md")]):
            if not f.is_file():
                continue
            name = f.name
            stem = f.stem
            is_example = ".example" in name or stem.endswith("_example")
            base_name = name.replace(".example.yaml", ".yaml").replace(".example.yml", ".yml").replace(".example.md", ".md")
            res.append({
                "name": name,
                "path": str(f.relative_to(workspace_root).as_posix()),
                "kind": "fragment" if f.suffix == ".md" else "node",
                "suffix": f.suffix,
                "is_example": is_example,
                "base_name": base_name,
                "size": f.stat().st_size,
                "updated_at": f.stat().st_mtime,
            })
        return res

    @router.get("/node-files/{filename}/raw")
    def get_node_file_raw(filename: str) -> dict[str, str]:
        if not (filename.endswith(".yaml") or filename.endswith(".yml") or filename.endswith(".md")):
            raise HTTPException(status_code=400, detail="Only .yaml/.yml/.md files are allowed")
        p = _safe_path(workspace_root / "config" / "nodes", filename)
        return _read_text(p)

    @router.put("/node-files/{filename}/raw")
    def update_node_file_raw(filename: str, payload: RawContent) -> dict[str, Any]:
        if not (filename.endswith(".yaml") or filename.endswith(".yml") or filename.endswith(".md")):
            raise HTTPException(status_code=400, detail="Only .yaml/.yml/.md files are allowed")
        p = _safe_path(workspace_root / "config" / "nodes", filename)
        return _write_text(p, payload.content)

    @router.post("/node-files/{filename}/make-example")
    def make_node_file_example(filename: str) -> dict[str, Any]:
        if not (filename.endswith(".yaml") or filename.endswith(".yml") or filename.endswith(".md")):
            raise HTTPException(status_code=400, detail="Only .yaml/.yml/.md files are allowed")
        src = _safe_path(workspace_root / "config" / "nodes", filename)
        if not src.exists():
            raise HTTPException(status_code=404, detail="Source file not found")
        if filename.endswith(".yaml"):
            example_name = filename[:-5] + ".example.yaml"
        elif filename.endswith(".yml"):
            example_name = filename[:-4] + ".example.yml"
        else:
            example_name = filename[:-3] + ".example.md"
        dst = _safe_path(workspace_root / "config" / "nodes", example_name)
        if dst.exists():
            return {"ok": True, "created": False, "path": str(dst.relative_to(workspace_root).as_posix())}
        shutil.copyfile(src, dst)
        return {"ok": True, "created": True, "path": str(dst.relative_to(workspace_root).as_posix())}

    # ----- Drawtools (NovelAI image generation) -----
    def _drawtools_dir() -> Path:
        return workspace_root / "tools" / "drawtools"

    def _drawtools_prompt_dir() -> Path:
        return _drawtools_dir() / "prompts" / "novelai"

    def _drawtools_read_with_example(path: Path, example_path: Path) -> dict[str, Any]:
        target = path if path.exists() else example_path
        if not target.exists():
            return {"content": "", "exists": False, "using_example": False}
        return {
            "content": target.read_text(encoding="utf-8"),
            "exists": path.exists(),
            "using_example": not path.exists() and example_path.exists(),
            "path": str(path.relative_to(workspace_root).as_posix()),
            "example_path": str(example_path.relative_to(workspace_root).as_posix()),
        }

    def _drawtools_write(path: Path, content: str) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(path.relative_to(workspace_root).as_posix())}

    @router.get("/drawtools")
    def get_drawtools_bundle() -> dict[str, Any]:
        base = _drawtools_dir()
        prompt_dir = _drawtools_prompt_dir()
        return {
            "settings": _drawtools_read_with_example(base / "settings.yaml", base / "settings.example.yaml"),
            "character_tags": _drawtools_read_with_example(base / "character_tags.yaml", base / "character_tags.example.yaml"),
            "prompts": {
                "top_system": _drawtools_read_with_example(prompt_dir / "top-system.md", prompt_dir / "top-system.example.md"),
                "output_format": _drawtools_read_with_example(prompt_dir / "output-format.md", prompt_dir / "output-format.example.md"),
                "tag_guide": _drawtools_read_with_example(prompt_dir / "tag-guide.md", prompt_dir / "tag-guide.example.md"),
            },
        }

    @router.post("/drawtools/init")
    def init_drawtools_configs() -> dict[str, Any]:
        created: list[str] = []
        pairs = [
            (_drawtools_dir() / "settings.example.yaml", _drawtools_dir() / "settings.yaml"),
            (_drawtools_dir() / "character_tags.example.yaml", _drawtools_dir() / "character_tags.yaml"),
            (_drawtools_prompt_dir() / "top-system.example.md", _drawtools_prompt_dir() / "top-system.md"),
            (_drawtools_prompt_dir() / "output-format.example.md", _drawtools_prompt_dir() / "output-format.md"),
            (_drawtools_prompt_dir() / "tag-guide.example.md", _drawtools_prompt_dir() / "tag-guide.md"),
        ]
        for src, dst in pairs:
            if dst.exists() or not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            created.append(str(dst.relative_to(workspace_root).as_posix()))
        return {"ok": True, "created": created}

    @router.get("/drawtools/settings/raw")
    def get_drawtools_settings_raw() -> dict[str, Any]:
        return _drawtools_read_with_example(_drawtools_dir() / "settings.yaml", _drawtools_dir() / "settings.example.yaml")

    @router.put("/drawtools/settings/raw")
    def update_drawtools_settings_raw(payload: RawContent) -> dict[str, Any]:
        return _drawtools_write(_drawtools_dir() / "settings.yaml", payload.content)

    @router.get("/drawtools/characters/raw")
    def get_drawtools_characters_raw() -> dict[str, Any]:
        return _drawtools_read_with_example(_drawtools_dir() / "character_tags.yaml", _drawtools_dir() / "character_tags.example.yaml")

    @router.put("/drawtools/characters/raw")
    def update_drawtools_characters_raw(payload: RawContent) -> dict[str, Any]:
        return _drawtools_write(_drawtools_dir() / "character_tags.yaml", payload.content)

    @router.get("/drawtools/prompts/{name}/raw")
    def get_drawtools_prompt_raw(name: str) -> dict[str, Any]:
        mapping = {
            "top-system": ("top-system.md", "top-system.example.md"),
            "output-format": ("output-format.md", "output-format.example.md"),
            "tag-guide": ("tag-guide.md", "tag-guide.example.md"),
        }
        if name not in mapping:
            raise HTTPException(status_code=404, detail="Unknown prompt template")
        actual, example = mapping[name]
        return _drawtools_read_with_example(_drawtools_prompt_dir() / actual, _drawtools_prompt_dir() / example)

    @router.put("/drawtools/prompts/{name}/raw")
    def update_drawtools_prompt_raw(name: str, payload: RawContent) -> dict[str, Any]:
        mapping = {
            "top-system": "top-system.md",
            "output-format": "output-format.md",
            "tag-guide": "tag-guide.md",
        }
        if name not in mapping:
            raise HTTPException(status_code=404, detail="Unknown prompt template")
        return _drawtools_write(_drawtools_prompt_dir() / mapping[name], payload.content)

    @router.post("/drawtools/cleanup")
    def cleanup_drawtools_attachments() -> dict[str, Any]:
        import sys as _sys
        drawtools = _drawtools_dir()
        if str(drawtools) not in _sys.path:
            _sys.path.insert(0, str(drawtools))
        from cleanup import cleanup_novelai_attachments  # type: ignore
        return cleanup_novelai_attachments(force=True)

    # ----- Runtime config -----
    # ----- Config (data/config.yaml) -----
    @router.get("/config/raw")
    def get_config_raw() -> dict[str, str]:
        p = workspace_root / "data" / "config.yaml"
        return _read_text(p)

    @router.put("/config/raw")
    def update_config_raw(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "data" / "config.yaml"
        return _write_text(p, payload.content)

    # ----- Runtime -----
    @router.get("/runtime/raw")
    def get_runtime() -> dict[str, str]:
        p = workspace_root / "config" / "runtime.yaml"
        return _read_text(p)
        
    @router.put("/runtime/raw")
    def update_runtime(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "config" / "runtime.yaml"
        return _write_text(p, payload.content)

    # ----- QQ 热载配置 -----
    # 只管文件；「实际生效值」由 bot 进程写 data/qq_live_state.json 公布。supervisor 不
    # 自己算生效值 —— bot 是独立进程，env 可能和这里不一样，算出来的会是另一份配置。
    def _qq_config_path() -> Path:
        return workspace_root / "config" / "qq.yaml"

    def _qq_live_state() -> dict[str, Any]:
        p = workspace_root / "data" / "qq_live_state.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _qq_leaf_paths(document: Any, prefix: str = "") -> list[str]:
        if isinstance(document, dict):
            leaves: list[str] = []
            for key, value in document.items():
                leaves.extend(_qq_leaf_paths(value, f"{prefix}.{key}" if prefix else str(key)))
            return leaves
        if document is None:
            # 只写了段名没写值（`channels:`）不是配置项，别报成拼错的键。
            # 显式 null 同理，语义是「这个键交回 .env」。
            return []
        return [prefix] if prefix else []

    @router.get("/qq/raw")
    def get_qq_raw() -> dict[str, Any]:
        p = _qq_config_path()
        if not p.exists():
            # 「还没接管」是正常状态，不是错误。刻意不回落到 qq.example.yaml：
            # 那份是默认值，被当成当前配置保存下去就等于悄悄覆盖 .env 里的设置。
            return {"content": "", "exists": False}
        return {"content": p.read_text(encoding="utf-8"), "exists": True}

    @router.put("/qq/raw")
    def update_qq_raw(payload: RawContent) -> dict[str, Any]:
        try:
            document = yaml.safe_load(payload.content)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"YAML 解析失败：{exc}") from exc
        if document is not None and not isinstance(document, dict):
            raise HTTPException(status_code=400, detail="顶层必须是映射（key: value）")

        warnings: list[str] = []
        known = _qq_live_state().get("paths")
        if isinstance(known, dict) and known:
            unknown = sorted(
                set(_qq_leaf_paths(document or {})) - set(known.values()) - {"version"}
            )
            if unknown:
                # 无人消费的键改了不会有任何效果，而界面只会显示保存成功。
                raise HTTPException(status_code=400, detail=f"无人消费的配置项：{unknown}")
        else:
            warnings.append("bot 进程未公布配置结构，本次只校验了 YAML 语法")

        p = _qq_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # 同目录 tmp + replace：bot 进程随时在读，读到半截 yaml 等于整份配置塌掉。
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(payload.content, encoding="utf-8")
        os.replace(tmp, p)
        return {"ok": True, "warnings": warnings}

    @router.get("/qq/state")
    def get_qq_state() -> dict[str, Any]:
        state = _qq_live_state()
        p = _qq_config_path()
        try:
            st = p.stat()
            fingerprint = {"exists": True, "mtime_ns": st.st_mtime_ns, "size": st.st_size}
        except OSError:
            fingerprint = {"exists": False, "mtime_ns": 0, "size": 0}
        if not state:
            return {
                "published": False,
                "reason": "bot 进程尚未公布生效配置（未启动，或还没跑完第一轮对齐）",
                "file": fingerprint,
            }
        age_sec = max(0.0, time.time() - float(state.get("published_at") or 0.0))
        return {
            "published": True,
            "age_sec": round(age_sec, 3),
            # bot 至少每 30s 重写一次，这里留三倍余量再判定失联。
            "stale": age_sec > 90.0,
            # 「已生效」的判据是 bot 跑着的那份值确实来自当前这个文件版本。
            # 不能拿 state["file"] 比 —— 那是 bot 看到的文件指纹，文件写坏时它照样等于
            # 当前文件，而 bot 用的还是上一份配置。
            "applied": state.get("loaded") == fingerprint,
            "file": fingerprint,
            "state": state,
        }

    @router.post("/qq/trigger/dry-run")
    def qq_trigger_dry_run(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """把一批模拟消息过一遍真实判定。

        config 省略时用 bot 公布的生效值 —— 「试听当前配置」不该要求调用方自己拼一份，
        拼出来的那份和 bot 跑着的不一样就等于试听骗人。
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise HTTPException(status_code=400, detail="messages 必须是数组")
        if len(messages) > 200:
            raise HTTPException(status_code=400, detail="一次最多 200 条")

        values = payload.get("config")
        if not isinstance(values, dict):
            values = _qq_live_state().get("values")
        if not isinstance(values, dict) or not values:
            raise HTTPException(
                status_code=409,
                detail="bot 进程尚未公布生效配置，请显式传 config 后再试听",
            )
        try:
            return qq_trigger.dry_run(
                workspace_root, messages, values,
                apply_cooldown=payload.get("cooldown", True) is not False,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # 缺键/类型不对最可能来自调用方自拼的 config，这是 400 不是 500。
            raise HTTPException(status_code=400, detail=f"配置不完整或类型不对：{exc}") from exc

    # ----- Policy -----
    @router.get("/policy/raw")
    def get_policy() -> dict[str, str]:
        p = workspace_root / "data" / "policy.yaml"
        if not p.exists():
            p = workspace_root / "policy.example.yaml"
        return _read_text(p)
        
    @router.put("/policy/raw")
    def update_policy(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "data" / "policy.yaml"
        return _write_text(p, payload.content)

    # ----- Schedules -----
    @router.get("/schedules/raw")
    def get_schedules() -> dict[str, str]:
        p = workspace_root / "data" / "schedules.yaml"
        if not p.exists():
            return {"content": "schedules: []"}
        return _read_text(p)
        
    @router.put("/schedules/raw")
    def update_schedules(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "data" / "schedules.yaml"
        return _write_text(p, payload.content)

    # ----- Skills -----
    @router.get("/skills")
    def list_skills() -> list[dict[str, Any]]:
        skills_dir = workspace_root / "skills"
        if not skills_dir.exists():
            return []
        res = []
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            try:
                text = skill_md.read_text(encoding="utf-8")
                meta, body = _parse_skill_frontmatter(text)
                res.append({
                    "name": str(meta.get("name") or skill_md.parent.name),
                    "description": str(meta.get("description") or ""),
                    "enabled": bool(meta.get("enabled", True)),
                    "strategy": str(meta.get("strategy") or "normal"),
                    "keywords": [str(k) for k in (meta.get("keywords") or []) if isinstance(k, str)],
                    "body_preview": body.strip()[:200] if body else "",
                })
            except Exception:
                res.append({"name": skill_md.parent.name, "error": "parse failed"})
        return res

    @router.get("/skills/{name}/raw")
    def get_skill_raw(name: str) -> dict[str, str]:
        p = _safe_path(workspace_root / "skills", name)
        md = p / "SKILL.md"
        return _read_text(md)

    @router.put("/skills/{name}/raw")
    def update_skill_raw(name: str, payload: RawContent) -> dict[str, Any]:
        p = _safe_path(workspace_root / "skills", name)
        md = p / "SKILL.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        return _write_text(md, payload.content)

    @router.post("/skills")
    def create_skill(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "skills", payload.id)
        md = p / "SKILL.md"
        if md.exists():
            raise HTTPException(status_code=409, detail="Skill already exists")
        md.parent.mkdir(parents=True, exist_ok=True)
        return _write_text(md, payload.content)

    @router.delete("/skills/{name}")
    def delete_skill(name: str) -> dict[str, Any]:
        p = _safe_path(workspace_root / "skills", name)
        if p.exists() and p.is_dir():
            shutil.rmtree(p)
        return {"ok": True}

    # ----- Tools -----
    def _catalog() -> list[ToolEntry]:
        return tool_catalog(workspace_root)

    def _tool_file(name: str) -> Path:
        """工具名对应的源码文件。

        子目录里的工具、SPEC 名和文件名对不上的都得走这里。内置和插件工具没有源码，
        直接说清楚 —— 拼一条 tools/{name}.py 出来只会让人看到「文件不存在」。
        """
        for entry in _catalog():
            if entry.name != name:
                continue
            if not entry.editable or not entry.file:
                raise HTTPException(
                    status_code=409,
                    detail=f"{name} 是{'插件' if entry.source == 'plugin' else '内置'}工具，没有可编辑的源码文件",
                )
            return workspace_root / entry.file
        raise HTTPException(status_code=404, detail="Tool not found")

    @router.get("/tools")
    def list_tools() -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in _catalog()]

    @router.get("/tools/{name}/raw")
    def get_tool_raw(name: str) -> dict[str, str]:
        return _read_text(_tool_file(name))

    @router.put("/tools/{name}/raw")
    def update_tool_raw(name: str, payload: RawContent) -> dict[str, Any]:
        return _write_text(_tool_file(name), payload.content)

    @router.post("/tools")
    def create_tool(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(workspace_root / "tools", payload.id, ".py")
        # 重名的新文件建了也没用：注册表只认其中一个，内置工具还会直接盖掉外部同名。
        if p.exists() or any(entry.name == payload.id for entry in _catalog()):
            raise HTTPException(status_code=409, detail="Tool already exists")
        return _write_text(p, payload.content)

    @router.delete("/tools/{name}")
    def delete_tool(name: str) -> dict[str, Any]:
        p = _tool_file(name)
        if not p.exists():
            raise HTTPException(status_code=404, detail="Tool not found")
        p.unlink()
        return {"ok": True}

    # ----- MCP Clients -----
    @router.get("/mcp-clients")
    def list_mcp_clients() -> list[dict[str, Any]]:
        p = workspace_root / "data" / "mcp_clients.yaml"
        if not p.exists():
            return []
        data = _read_yaml(p)
        clients = data.get("clients")
        if not isinstance(clients, dict):
            return []
        res = []
        for cid, spec in sorted(clients.items()):
            if not isinstance(spec, dict):
                continue
            item = {"id": str(cid)}
            item.update(spec)
            res.append(item)
        return res

    @router.get("/mcp-clients/raw")
    def get_mcp_clients_raw() -> dict[str, str]:
        p = workspace_root / "data" / "mcp_clients.yaml"
        if not p.exists():
            return {"content": "version: 1\nclients: {}\n"}
        return _read_text(p)

    @router.put("/mcp-clients/raw")
    def update_mcp_clients_raw(payload: RawContent) -> dict[str, Any]:
        p = workspace_root / "data" / "mcp_clients.yaml"
        return _write_text(p, payload.content)

    # ----- All tool names -----
    @router.get("/all-tool-names")
    def all_tool_names() -> list[str]:
        return tool_names(workspace_root)

    # ----- Effective tools -----
    @router.get("/nodes/{node_id}/effective-tools")
    def node_effective_tools(node_id: str) -> dict[str, Any]:
        """节点配了这些工具，实际还剩下哪些能用。

        勾了不等于能用：名字可能根本不存在，生图渠道没配 model 会在构建工具表时
        被摘掉，而外部脚本除非自己声明 guard，否则服务端策略那一页管不到它。
        """
        from toolbox.registry import extract_tool_spec, iter_external_tool_files

        for nodes_dir in (workspace_root / "engine" / "system_nodes", workspace_root / "config" / "nodes"):
            path = _safe_path(nodes_dir, node_id, ".yaml")
            if path.exists():
                break
        else:
            raise HTTPException(status_code=404, detail="Node not found")

        data = _read_yaml(path)
        ta = data.get("tool_access", {})
        if isinstance(ta, str):
            ta = {"mode": ta}
        elif not isinstance(ta, dict):
            ta = {}
        mode = str(ta.get("mode") or "none").strip()
        listed = ta.get("allow") if mode == "allowlist" else ta.get("deny")
        names = [str(x).strip() for x in (listed or []) if str(x or "").strip()]

        external: dict[str, bool] = {}
        for f in iter_external_tool_files(workspace_root / "tools"):
            spec, _timeout = extract_tool_spec(f)
            if spec and isinstance(spec.get("name"), str):
                external[spec["name"]] = isinstance(spec.get("guard"), dict)

        known = {entry.name for entry in _catalog()}
        try:
            from engine.builtin.image_gen_gating import disabled_image_tools
            gated = disabled_image_tools(workspace_root)
        except Exception:
            gated = set()

        rows: list[dict[str, Any]] = []
        for name in names:
            is_external = name in external
            rows.append({
                "name": name,
                "registered": name in known,
                "external": is_external,
                # 内置工具走不走 request_guard 得看源码，这里不猜；只有外部脚本
                # 能从 SPEC.guard 得到确定答案。None = 不下结论。
                "guarded": external.get(name) if is_external else None,
                "gated": "渠道未配置 model，构建工具表时会被摘掉" if name in gated else "",
            })
        return {"node_id": node_id, "mode": mode, "listed_as": "allow" if mode == "allowlist" else "deny", "tools": rows}

    # ----- Memory -----
    # data/memory 根目录是 conversation_key 缺失时的落点，URL 里没法用空串表示。
    _ROOT_NAMESPACE = "__root__"

    def _namespace_arg(namespace: str) -> str:
        return "" if namespace == _ROOT_NAMESPACE else namespace

    def _memory_call(fn, *args):
        from engine.memory_admin import MemoryAdminError

        try:
            return fn(*args)
        except MemoryAdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/memory/overview")
    def memory_overview() -> dict[str, Any]:
        from engine import memory_admin

        from .conversation_labels import describe_namespaces, subject_label

        labels = describe_namespaces(workspace_root)
        rows: list[dict[str, Any]] = []
        for row in memory_admin.list_namespaces(workspace_root):
            namespace = row["namespace"]
            described = labels.get(namespace)
            if row["kind"] == "subject":
                described = {
                    "kind": "subject",
                    "label": subject_label(workspace_root, row["subject"]),
                    "alias": row["subject"],
                }
            rows.append({
                **row,
                "key": namespace or _ROOT_NAMESPACE,
                # 认不出归属的目录多半是用完即弃的子代理会话，堆着没人清。
                "owner": described or {"kind": "unknown", "label": "未知来源"},
            })
        return {"namespaces": rows}

    @router.get("/memory/{namespace}/entries")
    def memory_entries(namespace: str) -> dict[str, Any]:
        from engine import memory_admin

        entries = _memory_call(memory_admin.list_entries, workspace_root, _namespace_arg(namespace))
        return {"entries": entries}

    @router.post("/memory/{namespace}/entries")
    def memory_upsert(namespace: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        from engine import memory_admin

        entry = _memory_call(
            memory_admin.upsert_entry, workspace_root, _namespace_arg(namespace), payload,
        )
        return {"ok": True, "entry": entry}

    @router.delete("/memory/{namespace}/entries/{book}/{entry_id}")
    def memory_delete(namespace: str, book: str, entry_id: str) -> dict[str, Any]:
        from engine import memory_admin

        removed = _memory_call(
            memory_admin.delete_entry, workspace_root, _namespace_arg(namespace), book, entry_id,
        )
        if not removed:
            raise HTTPException(status_code=404, detail="memory entry not found")
        return {"ok": True}

    @router.delete("/memory/{namespace}")
    def memory_clear(namespace: str) -> dict[str, Any]:
        from engine import memory_admin

        removed = _memory_call(memory_admin.clear_namespace, workspace_root, _namespace_arg(namespace))
        return {"ok": True, "removed": removed}

    return router
