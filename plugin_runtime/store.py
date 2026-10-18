"""插件文件、静态元信息和共享配置。读取清单不会执行插件。"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from . import packages

import config_layout

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_CONFIG_BYTES = 128 * 1024
_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,79}\Z")
_SELECTORS = ("nodes", "channels", "providers", "purposes")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def validate_id(plugin_id: str) -> str:
    if not isinstance(plugin_id, str) or not _ID.fullmatch(plugin_id) or plugin_id.lower() in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}:
        raise ValueError("插件 ID 必须以字母开头，只能包含字母、数字、下划线和连字符")
    return plugin_id


def _literal(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values):
            if key is None:
                unpacked = _literal(value, values)
                if not isinstance(unpacked, dict):
                    raise ValueError("字典展开值必须是字典")
                result.update(unpacked)
            else:
                result[_literal(key, values)] = _literal(value, values)
        return result
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(item, values) for item in node.elts]
    return ast.literal_eval(node)


def inspect_source(content: str, filename: str) -> dict[str, Any]:
    if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("插件文件不能超过 2 MiB")
    try:
        tree = ast.parse(content, filename=filename)
        compile(tree, filename, "exec")
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"插件语法错误：{exc}") from exc
    values: dict[str, Any] = {}
    functions: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(statement.name)
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            try:
                values[target.id] = _literal(value, values)
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                if target.id in {"PLUGIN_META", "PLUGIN_INFO"}:
                    raise ValueError(f"{filename}:{statement.lineno} 的 {target.id} 必须使用静态字典或常量") from exc
                continue
    metadata = values.get("PLUGIN_META", values.get("PLUGIN_INFO", {}))
    validate_metadata(metadata)
    if not functions.intersection({"setup", "register"}) and not (metadata.get("handler_class") and metadata.get("hook_points")):
        raise ValueError("插件需要 setup(api)、register(registry) 或 PLUGIN_META.handler_class/hook_points 入口")
    json.dumps(metadata, allow_nan=False)
    return metadata


def validate_metadata(metadata: dict[str, Any]) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("PLUGIN_META 必须是可静态读取的字典")
    if type(metadata.get("priority", 100)) is not int:
        raise ValueError("PLUGIN_META.priority 必须是整数")
    nested = metadata.get("metadata")
    schema = metadata.get("params_schema", (nested if isinstance(nested, dict) else {}).get("params_schema", []))
    if not isinstance(schema, list):
        raise ValueError("插件参数声明 params_schema 必须是列表")
    keys: set[str] = set()
    for field in schema:
        if not isinstance(field, dict):
            raise ValueError("插件参数声明必须是字典")
        key = field.get("key") or field.get("name")
        if not isinstance(key, str) or not key.strip() or key in keys:
            raise ValueError("每个插件参数需要唯一的字符串 key 或 name")
        keys.add(key)
        for name in ("label", "type", "kind", "description", "desc", "hint"):
            if name in field and not isinstance(field[name], str):
                raise ValueError(f"参数 {key} 的 {name} 必须是字符串")
        for name in ("options", "choices"):
            if name in field and not isinstance(field[name], list):
                raise ValueError(f"参数 {key} 的 {name} 必须是列表")
        if "required" in field and not isinstance(field["required"], bool):
            raise ValueError(f"参数 {key} 的 required 必须是布尔值")
    processes = metadata.get("processes", ["engine", "supervisor"])
    if not isinstance(processes, list) or not processes or any(not isinstance(value, str) or value not in {"engine", "supervisor"} for value in processes):
        raise ValueError("PLUGIN_META.processes 必须是包含 engine 或 supervisor 的非空列表")
    json.dumps(metadata, allow_nan=False)


def parameter_schema(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    nested = metadata.get("metadata")
    raw = metadata.get("params_schema", (nested if isinstance(nested, dict) else {}).get("params_schema", []))
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def parameter_defaults(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        str(item.get("key") or item.get("name")): item["default"]
        for item in parameter_schema(metadata)
        if (item.get("key") or item.get("name")) and "default" in item
    }


def _validate_config(config: dict[str, Any], metadata: dict[str, Any]) -> None:
    if len(json.dumps(config, ensure_ascii=False, allow_nan=False).encode()) > MAX_CONFIG_BYTES:
        raise ValueError("插件配置不能超过 128 KiB")
    for field in parameter_schema(metadata):
        key = field.get("key") or field.get("name")
        if key not in config:
            if field.get("required"):
                raise ValueError(f"缺少插件参数：{key}")
            continue
        value = config[key]
        kind = field.get("type") or field.get("kind", "text")
        if kind in {"toggle", "boolean", "bool"} and not isinstance(value, bool):
            raise ValueError(f"参数 {key} 必须是布尔值")
        if kind in {"text", "string", "password", "textarea"} and not isinstance(value, str):
            raise ValueError(f"参数 {key} 必须是字符串")
        if kind in {"int", "integer", "float", "number"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError(f"参数 {key} 必须是有效数字")
            if kind in {"int", "integer"} and int(value) != value:
                raise ValueError(f"参数 {key} 必须是整数")
            for bound, compare in (("minimum", lambda n: value < n), ("maximum", lambda n: value > n)):
                limit = field.get(bound)
                if isinstance(limit, (int, float)) and compare(limit):
                    raise ValueError(f"参数 {key} 超出 {bound}={limit} 的限制")
        choices = field.get("options", field.get("choices"))
        if kind in {"enum", "select"} and isinstance(choices, list):
            allowed = [choice.get("value") if isinstance(choice, dict) else choice for choice in choices]
            if value not in allowed:
                raise ValueError(f"参数 {key} 不在允许的选项中")


def _selectors(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict) or set(raw) - set(_SELECTORS):
        raise ValueError("插件作用范围必须使用 nodes/channels/providers/purposes")
    result: dict[str, list[str]] = {}
    for key in _SELECTORS:
        items = raw.get(key, [])
        if not isinstance(items, list) or len(items) > 200 or any(not isinstance(x, str) or len(x) > 256 for x in items):
            raise ValueError(f"插件作用范围 {key} 必须是字符串列表")
        result[key] = list(dict.fromkeys(x.strip() for x in items if x.strip()))
    return result


class PluginStore:
    def __init__(self, workspace_root: Path | str):
        self.root = Path(workspace_root)
        self.directory = config_layout.custom_plugins_dir(self.root)
        self.settings_path = config_layout.plugins_path(self.root)

    def _check_paths(self) -> None:
        config = config_layout.config_dir(self.root).resolve()
        for path in (self.directory, self.settings_path):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or not path.resolve().is_relative_to(config):
                raise ValueError("插件目录或配置文件不能通过链接指向其他位置")

    def _settings(self) -> dict[str, Any]:
        self._check_paths()
        try:
            raw = self.settings_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "plugins": {}}
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError(f"插件配置无法读取：{exc}") from exc
        if not isinstance(doc, dict) or doc.get("version", 1) != 1 or not isinstance(doc.get("plugins"), dict):
            raise ValueError("插件配置必须包含 version: 1 和 plugins 字典")
        return doc

    def _files(self) -> dict[str, tuple[Path, str]]:
        files: dict[str, tuple[Path, str]] = {}
        for directory, source in ((self.root / "plugins", "bundled"), (self.directory, "custom")):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_") or path.name.endswith(".disabled.py") or not path.is_file():
                    continue
                if source == "custom" and (path.is_symlink() or not path.resolve().is_relative_to(self.directory.resolve())):
                    continue
                try:
                    plugin_id = validate_id(path.stem)
                except ValueError:
                    continue
                files[plugin_id] = (path, source)
            for path in sorted(directory.iterdir()):
                if path.name.startswith(("_", ".")) or packages.is_link(path):
                    continue
                if path.is_dir() and (path / "plugin.json").is_file():
                    plugin_id = path.name
                elif source == "custom" and path.name.endswith(".package.json"):
                    plugin_id = path.name[:-len(".package.json")]
                else:
                    continue
                try:
                    validate_id(plugin_id)
                except ValueError:
                    continue
                files[plugin_id] = (path, source)
        return files

    def _package(self, plugin_id: str, path: Path) -> tuple[dict[str, Any], Path]:
        if path.is_dir():
            files = packages.read_directory(path)
            package = packages.inspect_package(files)
            if package["metadata"]["id"] != plugin_id:
                raise ValueError("plugin.json.id 必须与插件目录名称一致")
            root = packages.materialize(self.directory, files, package)
        else:
            if path.stat().st_size > 4096:
                raise ValueError("插件包版本指针过大")
            pointer = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(pointer, dict):
                raise ValueError("插件包版本指针无效")
            root = packages.version_directory(self.directory, plugin_id, pointer.get("package_hash", ""))
            package = packages.inspect_package(packages.read_directory(root))
            if package["package_hash"] != pointer["package_hash"]:
                raise ValueError("已安装插件版本的文件被外部修改")
        if package["metadata"]["id"] != plugin_id:
            raise ValueError("plugin.json.id 必须与插件目录或版本指针名称一致")
        return package, root

    def definitions(self) -> list[dict[str, Any]]:
        settings = self._settings()["plugins"]
        definitions: list[dict[str, Any]] = []
        for plugin_id, (path, source) in self._files().items():
            error = None
            metadata: dict[str, Any] = {}
            source_hash = ""
            kind = "module" if path.suffix == ".py" else "package"
            details: dict[str, Any] = {"kind": kind, "package_root": None, "package_hash": "", "files": [], "frontend": {"entry": None, "styles": [], "assets": []}}
            backend_path = path if kind == "module" else None
            try:
                if kind == "package":
                    package, package_root = self._package(plugin_id, path)
                    metadata, source_hash = package["metadata"], package["source_hash"]
                    details.update({key: package[key] for key in ("package_hash", "files", "frontend")})
                    details["package_root"] = package_root
                    backend_path = package_root / package["backend"] if package["backend"] else None
                else:
                    if path.stat().st_size > MAX_SOURCE_BYTES:
                        raise ValueError("插件文件不能超过 2 MiB")
                    raw = path.read_bytes()
                    source_hash = config_layout.bytes_hash(raw)
                    metadata = inspect_source(raw.decode("utf-8-sig"), path.name)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                error = str(exc)
            entry = settings.get(plugin_id, {})
            if not isinstance(entry, dict):
                entry, error = {}, "插件配置项必须是字典"
            enabled = entry.get("enabled", source == "bundled")
            config = parameter_defaults(metadata)
            selectors = {key: [] for key in _SELECTORS}
            try:
                if not isinstance(enabled, bool) or not isinstance(entry.get("config", {}), dict):
                    raise ValueError("插件 enabled 必须是布尔值，config 必须是字典")
                config.update(entry.get("config", {}))
                selectors = _selectors(entry.get("selectors", {}))
                _validate_config(config, metadata)
            except (ValueError, TypeError) as exc:
                error = str(exc)
            definitions.append({
                **details, "id": plugin_id, "path": backend_path, "origin_path": path, "filename": path.name, "source": source,
                "source_hash": source_hash, "metadata": metadata, "enabled": bool(enabled),
                "config": config, "selectors": selectors, "error": error,
                "desired_hash": _digest([source, details["package_hash"] or source_hash, entry, error]),
            })
        return sorted(definitions, key=lambda item: item["id"])

    def generation(self, definitions: list[dict[str, Any]] | None = None) -> str:
        items = self.definitions() if definitions is None else definitions
        return _digest([(item["id"], item["desired_hash"]) for item in items])

    def get(self, plugin_id: str) -> dict[str, Any]:
        validate_id(plugin_id)
        for item in self.definitions():
            if item["id"] == plugin_id:
                return item
        raise FileNotFoundError(f"插件不存在：{plugin_id}")

    @staticmethod
    def public(item: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in item.items() if key not in {"path", "package_root", "origin_path"}}
        meta = item["metadata"]
        result.update({key: str(meta.get(key) or default) for key, default in (
            ("name", item["id"]), ("version", "unknown"), ("description", ""), ("author", ""),
        )})
        result["params_schema"] = parameter_schema(meta)
        result["source_editable"] = item["source"] == "custom" and item["kind"] == "module"
        return result

    def source(self, plugin_id: str) -> dict[str, Any]:
        item = self.get(plugin_id)
        path = item["path"] or (item["package_root"] / "plugin.json" if item["package_root"] else None)
        if path is None:
            raise ValueError(item["error"] or "插件包没有可读取的源码")
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("插件文件不能超过 2 MiB")
        return {"content": raw.decode("utf-8-sig"), "hash": config_layout.bytes_hash(raw), "editable": item["source"] == "custom" and item["kind"] == "module", "filename": path.name}

    def _write_settings_with(self, doc: dict[str, Any], operation: Callable[[], Any]) -> None:
        previous = self.settings_path.read_bytes() if self.settings_path.exists() else None
        config_layout.write_locked(self.settings_path, yaml.safe_dump(doc, allow_unicode=True), backup=True, mode=0o600)
        try:
            operation()
        except BaseException:
            if previous is None:
                self.settings_path.unlink(missing_ok=True)
            else:
                config_layout.write_locked(self.settings_path, previous.decode("utf-8"), mode=0o600)
            raise

    def install(self, filename: str, content: str) -> dict[str, Any]:
        if Path(filename).name != filename or "/" in filename or "\\" in filename or not filename.lower().endswith(".py"):
            raise ValueError("请上传单个 .py 文件")
        plugin_id = validate_id(re.sub(r"[^a-zA-Z0-9_-]+", "_", filename[:-3]).strip("_").lower())
        inspect_source(content, filename)
        self._check_paths()
        with config_layout.locked(self.settings_path):
            doc = self._settings()
            if any(existing.casefold() == plugin_id.casefold() for existing in self._files()):
                raise config_layout.ConfigConflict("插件 ID 已存在，请选择该插件后更新源码")
            target = self.directory / f"{plugin_id}.py"
            if target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
                raise ValueError("插件文件不能是符号链接")
            # 删除后重装也从停用状态开始，不能继承旧的启用标志。
            doc["plugins"][plugin_id] = {"enabled": False, "config": {}, "selectors": {}}
            self._write_settings_with(doc, lambda: config_layout.write_config_text(target, content, base_hash="", mode=0o600))
            return self.public(self.get(plugin_id))

    def update_source(self, plugin_id: str, content: str, base_hash: str) -> dict[str, Any]:
        inspect_source(content, f"{validate_id(plugin_id)}.py")
        with config_layout.locked(self.settings_path):
            item = self.get(plugin_id)
            if item["source"] != "custom":
                raise PermissionError("随项目发布的插件源码只读")
            if item["kind"] == "package":
                raise PermissionError("插件包源码只读，请上传完整 ZIP 更新")
            config_layout.write_config_text(item["path"], content, base_hash=base_hash, backup=True, mode=0o600)
            return self.public(self.get(plugin_id))

    def configure(self, plugin_id: str, *, enabled: bool, config: dict[str, Any], selectors: dict[str, Any], base_hash: str) -> dict[str, Any]:
        with config_layout.locked(self.settings_path):
            item = self.get(plugin_id)
            if item["desired_hash"] != base_hash:
                raise config_layout.ConfigConflict("插件已被别处修改，刷新后再保存")
            if not isinstance(enabled, bool) or not isinstance(config, dict):
                raise ValueError("插件 enabled 必须是布尔值，config 必须是字典")
            if enabled:
                if item["kind"] == "package":
                    self._package(plugin_id, item["origin_path"])
                else:
                    inspect_source(item["path"].read_text(encoding="utf-8-sig"), item["filename"])
            merged = {**parameter_defaults(item["metadata"]), **config}
            _validate_config(merged, item["metadata"] if enabled else {})
            entry = {"enabled": enabled, "config": config, "selectors": _selectors(selectors)}
            doc = self._settings()
            doc["plugins"][plugin_id] = entry
            config_layout.write_locked(self.settings_path, yaml.safe_dump(doc, allow_unicode=True), backup=True, mode=0o600)
            return self.public(self.get(plugin_id))

    def remove(self, plugin_id: str, base_hash: str) -> None:
        with config_layout.locked(self.settings_path):
            item = self.get(plugin_id)
            if item["source"] != "custom":
                raise PermissionError("随项目发布的插件不能删除，可将它停用")
            if item["desired_hash"] != base_hash:
                raise config_layout.ConfigConflict("插件已被别处修改，刷新后再删除")
            doc = self._settings()
            doc["plugins"][plugin_id] = {"enabled": False, "config": {}, "selectors": {}}
            if item["kind"] == "package":
                def remove_package() -> None:
                    origin = item["origin_path"]
                    manual = self.directory / plugin_id
                    retired = None
                    if manual.is_dir() and not packages.is_link(manual):
                        import secrets

                        retired = manual.with_name(f".removed-{plugin_id}-{secrets.token_hex(6)}")
                        manual.rename(retired)
                    try:
                        if origin != manual:
                            config_layout.remove_config_file(origin)
                    except BaseException:
                        if retired is not None:
                            retired.rename(manual)
                        raise
                self._write_settings_with(doc, remove_package)
            else:
                self._write_settings_with(doc, lambda: config_layout.remove_config_file(item["path"], base_hash=item["source_hash"]))

    def install_package(self, filename: str, content: bytes, base_hash: str | None = None) -> dict[str, Any]:
        if Path(filename).name != filename or "/" in filename or "\\" in filename or not filename.lower().endswith(".zip"):
            raise ValueError("请上传单个 .zip 插件包")
        return self._install_package_files(packages.read_zip(content), base_hash)

    def import_directory(self, directory: Path | str, base_hash: str | None = None) -> dict[str, Any]:
        return self._install_package_files(packages.read_directory(Path(directory)), base_hash)

    def _install_package_files(self, files: dict[str, bytes], base_hash: str | None) -> dict[str, Any]:
        package = packages.inspect_package(files)
        plugin_id = package["metadata"]["id"]
        self._check_paths()
        with config_layout.locked(self.settings_path):
            existing = next((item for item in self.definitions() if item["id"].casefold() == plugin_id.casefold()), None)
            if existing:
                if existing["id"] != plugin_id:
                    raise config_layout.ConfigConflict("插件 ID 与已有插件存在大小写冲突")
                if existing["source"] != "custom" or existing["kind"] != "package":
                    raise config_layout.ConfigConflict("插件 ID 已存在，不能用插件包覆盖单文件或内置插件")
                if base_hash != existing["desired_hash"]:
                    raise config_layout.ConfigConflict("更新插件包需要当前 desired_hash，刷新后再上传")
            elif base_hash:
                raise config_layout.ConfigConflict("插件已被删除，刷新后重新安装")
            doc = self._settings()
            if not existing:
                doc["plugins"][plugin_id] = {"enabled": False, "config": {}, "selectors": {}}
            entry = doc["plugins"].get(plugin_id, {})
            if entry.get("enabled"):
                _validate_config({**parameter_defaults(package["metadata"]), **entry.get("config", {})}, package["metadata"])
            packages.materialize(self.directory, files, package)
            pointer = self.directory / f"{plugin_id}.package.json"
            if packages.is_link(pointer):
                raise ValueError("插件包版本指针不能是链接")
            content = json.dumps({"package_hash": package["package_hash"]}) + "\n"
            self._write_settings_with(doc, lambda: config_layout.write_config_text(pointer, content, mode=0o600))
            return self.public(self.get(plugin_id))

    def frontend_catalog(self) -> dict[str, Any]:
        definitions = self.definitions()
        plugins = [{
            "id": item["id"], "desired_hash": item["desired_hash"], "package_hash": item["package_hash"],
            "entry": item["frontend"]["entry"], "styles": item["frontend"]["styles"], "assets": item["frontend"]["assets"],
            "config": item["config"], "metadata": item["metadata"],
        } for item in definitions if item["enabled"] and not item["error"] and item["frontend"]["entry"]]
        errors = [{"id": item["id"], "error": item["error"], "enabled": True}
                  for item in definitions if item["enabled"] and item["error"] and item["kind"] == "package"]
        return {"plugins": plugins, "errors": errors, "generation": self.generation(definitions)}

    def asset(self, plugin_id: str, package_hash: str, path: str) -> tuple[bytes, str]:
        self._check_paths()
        path = packages.safe_path(path)
        root = packages.version_directory(self.directory, plugin_id, package_hash)
        files = packages.read_directory(root)
        package = packages.inspect_package(files)
        if package["metadata"]["id"] != plugin_id or package["package_hash"] != package_hash:
            raise ValueError("插件资源版本无效")
        frontend = package["frontend"]
        allowed = ([frontend["entry"]] if frontend["entry"] else []) + frontend["styles"] + frontend["assets"]
        if path not in {entry["path"] for entry in allowed}:
            raise FileNotFoundError("插件前端资源不存在")
        import mimetypes

        return files[path], mimetypes.guess_type(path)[0] or "application/octet-stream"
