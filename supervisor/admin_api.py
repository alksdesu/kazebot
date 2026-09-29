from __future__ import annotations

import contextlib
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

import config_layout
from engine.node import node_file

from .scheduler import schedules_lock, schedules_path
from .tool_catalog import ToolEntry, tool_catalog, tool_names

class RawContent(BaseModel):
    content: str
    # 打开编辑器时拿到的指纹。给了就先比对：中途被别处改过就 409，一个字节都不写。
    base_hash: str | None = None

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
        """base_dir 下的相对路径，走出去就 400。

        返回不解析软链的写法：共享组的 config/ 是软链，显示给人看的路径和 policy 认的名字都按工作区里的走。
        """
        name = rel_path if not suffix or rel_path.endswith(suffix) else rel_path + suffix
        base = base_dir.resolve()
        resolved = (base_dir / name).resolve()
        # 按字符串前缀比会放过 nodes-evil/ 这种同名开头的兄弟目录。
        if resolved == base or not resolved.is_relative_to(base):
            raise HTTPException(status_code=400, detail="Invalid path")
        return base_dir / resolved.relative_to(base)

    def _rel(path: Path) -> str:
        return config_layout.logical_path(workspace_root, path)

    def _read_yaml(p: Path) -> dict[str, Any]:
        if not p.exists():
            return {}
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _read_text(p: Path, **extra: Any) -> dict[str, Any]:
        """文件内容连同指纹：编辑器保存时带回指纹，写入方据此认出中途被别处改过。"""
        if not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        data = p.read_bytes()
        # 和 read_text 一样把换行统一成 \n；指纹按落盘的原始字节算，写入方比的也是字节。
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        return {"content": text, "hash": config_layout.bytes_hash(data), "path": _rel(p), **extra}

    def _write_overlay(target: Path, shown: Path, content: str, base_hash: str | None) -> dict[str, Any]:
        """整份覆盖写入 target，覆盖前留一份 .bak。

        target 还不存在时编辑器里显示的是 shown（仓库默认或模板），指纹按那份比。
        这一层不解析、不校验，上游表单出一次 bug 就能把 163 行的 runtime.yaml 截成三行，
        而覆盖不可逆。备份失败就不写：没有退路的覆盖比保存失败危险得多。
        raw 配置端点共用这个函数，所以保险加在这里而不是逐个端点。
        """
        try:
            with config_layout.locked(target):
                current = target if target.exists() else shown
                if base_hash is not None and base_hash != config_layout.file_hash(current):
                    raise config_layout.ConfigConflict(f"{target.name} 已被别处改过，刷新后再改")
                new_hash = config_layout.write_locked(
                    target, content, backup=True, mode=config_layout.private_mode_for(workspace_root, target),
                )
        except config_layout.ConfigConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except config_layout.ConfigBackupError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"写入 {target.name} 失败，原文件没动：{exc}") from exc
        return {"ok": True, "hash": new_hash, "path": _rel(target)}

    def _write_text(p: Path, content: str, base_hash: str | None = None) -> dict[str, Any]:
        return _write_overlay(p, p, content, base_hash)

    def _create_text(p: Path, content: str) -> dict[str, Any]:
        """只在文件还不存在时写：检查和落盘之间被别处抢先建了，照样按冲突报。"""
        return _write_overlay(p, p, content, "")

    def _remove(p: Path) -> bool:
        try:
            return config_layout.remove_config_file(p)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除 {p.name} 失败：{exc}") from exc

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
    def _node_source(path: Path) -> str:
        """system：跟着代码走的系统节点；default：仓库默认原样；override：改过的仓库默认；custom：实例自己加的。"""
        system_dir, _own_dir, default_dir = config_layout.node_search_dirs(workspace_root)
        if path.parent == system_dir:
            return "system"
        if path.parent == default_dir:
            return "default"
        return "override" if (default_dir / path.name).is_file() else "custom"

    def _same_as_default(path: Path) -> bool:
        """和仓库默认一字不差的覆盖什么也没改，只会让以后默认的更新到不了这个实例。"""
        default = config_layout.default_nodes_dir() / path.name
        return path.parent != default.parent and default.is_file() and path.read_bytes() == default.read_bytes()

    @router.get("/nodes")
    def list_nodes() -> list[dict[str, Any]]:
        """节点列表。id 一律取文件名。

        engine 的 load_node 是拿 node_id 直接拼 {node_id}.yaml 去找文件的，yaml 里那个
        id 字段它根本不读。这里以前用 id 字段去重，于是 qq.orchestrator.example.yaml
        把同名的 qq.orchestrator.yaml 顶掉了：界面显示示例文件的内容，写回却按文件名
        落到真文件上——读一份、写另一份。
        """
        # 系统节点、实例改过或自己加的、仓库默认依次排，同名时排在前面的那份才会被加载，
        # 和 load_node 的取舍顺序一致。
        res = []
        seen_ids: set[str] = set()
        for nodes_dir in config_layout.node_search_dirs(workspace_root):
            if not nodes_dir.is_dir():
                continue
            for f in sorted(nodes_dir.glob("*.yaml")):
                nid = f.stem
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)
                data = _read_yaml(f)
                ta_raw = data.get("tool_access", {})
                if isinstance(ta_raw, str):
                    ta_raw = {"mode": ta_raw}
                elif not isinstance(ta_raw, dict):
                    ta_raw = {"mode": "none"}
                declared = str(data.get("id") or "").strip()
                source = _node_source(f)
                res.append({
                    "id": nid,
                    # 模板和示例不会被派发到，列出来只是为了能编辑，别混进要配置的节点里。
                    "active": not (nid.startswith("_") or ".example" in nid),
                    # yaml 里写的 id。和文件名不一致时它是个摆设，改了也不会换节点。
                    "declared_id": declared if declared and declared != nid else "",
                    "name": data.get("name", ""),
                    "type": data.get("type", ""),
                    "model": data.get("model", ""),
                    "tool_access": ta_raw,
                    "skills": data.get("skills", {}),
                    "description": data.get("description", ""),
                    "delegate_targets": list(data.get("delegate_targets") or []),
                    "source": source,
                    "same_as_default": source == "override" and _same_as_default(f),
                })
        return res

    def _node_file(node_id: str) -> Path:
        """节点 id 实际加载的那份文件，取舍顺序和 engine 的 load_node 一样。一份都没有时是实例里该建的位置。"""
        dirs = config_layout.node_search_dirs(workspace_root)
        for nodes_dir in dirs:
            path = _safe_path(nodes_dir, node_id, ".yaml")
            if path.is_file():
                return path
        return _safe_path(dirs[1], node_id, ".yaml")

    def _node_targets(node_id: str) -> tuple[Path, Path]:
        """(改这个节点时写的位置, 仓库默认那份)。

        写系统节点要挡住：落到 config/nodes/ 下的同名文件永远不会被加载，
        改完看着成功，实际什么也没变。仓库默认那份不动，改动落成实例目录里的同名覆盖。
        """
        system_dir, own_dir, default_dir = config_layout.node_search_dirs(workspace_root)
        if _safe_path(system_dir, node_id, ".yaml").exists():
            raise HTTPException(
                status_code=409,
                detail=f"{node_id} 是系统节点，跟着代码仓库走，不能从这里改",
            )
        return _safe_path(own_dir, node_id, ".yaml"), _safe_path(default_dir, node_id, ".yaml")

    @router.get("/nodes/{node_id}/raw")
    def get_node_raw(node_id: str) -> dict[str, Any]:
        path = _node_file(node_id)
        return _read_text(path, source=_node_source(path))

    @router.put("/nodes/{node_id}/raw")
    def update_node_raw(node_id: str, payload: RawContent) -> dict[str, Any]:
        target, default = _node_targets(node_id)
        return _write_overlay(target, default, payload.content, payload.base_hash)

    @router.post("/nodes")
    def create_node(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(config_layout.nodes_dir(workspace_root), payload.id, ".yaml")
        if p.exists():
            raise HTTPException(status_code=409, detail="Node already exists")
        # 同名的系统节点和仓库默认、把这个 id 声明成旧 id 的节点都会让新文件加载不到或顶掉别人。
        taken_by = node_file(workspace_root, payload.id)
        if taken_by is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{payload.id} 已经指向 {_rel(taken_by)}，新建的文件不会被加载",
            )
        return _create_text(p, payload.content)

    @router.delete("/nodes/{node_id}")
    def delete_node(node_id: str) -> dict[str, Any]:
        """删实例自己加的节点；改过的仓库默认删掉覆盖，恢复成默认。"""
        target, default = _node_targets(node_id)
        if not target.exists():
            if default.exists():
                raise HTTPException(
                    status_code=409,
                    detail=f"{node_id} 是仓库自带的默认节点，没有改过，删不掉；不想用它就从委派关系里去掉",
                )
            raise HTTPException(status_code=404, detail="Node not found")
        _remove(target)
        return {"ok": True, "restored": default.exists()}

    # ----- Node files: YAML nodes and Markdown fragments -----
    _NODE_FILE_SUFFIXES = (".yaml", ".yml", ".md")

    def _node_file_targets(filename: str) -> tuple[Path, Path]:
        """(实例里这份文件的位置, 仓库默认那份)。"""
        if not filename.endswith(_NODE_FILE_SUFFIXES):
            raise HTTPException(status_code=400, detail="Only .yaml/.yml/.md files are allowed")
        return (
            _safe_path(config_layout.nodes_dir(workspace_root), filename),
            _safe_path(config_layout.default_nodes_dir(), filename),
        )

    def _layer_source(own: Path, default: Path) -> str:
        if own.is_file():
            return "override" if default.is_file() else "custom"
        return "default"

    def _node_file_map(nodes_dir: Path) -> dict[str, Path]:
        if not nodes_dir.is_dir():
            return {}
        return {
            f.name: f
            for f in nodes_dir.iterdir()
            if f.is_file() and f.suffix in _NODE_FILE_SUFFIXES
        }

    @router.get("/node-files")
    def list_node_files() -> list[dict[str, Any]]:
        own = _node_file_map(config_layout.nodes_dir(workspace_root))
        defaults = _node_file_map(config_layout.default_nodes_dir())
        res: list[dict[str, Any]] = []
        for name in sorted({*own, *defaults}):
            f = own.get(name) or defaults[name]
            stem = f.stem
            is_example = ".example" in name or stem.endswith("_example")
            base_name = name.replace(".example.yaml", ".yaml").replace(".example.yml", ".yml").replace(".example.md", ".md")
            stat = f.stat()
            overrides = name in own and name in defaults
            res.append({
                "name": name,
                "path": _rel(f),
                "kind": "fragment" if f.suffix == ".md" else "node",
                "suffix": f.suffix,
                "is_example": is_example,
                "base_name": base_name,
                "size": stat.st_size,
                "updated_at": stat.st_mtime,
                "source": "override" if overrides else ("custom" if name in own else "default"),
                "same_as_default": overrides and _same_as_default(f),
            })
        return res

    @router.get("/node-files/{filename}/raw")
    def get_node_file_raw(filename: str) -> dict[str, Any]:
        own, default = _node_file_targets(filename)
        return _read_text(own if own.is_file() else default, source=_layer_source(own, default))

    @router.put("/node-files/{filename}/raw")
    def update_node_file_raw(filename: str, payload: RawContent) -> dict[str, Any]:
        own, default = _node_file_targets(filename)
        return _write_overlay(own, default, payload.content, payload.base_hash)

    @router.delete("/node-files/{filename}")
    def delete_node_file(filename: str) -> dict[str, Any]:
        """改过的仓库默认文件恢复成默认；实例自己加的直接删。"""
        own, default = _node_file_targets(filename)
        if not own.exists():
            if default.exists():
                raise HTTPException(status_code=409, detail=f"{filename} 是仓库自带的默认文件，没有改过，不用恢复")
            raise HTTPException(status_code=404, detail="File not found")
        _remove(own)
        return {"ok": True, "restored": default.exists()}

    # ----- Drawtools (NovelAI image generation) -----
    # 前端的名字 → drawtools 目录里的相对路径。
    _DRAWTOOLS_PROMPTS = {
        "top-system": "prompts/novelai/top-system.md",
        "output-format": "prompts/novelai/output-format.md",
        "tag-guide": "prompts/novelai/tag-guide.md",
    }

    def _drawtools_read_with_example(name: str) -> dict[str, Any]:
        path = config_layout.drawtools_file(workspace_root, name)
        example_path = config_layout.drawtools_example(name)
        target = path if path.exists() else example_path
        if not target.is_file():
            return {"content": "", "exists": False, "using_example": False, "hash": "", "path": _rel(path)}
        return {
            **_read_text(target),
            "exists": path.exists(),
            "using_example": not path.exists() and example_path.exists(),
            "path": _rel(path),
            "example_path": _rel(example_path),
        }

    def _drawtools_write(name: str, payload: RawContent) -> dict[str, Any]:
        path = config_layout.drawtools_file(workspace_root, name)
        return _write_overlay(path, config_layout.drawtools_example(name), payload.content, payload.base_hash)

    def _drawtools_prompt(name: str) -> str:
        if name not in _DRAWTOOLS_PROMPTS:
            raise HTTPException(status_code=404, detail="Unknown prompt template")
        return _DRAWTOOLS_PROMPTS[name]

    @router.get("/drawtools")
    def get_drawtools_bundle() -> dict[str, Any]:
        return {
            "settings": _drawtools_read_with_example("settings.yaml"),
            "character_tags": _drawtools_read_with_example("character_tags.yaml"),
            "prompts": {
                "top_system": _drawtools_read_with_example(_DRAWTOOLS_PROMPTS["top-system"]),
                "output_format": _drawtools_read_with_example(_DRAWTOOLS_PROMPTS["output-format"]),
                "tag_guide": _drawtools_read_with_example(_DRAWTOOLS_PROMPTS["tag-guide"]),
            },
        }

    @router.post("/drawtools/init")
    def init_drawtools_configs() -> dict[str, Any]:
        return {"ok": True, "created": config_layout.ensure_drawtools_configs(workspace_root)}

    @router.get("/drawtools/settings/raw")
    def get_drawtools_settings_raw() -> dict[str, Any]:
        return _drawtools_read_with_example("settings.yaml")

    @router.put("/drawtools/settings/raw")
    def update_drawtools_settings_raw(payload: RawContent) -> dict[str, Any]:
        return _drawtools_write("settings.yaml", payload)

    @router.get("/drawtools/characters/raw")
    def get_drawtools_characters_raw() -> dict[str, Any]:
        return _drawtools_read_with_example("character_tags.yaml")

    @router.put("/drawtools/characters/raw")
    def update_drawtools_characters_raw(payload: RawContent) -> dict[str, Any]:
        return _drawtools_write("character_tags.yaml", payload)

    @router.get("/drawtools/prompts/{name}/raw")
    def get_drawtools_prompt_raw(name: str) -> dict[str, Any]:
        return _drawtools_read_with_example(_drawtools_prompt(name))

    @router.put("/drawtools/prompts/{name}/raw")
    def update_drawtools_prompt_raw(name: str, payload: RawContent) -> dict[str, Any]:
        return _drawtools_write(_drawtools_prompt(name), payload)

    @router.post("/drawtools/cleanup")
    def cleanup_drawtools_attachments() -> dict[str, Any]:
        # 按包导入，不往 sys.path 插 drawtools 目录：插了它的 common、cleanup 会盖住 supervisor 里的同名模块。
        from tools.drawtools.cleanup import cleanup_novelai_attachments
        return cleanup_novelai_attachments(force=True)

    # ----- Model channels (config/models.yaml) -----
    @router.get("/config/raw")
    def get_config_raw() -> dict[str, Any]:
        return _read_text(config_layout.models_path(workspace_root))

    @router.put("/config/raw")
    def update_config_raw(payload: RawContent) -> dict[str, Any]:
        return _write_text(config_layout.models_path(workspace_root), payload.content, payload.base_hash)

    # ----- Runtime -----
    # 编辑的是实例那份；还没改过时显示仓库默认，保存后落成实例那份，运行时逐键盖在默认上面。
    @router.get("/runtime/raw")
    def get_runtime() -> dict[str, Any]:
        default, own = config_layout.runtime_layers(workspace_root)
        if own.is_file():
            return _read_text(own, source="override")
        return _read_text(default, source="default")

    @router.put("/runtime/raw")
    def update_runtime(payload: RawContent) -> dict[str, Any]:
        default, own = config_layout.runtime_layers(workspace_root)
        return _write_overlay(own, default, payload.content, payload.base_hash)

    @router.delete("/runtime/raw")
    def reset_runtime() -> dict[str, Any]:
        _default, own = config_layout.runtime_layers(workspace_root)
        return {"ok": True, "restored": _remove(own)}

    # ----- 旧的 QQ 热载配置路由 -----
    # 没刷新的旧控制台和照文档写的运维脚本还在调这几条，行为与 /v1/platforms/qq/... 完全一致。
    from . import platform_api
    from platforms.builtin import QQ_NAPCAT

    @router.get("/qq/raw")
    def get_qq_raw() -> dict[str, Any]:
        return platform_api.read_config_raw(workspace_root, QQ_NAPCAT)

    @router.put("/qq/raw")
    def update_qq_raw(payload: RawContent) -> dict[str, Any]:
        return platform_api.write_config_raw(workspace_root, QQ_NAPCAT, payload.content, base_hash=payload.base_hash)

    @router.get("/qq/state")
    def get_qq_state() -> dict[str, Any]:
        return platform_api.config_state(workspace_root, QQ_NAPCAT)

    @router.post("/qq/trigger/dry-run")
    def qq_trigger_dry_run(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return platform_api.trigger_dry_run(workspace_root, QQ_NAPCAT, payload)

    # ----- Policy -----
    @router.get("/policy/raw")
    def get_policy() -> dict[str, Any]:
        p = config_layout.policy_path(workspace_root)
        return _read_text(p if p.is_file() else config_layout.default_template(config_layout.POLICY_TEMPLATE))

    @router.put("/policy/raw")
    def update_policy(payload: RawContent) -> dict[str, Any]:
        return _write_overlay(
            config_layout.policy_path(workspace_root),
            config_layout.default_template(config_layout.POLICY_TEMPLATE),
            payload.content,
            payload.base_hash,
        )

    # ----- Schedules -----
    @router.get("/schedules/raw")
    def get_schedules() -> dict[str, Any]:
        p = schedules_path(workspace_root)
        if not p.exists():
            # 空指纹表示「文件还不存在」：保存时有人抢先建了，照样按冲突报。
            return {"content": "schedules: []", "hash": "", "path": _rel(p)}
        return _read_text(p)

    @router.put("/schedules/raw")
    def update_schedules(payload: RawContent) -> dict[str, Any]:
        with schedules_lock(workspace_root):
            return _write_text(schedules_path(workspace_root), payload.content, payload.base_hash)

    # ----- Skills -----
    def _skills_dir() -> Path:
        return config_layout.skills_dir(workspace_root)

    @router.get("/skills")
    def list_skills() -> list[dict[str, Any]]:
        skills_dir = _skills_dir()
        if not skills_dir.is_dir():
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
    def get_skill_raw(name: str) -> dict[str, Any]:
        return _read_text(_safe_path(_skills_dir(), name) / "SKILL.md")

    @router.put("/skills/{name}/raw")
    def update_skill_raw(name: str, payload: RawContent) -> dict[str, Any]:
        return _write_text(_safe_path(_skills_dir(), name) / "SKILL.md", payload.content, payload.base_hash)

    @router.post("/skills")
    def create_skill(payload: NodeCreate) -> dict[str, Any]:
        md = _safe_path(_skills_dir(), payload.id) / "SKILL.md"
        if md.exists():
            raise HTTPException(status_code=409, detail="Skill already exists")
        return _create_text(md, payload.content)

    @router.delete("/skills/{name}")
    def delete_skill(name: str) -> dict[str, Any]:
        p = _safe_path(_skills_dir(), name)
        if p.is_dir():
            shutil.rmtree(p)
        return {"ok": True}

    # ----- Tools -----
    def _catalog() -> list[ToolEntry]:
        return tool_catalog(workspace_root)

    def _tool_entry(name: str) -> ToolEntry:
        """工具名对应的外部脚本。

        子目录里的工具、SPEC 名和文件名对不上的都得走这里。内置和插件工具没有源码，
        直接说清楚 —— 拼一条 tools/{name}.py 出来只会让人看到「文件不存在」。
        """
        for entry in _catalog():
            if entry.name != name:
                continue
            if not entry.editable or not entry.file or entry.rel is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"{name} 是{'插件' if entry.source == 'plugin' else '内置'}工具，没有可编辑的源码文件",
                )
            return entry
        raise HTTPException(status_code=404, detail="Tool not found")

    def _tool_override(entry: ToolEntry) -> Path:
        """改这个工具时写的位置：config/tools/ 里同一相对路径。仓库那份不动，改了就落成覆盖。"""
        return _safe_path(config_layout.custom_tools_dir(workspace_root), str(entry.rel))

    @router.get("/tools")
    def list_tools() -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in _catalog()]

    @router.get("/tools/{name}/raw")
    def get_tool_raw(name: str) -> dict[str, Any]:
        entry = _tool_entry(name)
        return _read_text(workspace_root / str(entry.file), origin=entry.origin)

    @router.put("/tools/{name}/raw")
    def update_tool_raw(name: str, payload: RawContent) -> dict[str, Any]:
        entry = _tool_entry(name)
        return _write_overlay(_tool_override(entry), workspace_root / str(entry.file), payload.content, payload.base_hash)

    @router.post("/tools")
    def create_tool(payload: NodeCreate) -> dict[str, Any]:
        p = _safe_path(config_layout.custom_tools_dir(workspace_root), payload.id, ".py")
        # 重名的新文件建了也没用：注册表只认其中一个，内置工具还会直接盖掉外部同名。
        if p.exists() or any(entry.name == payload.id for entry in _catalog()):
            raise HTTPException(status_code=409, detail="Tool already exists")
        return _create_text(p, payload.content)

    @router.delete("/tools/{name}")
    def delete_tool(name: str) -> dict[str, Any]:
        """删实例自己加的工具；改过的仓库工具删掉覆盖，恢复成仓库那份。"""
        entry = _tool_entry(name)
        if entry.origin == "bundled":
            raise HTTPException(status_code=409, detail=f"{name} 是仓库自带的工具，没有改过，删不掉；不想用它就在节点里去掉")
        target = _tool_override(entry)
        if not target.exists():
            raise HTTPException(status_code=404, detail="Tool not found")
        _remove(target)
        return {"ok": True, "restored": entry.origin == "override"}

    # ----- MCP Clients -----
    @router.get("/mcp-clients")
    def list_mcp_clients() -> list[dict[str, Any]]:
        p = config_layout.mcp_path(workspace_root)
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
    def get_mcp_clients_raw() -> dict[str, Any]:
        p = config_layout.mcp_path(workspace_root)
        if not p.exists():
            return {"content": "version: 1\nclients: {}\n", "hash": "", "path": _rel(p)}
        return _read_text(p)

    @router.put("/mcp-clients/raw")
    def update_mcp_clients_raw(payload: RawContent) -> dict[str, Any]:
        return _write_text(config_layout.mcp_path(workspace_root), payload.content, payload.base_hash)

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
        from toolbox.registry import external_tool_files, extract_tool_spec

        path = _node_file(node_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Node not found")

        data = _read_yaml(path)
        ta = data.get("tool_access", {})
        if isinstance(ta, str):
            ta = {"mode": ta}
        elif not isinstance(ta, dict):
            ta = {}
        mode = str(ta.get("mode") or "none").strip()

        def _names(key: str) -> list[str]:
            return [str(x).strip() for x in (ta.get(key) or []) if str(x or "").strip()]

        external: dict[str, bool] = {}
        for item in external_tool_files(workspace_root):
            spec, _timeout = extract_tool_spec(item.path)
            # 同名时先出现的那份生效，和注册表一致。
            if spec and isinstance(spec.get("name"), str) and spec["name"] not in external:
                external[spec["name"]] = isinstance(spec.get("guard"), dict)

        known = {entry.name for entry in _catalog()}

        # 报的是「这个节点实际能调什么」，两种模式算法不同但答案同质。
        # 只报 deny 名单的话，最宽的那些节点反而什么注解都看不到。
        if mode == "allowlist":
            names = _names("allow")
            # 白名单里写错的名字注入时静默忽略，那一条从来没生效过。
            dead = [name for name in names if name not in known]
        elif mode == "all":
            denied = set(_names("deny"))
            names = sorted(known - denied)
            # 禁令写错的名字更要紧：以为禁掉了，其实那个工具一直开着。
            dead = sorted(denied - known)
        else:
            names, dead = [], []
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
        return {"node_id": node_id, "mode": mode, "dead_names": dead, "tools": rows}

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
