"""插件包静态校验和不可变版本目录。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_FILES = 512
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_WEB_EXTENSIONS = {".js", ".mjs", ".css", ".json", ".html", ".txt", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".wav", ".mp4", ".webm"}


def safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240 or any(c in value for c in '\\:<>"|?*') or any(ord(c) < 32 for c in value):
        raise ValueError("插件包路径无效")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or part.endswith((".", " ")) for part in parts):
        raise ValueError("插件包路径不能是绝对路径或包含空段、上级目录")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
    if any(part.split(".", 1)[0].lower() in reserved for part in parts):
        raise ValueError("插件包路径包含系统保留名称")
    return PurePosixPath(value).as_posix()


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def checked_directory(path: Path, boundary: Path) -> Path:
    if not path.resolve().is_relative_to(boundary.resolve()):
        raise ValueError("插件版本目录超出插件目录")
    cursor = path
    while cursor != boundary:
        if is_link(cursor):
            raise ValueError("插件包不能包含符号链接或目录链接")
        cursor = cursor.parent
        if cursor == cursor.parent:
            raise ValueError("插件版本目录无效")
    return path


def read_zip(content: bytes) -> dict[str, bytes]:
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("插件 ZIP 不能超过 16 MiB")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_PACKAGE_FILES:
                raise ValueError("插件包最多包含 512 个条目")
            for item in entries:
                original = item.orig_filename
                name = safe_path(original[:-1] if original.endswith("/") else original)
                if "__pycache__" in PurePosixPath(name).parts or name.endswith((".pyc", ".pyo")):
                    raise ValueError("插件包不能包含 Python 缓存文件")
                if name.casefold() in seen:
                    raise ValueError("插件包包含重复或大小写冲突的路径")
                seen.add(name.casefold())
                mode = stat.S_IFMT(item.external_attr >> 16)
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or item.flag_bits & 1:
                    raise ValueError("插件包不能包含链接、设备或加密文件")
                if item.is_dir():
                    continue
                total += item.file_size
                if item.file_size > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES:
                    raise ValueError("插件包解压大小超过限制")
                with archive.open(item) as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                if len(raw) != item.file_size or len(raw) > MAX_FILE_BYTES:
                    raise ValueError("插件包文件大小无效")
                files[name] = raw
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError(f"插件 ZIP 无法读取：{exc}") from exc
    if "plugin.json" not in files:
        prefixes = {name.split("/", 1)[0] for name in files}
        if len(prefixes) == 1:
            prefix = next(iter(prefixes)) + "/"
            if prefix + "plugin.json" in files:
                files = {name[len(prefix):]: raw for name, raw in files.items()}
    return files


def read_directory(directory: Path) -> dict[str, bytes]:
    if not directory.is_dir():
        raise FileNotFoundError("插件包版本目录不存在")
    if is_link(directory):
        raise ValueError("插件包目录不能是链接")
    files: dict[str, bytes] = {}
    total = 0
    seen: set[str] = set()
    count = 0
    for parent, directories, names in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories if name != "__pycache__"]
        count += len(directories) + len(names)
        if count > MAX_PACKAGE_FILES:
            raise ValueError("插件包条目数量超过限制")
        for name in directories + names:
            child = Path(parent) / name
            if is_link(child):
                raise ValueError("插件包不能包含符号链接或目录链接")
            if child.is_dir():
                continue
            if not child.is_file():
                raise ValueError("插件包只能包含普通文件")
            rel = safe_path(child.relative_to(directory).as_posix())
            if rel.endswith((".pyc", ".pyo")):
                raise ValueError("插件包不能包含 Python 缓存文件")
            if rel.casefold() in seen:
                raise ValueError("插件包包含大小写冲突的路径")
            seen.add(rel.casefold())
            if len(files) >= MAX_PACKAGE_FILES or child.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("插件包文件数量或大小超过限制")
            raw = child.read_bytes()
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES:
                raise ValueError("插件包大小超过限制")
            files[rel] = raw
    return files


def inspect_package(files: dict[str, bytes]) -> dict[str, Any]:
    from .store import MAX_SOURCE_BYTES, inspect_source, validate_id, validate_metadata

    paths: dict[str, tuple[str, bool]] = {}
    total = 0
    for name, raw in files.items():
        safe_path(name)
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES:
            raise ValueError("插件包大小超过限制")
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            node = (path, index == len(parts))
            previous = paths.setdefault(path.casefold(), node)
            if previous != node:
                raise ValueError("插件包包含文件类型或大小写冲突的路径")
        if len(paths) > MAX_PACKAGE_FILES:
            raise ValueError("插件包条目数量超过限制")
    if "plugin.json" not in files:
        raise ValueError("插件包根目录需要 plugin.json")
    if len(files["plugin.json"]) > 128 * 1024:
        raise ValueError("plugin.json 不能超过 128 KiB")
    try:
        manifest = json.loads(files["plugin.json"].decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"plugin.json 无效：{exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("plugin.json 必须是字典")
    validate_id(manifest.get("id", ""))
    for key in ("name", "version"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"plugin.json.{key} 必须是非空字符串")
    if type(manifest.get("sdk_version")) is not int or manifest["sdk_version"] != 1:
        raise ValueError("插件仅支持 sdk_version: 1")
    validate_metadata(manifest)
    backend = manifest.get("backend")
    frontend = manifest.get("frontend")
    styles, assets = manifest.get("styles", []), manifest.get("assets", [])
    if not backend and not frontend:
        raise ValueError("插件包至少需要 backend 或 frontend 入口")
    if not isinstance(styles, list) or not isinstance(assets, list):
        raise ValueError("插件 styles 和 assets 必须是路径列表")
    for entry in ([backend] if backend else []) + ([frontend] if frontend else []) + styles + assets:
        if safe_path(entry) not in files:
            raise ValueError(f"插件包声明的文件不存在：{entry}")
    if backend:
        if not backend.endswith(".py"):
            raise ValueError("插件 backend 必须是 .py 文件")
        inspect_source(files[backend].decode("utf-8-sig"), backend)
    if frontend and Path(frontend).suffix.lower() not in {".js", ".mjs"}:
        raise ValueError("插件 frontend 必须是 JavaScript 模块")
    if any(not path.endswith(".css") for path in styles):
        raise ValueError("插件 styles 只能包含 CSS 文件")
    public = ([frontend] if frontend else []) + styles + assets
    for path in public:
        if path == "plugin.json" or path == backend or Path(path).suffix.lower() not in _WEB_EXTENSIONS or any(part.lower().split(".")[0] in {"config", "settings", "secret", "secrets"} for part in PurePosixPath(path).parts):
            raise ValueError(f"文件不能作为前端资源公开：{path}")
    for name, raw in files.items():
        if name.endswith(".py"):
            if len(raw) > MAX_SOURCE_BYTES:
                raise ValueError("插件 Python 文件不能超过 2 MiB")
            try:
                compile(raw.decode("utf-8-sig"), name, "exec")
            except (ValueError, SyntaxError, UnicodeError) as exc:
                raise ValueError(f"插件语法错误：{name}: {exc}") from exc
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(files.items())}
    package_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    def asset(path: str) -> dict[str, str]:
        return {"path": path, "hash": hashes[path]}

    return {
        "metadata": manifest, "package_hash": package_hash,
        "backend": backend, "source_hash": hashes.get(backend, ""),
        "frontend": {"entry": asset(frontend) if frontend else None, "styles": [asset(path) for path in styles], "assets": [asset(path) for path in assets]},
        "files": [{"path": name, "hash": hashes[name], "size": len(raw)} for name, raw in sorted(files.items())],
    }


def version_directory(directory: Path, plugin_id: str, package_hash: str) -> Path:
    from .store import validate_id

    validate_id(plugin_id)
    if not isinstance(package_hash, str) or not _HASH.fullmatch(package_hash):
        raise ValueError("插件包版本指纹无效")
    return checked_directory(directory / ".packages" / plugin_id / package_hash, directory)


def materialize(directory: Path, files: dict[str, bytes], package: dict[str, Any]) -> Path:
    target = version_directory(directory, package["metadata"]["id"], package["package_hash"])
    if target.exists():
        existing = inspect_package(read_directory(target))
        if existing["package_hash"] != package["package_hash"]:
            raise ValueError("已安装插件版本的文件被外部修改")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".pending-{secrets.token_hex(12)}")
    staging.mkdir()
    try:
        for name, raw in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            os.rename(staging, target)
        except OSError:
            if not target.is_dir() or inspect_package(read_directory(target))["package_hash"] != package["package_hash"]:
                raise
    finally:
        if staging.exists():
            import shutil

            checked_directory(staging, directory)
            shutil.rmtree(staging)
    return target
