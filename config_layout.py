"""实例的配置放在哪、怎么写。

config/ 装这个 bot 怎么运作的全部配置，能整份和别的实例共享；data/ 是它攒下的私有状态；.env 是实例身份。
仓库自带的默认节点和 runtime 在代码目录的 defaults/ 下，实例的 config/ 里只放改过的那几份。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from file_lock import exclusive_lock
from toolbox.text_files import encode_as, newline_of

_log = logging.getLogger(__name__)

CODE_ROOT = Path(__file__).resolve().parent

CONFIG_DIR = "config"
NODES_DIR = f"{CONFIG_DIR}/nodes"
RUNTIME_FILE = f"{CONFIG_DIR}/runtime.yaml"
MODELS_FILE = f"{CONFIG_DIR}/models.yaml"
POLICY_FILE = f"{CONFIG_DIR}/policy.yaml"
MCP_FILE = f"{CONFIG_DIR}/mcp.yaml"
SKILLS_DIR = f"{CONFIG_DIR}/skills"
TOOLS_DIR = f"{CONFIG_DIR}/tools"
PLUGINS_DIR = f"{CONFIG_DIR}/plugins"
PLUGINS_FILE = f"{CONFIG_DIR}/plugins.yaml"
DRAWTOOLS_DIR = f"{CONFIG_DIR}/drawtools"
# 平台的连接令牌是实例身份，config/ 可能整份共享出去，所以放 data/。
PLATFORM_SECRETS_FILE = "data/platform_secrets.yaml"
# 能跟着 config/ 一起共享的 API 密钥。端口、地址、平台令牌这类实例身份留在各自的 .env，同名时 .env 的算数。
SHARED_SECRETS_FILE = f"{CONFIG_DIR}/secrets.env"

# 多开时清单文件放在几个实例共用的数据根下，数据根就按它的位置认。单实例部署不设它。
INSTANCES_FILE_ENV = "CLONOTH_INSTANCES_FILE"
# 数据根下放共享组的目录：每组一个子目录，组的配置在子目录的 config/ 里，成员的 config/ 是指向它的链接。
SHARED_DIR = "shared"
SHARE_GROUP_PATTERN = r"[a-z][a-z0-9_]{1,31}"
_SHARE_GROUP_RE = re.compile(rf"^{SHARE_GROUP_PATTERN}$")
# 多开实例的工作区里软链回代码目录的几个目录，和共享表情包库的链接位置。
CODE_LINKS: tuple[str, ...] = ("adapters", "engine", "plugins", "tools", "defaults")
STICKERS_LINK = "data/stickers"

DEFAULTS_DIR = "defaults"
DEFAULT_NODES_DIR = f"{DEFAULTS_DIR}/nodes"
DEFAULT_RUNTIME_FILE = f"{DEFAULTS_DIR}/runtime.yaml"
# 新建实例时抄进 config/ 或 data/ 的模板，相对 defaults/ 写。
MODELS_TEMPLATE = "models.example.yaml"
POLICY_TEMPLATE = "policy.example.yaml"
PLATFORM_SECRETS_TEMPLATE = "platform_secrets.example.yaml"

# 挪进 config/ 之前的位置。启动时照这张表搬一次，之后谁也不再读它们，留着给退回旧版本用。
LEGACY_MODELS_FILE = "data/config.yaml"
LEGACY_POLICY_FILE = "data/policy.yaml"
LEGACY_MCP_FILE = "data/mcp_clients.yaml"
LEGACY_SKILLS_DIR = "skills"
LEGACY_TOOLS_DIR = "tools"
LEGACY_DRAWTOOLS_DIR = "tools/drawtools"
# 平台密钥原先是 data/config.yaml 顶层的这一段。
LEGACY_PLATFORMS_KEY = "platforms"

# 绘图的运行时配置，相对 drawtools 目录；仓库只发同名的 .example 模板。
DRAWTOOLS_FILES: tuple[str, ...] = (
    "settings.yaml",
    "character_tags.yaml",
    "prompts/novelai/top-system.md",
    "prompts/novelai/output-format.md",
    "prompts/novelai/tag-guide.md",
)

# 装着明文密钥的配置，新建时只给属主读写。
_PRIVATE_MODE = 0o600
_SECRET_BEARING = frozenset({
    MODELS_FILE, POLICY_FILE, MCP_FILE, PLUGINS_FILE, PLATFORM_SECRETS_FILE, SHARED_SECRETS_FILE, f"{DRAWTOOLS_DIR}/settings.yaml",
})

LAYOUT_VERSION = 1
_LAYOUT_MARKER = "data/.config_layout.json"
_LAYOUT_LOCK = "data/.config_layout.lock"
# 改造前跑过的工作区必然留着其中之一：policy 和事件流在第一次启动时就建好了。
_LEGACY_WORKSPACE_MARKS: tuple[str, ...] = (LEGACY_MODELS_FILE, LEGACY_POLICY_FILE, LEGACY_MCP_FILE, "data/events.jsonl")


# ---------------------------------------------------------------------------
#  位置
# ---------------------------------------------------------------------------

def defaults_dir() -> Path:
    return CODE_ROOT / DEFAULTS_DIR


def default_nodes_dir() -> Path:
    return CODE_ROOT / DEFAULT_NODES_DIR


def default_runtime_path() -> Path:
    return CODE_ROOT / DEFAULT_RUNTIME_FILE


def default_template(name: str) -> Path:
    return defaults_dir() / name


def bundled_tools_dir() -> Path:
    """随代码仓库发布的脚本工具。多开实例的工作区里那个 tools 只是软链回这里。"""
    return CODE_ROOT / LEGACY_TOOLS_DIR


def workspace_tools_dir(workspace_root: Path | str) -> Path:
    """从工作区看过去的仓库工具目录。按它算出的路径在单实例和多开实例里都能相对工作区写出来。"""
    return Path(workspace_root) / LEGACY_TOOLS_DIR


def drawtools_code_dir() -> Path:
    return CODE_ROOT / LEGACY_DRAWTOOLS_DIR


def config_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / CONFIG_DIR


def nodes_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / NODES_DIR


def runtime_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / RUNTIME_FILE


def models_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / MODELS_FILE


def policy_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / POLICY_FILE


def mcp_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / MCP_FILE


def skills_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / SKILLS_DIR


def custom_tools_dir(workspace_root: Path | str) -> Path:
    """本实例自己加的脚本工具，和仓库发布的那批分开放，同名同路径时盖过仓库那份。"""
    return Path(workspace_root) / TOOLS_DIR


def custom_plugins_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / PLUGINS_DIR


def plugins_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / PLUGINS_FILE


def drawtools_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / DRAWTOOLS_DIR


def platform_secrets_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / PLATFORM_SECRETS_FILE


def shared_secrets_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / SHARED_SECRETS_FILE


def example_name(name: str) -> str:
    """模板的文件名：settings.yaml → settings.example.yaml，prompts/a.md → prompts/a.example.md。"""
    path = Path(name)
    return (path.parent / f"{path.stem}.example{path.suffix}").as_posix() if path.suffix else f"{name}.example"


def drawtools_file(workspace_root: Path | str, name: str) -> Path:
    return drawtools_dir(workspace_root) / name


def drawtools_example(name: str) -> Path:
    return drawtools_code_dir() / example_name(name)


def _contained(path: Path, base: Path) -> bool:
    """path 顺着软链走到头还在 base 里。目录里被人放了个指向别处的软链，读出来的就是目录外的文件。"""
    try:
        path.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return False
    return True


def drawtools_effective(workspace_root: Path | str, name: str) -> Path:
    """绘图配置实际读哪份：实例自己的，没有就读仓库的模板。都不在各自目录里时返回实例那份的位置。"""
    own = drawtools_file(workspace_root, name)
    if own.exists() and _contained(own, drawtools_dir(workspace_root)):
        return own
    example = drawtools_example(name)
    if example.exists() and _contained(example, drawtools_code_dir()):
        return example
    return own


def ensure_drawtools_configs(workspace_root: Path | str) -> list[str]:
    """绘图配置缺的那几份从仓库模板抄进实例的 config/drawtools/，返回新建的那几份。已有的不动。"""
    root = Path(workspace_root)
    created: list[str] = []
    for name in DRAWTOOLS_FILES:
        source, target = drawtools_example(name), drawtools_file(root, name)
        if target.exists() or not source.is_file():
            continue
        try:
            write_config_text(target, source.read_bytes().decode("utf-8"), base_hash="", mode=private_mode_for(root, target))
        except ConfigConflict:
            continue
        created.append(logical_path(root, target))
    return created


def node_search_dirs(workspace_root: Path | str) -> tuple[Path, Path, Path]:
    """节点文件依次在哪找：系统节点跟着代码走，同名时最优先；然后是实例改过的，最后是仓库默认。"""
    root = Path(workspace_root)
    return root / "engine" / "system_nodes", nodes_dir(root), default_nodes_dir()


def is_default_node(path: Path) -> bool:
    return Path(path).parent == default_nodes_dir()


def node_edit_target(workspace_root: Path | str, path: Path) -> Path:
    """改 path 这份节点时实际写哪：仓库默认的那份不动，改动落成实例目录里的同名覆盖；其余原地改。"""
    return nodes_dir(workspace_root) / Path(path).name if is_default_node(path) else Path(path)


def logical_path(workspace_root: Path | str, path: Path) -> str:
    """文件在工作区里叫什么。仓库默认那几份在多开实例的工作区外面，按代码目录里的相对路径叫，policy 也认这个名字。"""
    for base in (Path(workspace_root), CODE_ROOT):
        try:
            return Path(path).relative_to(base).as_posix()
        except ValueError:
            continue
    return Path(path).as_posix()


def fragment_file(workspace_root: Path | str, name: str) -> Path | None:
    """节点里 {{include:名字}} 引到的片段：实例的 > 仓库默认 > 两边的同名模板。都没有返回 None。"""
    own, default = nodes_dir(workspace_root), default_nodes_dir()
    for candidate in (name, example_name(name)):
        for base in (own, default):
            path = base / candidate
            if path.is_file() and _contained(path, base):
                return path
    return None


def runtime_layers(workspace_root: Path | str) -> tuple[Path, Path]:
    """runtime 的两层：仓库默认在下，实例改过的那份逐键盖在上面。"""
    return default_runtime_path(), runtime_path(workspace_root)


def is_secret_bearing(rel_path: str) -> bool:
    return rel_path in _SECRET_BEARING


# ---------------------------------------------------------------------------
#  共享组与受信链接
# ---------------------------------------------------------------------------

def data_root() -> Path | None:
    """几个实例共用的数据根：清单文件所在的目录。单实例部署没配清单，也就没有数据根。"""
    raw = str(os.environ.get(INSTANCES_FILE_ENV) or "").strip()
    return Path(raw).expanduser().parent if raw else None


def shared_root() -> Path | None:
    root = data_root()
    return root / SHARED_DIR if root is not None else None


def is_share_group_name(name: str) -> bool:
    return bool(_SHARE_GROUP_RE.fullmatch(str(name or "")))


def share_group_dir(group: str) -> Path:
    """组的目录。没有数据根或名字不合规时抛 ValueError：拿任意字符串拼出来的路径可能落在任何地方。"""
    root = shared_root()
    if root is None:
        raise ValueError("单实例部署没有数据根，用不了共享组")
    if not is_share_group_name(group):
        raise ValueError(f"组名不合规：{group!r}")
    return root / group


def share_group_config_dir(group: str) -> Path:
    return share_group_dir(group) / CONFIG_DIR


def is_dir_link(path: Path | str) -> bool:
    """软链，或者 Windows 上没有建软链的权限时退而用的目录联接。"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    junction_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    return junction_tag is not None and getattr(info, "st_reparse_tag", None) == junction_tag


def config_share_group(workspace_root: Path | str) -> str | None:
    """本实例在哪个共享组：config/ 是指向数据根 shared/<组名>/config 的链接就是那个组，否则 None。"""
    link = config_dir(workspace_root)
    shared = shared_root()
    if shared is None or not is_dir_link(link):
        return None
    try:
        relative = link.resolve(strict=True).relative_to(shared.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    parts = relative.parts
    if len(parts) != 2 or parts[1] != CONFIG_DIR or not is_share_group_name(parts[0]):
        return None
    return parts[0]


def trusted_links(workspace_root: Path | str) -> tuple[tuple[str, Path], ...]:
    """工作区里允许通到外面去的链接，以及各自必须通到的真实目录：代码目录那几个、共享表情包库、共享组的配置。

    路径判定只认这几处。链接被改去指别处就不再算数，工作区里随手建的软链也一概不认：否则建一个链接就能把任意目录变成工作区。
    """
    root = Path(workspace_root)
    links: list[tuple[str, Path]] = [(name, CODE_ROOT / name) for name in CODE_LINKS]
    data = data_root()
    if data is not None:
        links.append((STICKERS_LINK, data / "stickers"))
    group = config_share_group(root)
    if group is not None:
        links.append((CONFIG_DIR, share_group_config_dir(group)))
    return tuple(links)


def workspace_logical_path(workspace_root: Path | str, resolved: Path) -> str | None:
    """解析到底的 resolved 在工作区里叫什么：工作区里的写相对路径，经受信链接通到外面的按链接名写，都不是返回 None。"""
    root = Path(workspace_root)
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        pass
    for name, expected in trusted_links(root):
        link = root / name
        if not is_dir_link(link):
            continue
        try:
            target = link.resolve(strict=True)
            if target != expected.resolve():
                continue
            relative = resolved.relative_to(target)
        except (OSError, RuntimeError, ValueError):
            continue
        return name if relative == Path(".") else f"{name}/{relative.as_posix()}"
    return None


class ConfigLayoutError(RuntimeError):
    """工作区的 config/ 用不了：共享组的链接断了，或者这个名字被一个普通文件占着。"""


def check_config_dir(workspace_root: Path | str) -> None:
    """config/ 要么不存在，要么是能进得去的目录。断掉的共享组链接必须停下等人：接着跑会在链接旁边另起一份空配置。"""
    link = config_dir(workspace_root)
    if is_dir_link(link) and not link.is_dir():
        try:
            target = os.readlink(link)
        except OSError:
            target = "?"
        raise ConfigLayoutError(
            f"{link} 指向的共享配置不在了（{target}）：把链接改回存在的组，或者删掉链接、从 data/config.archived-* 挑一份改名回 config"
        )
    if (link.exists() or link.is_symlink()) and not link.is_dir():
        raise ConfigLayoutError(f"{link} 不是目录")


# ---------------------------------------------------------------------------
#  dotenv
# ---------------------------------------------------------------------------

_ENV_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
# 按路径缓存解析结果，按 file_stamp 认出改动。
_ENV_FILE_CACHE: dict[str, tuple[tuple[int, int, int], Mapping[str, str]]] = {}
_EMPTY_ENV: Mapping[str, str] = MappingProxyType({})


def env_line_key(line: str) -> str:
    """dotenv 里这一行给哪个键赋值；注释、空行和不像赋值的行是空串。"""
    match = _ENV_ASSIGN.match(line)
    return match.group(1) if match else ""


# 不加引号 dotenv 和 systemd 的 EnvironmentFile 都能原样读回的值。
_BARE_ENV_VALUE = re.compile(r"[A-Za-z0-9_\-.:/+=@,%~]*")


def env_assignment(key: str, value: str) -> str:
    """写进 dotenv 的一行。带空格、# 的值加单引号，否则 # 后面会被当成注释截掉；
    值里本身有单引号或反斜杠的，两种解析器对引号里的转义理解不一致，只能照原样写。"""
    if _BARE_ENV_VALUE.fullmatch(value) or "'" in value or "\\" in value:
        return f"{key}={value}"
    return f"{key}='{value}'"


def read_env_file(path: Path) -> Mapping[str, str]:
    """dotenv 文件的 {键: 值}，读不到是空表。解析器和进程启动时灌 .env 用的是同一个，两边对同一行的理解才一致。"""
    stamp = file_stamp(path)
    if stamp is None:
        return _EMPTY_ENV
    key = str(path)
    cached = _ENV_FILE_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    from dotenv import dotenv_values

    try:
        raw = dotenv_values(path, encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        _log.warning("读不了 %s，当它是空的", path, exc_info=True)
        return _EMPTY_ENV
    values: Mapping[str, str] = MappingProxyType({
        str(name): str(value).strip() for name, value in raw.items() if value is not None and str(value).strip()
    })
    _ENV_FILE_CACHE[key] = (stamp, values)
    return values


def shared_secrets(workspace_root: Path | str) -> Mapping[str, str]:
    """共享密钥文件的现状。别的实例改了它，这边下一次取值就换上，不用重启。"""
    return read_env_file(shared_secrets_path(workspace_root))


def edit_env_file(path: Path, updates: Mapping[str, str | None], *, mode: int | None = _PRIVATE_MODE) -> bool:
    """逐键改一份 dotenv，返回改没改：值为 None 的键删掉，其余写成「键=值」；别的行原样留着。

    读改写都在锁里：共享组里几个实例可能同时改同一份 secrets.env，各读各写会丢掉对方刚写的键。
    """
    with locked(path):
        try:
            text = path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            text = ""
        lines = text.splitlines()
        pending = dict(updates)
        kept: list[str] = []
        written: set[str] = set()
        for line in lines:
            key = env_line_key(line)
            if key not in pending:
                kept.append(line)
                continue
            value = pending[key]
            # 同一个键写了好几行时只留第一行的位置，后面几行一并去掉：dotenv 取最后一行，留着就改不动。
            if value is not None and key not in written:
                kept.append(env_assignment(key, value))
                written.add(key)
        for key, value in pending.items():
            if value is not None and key not in written:
                kept.append(env_assignment(key, value))
                written.add(key)
        if kept == lines:
            return False
        write_locked(path, "\n".join(kept) + "\n" if kept else "", mode=mode)
        return True


# ---------------------------------------------------------------------------
#  policy 眼里的旧名字
# ---------------------------------------------------------------------------

# 挪了位置的文件在 policy 眼里还叫原来的名字：老 policy.yaml 给旧路径写的规则照样护住新位置，几个名字取最严的那条。
# 仓库默认的节点和 runtime 在 policy 眼里就是实例那份：读得到默认提示词和读得到实例提示词是一回事。
_POLICY_ALIASES: tuple[tuple[str, str], ...] = (
    (MODELS_FILE, LEGACY_MODELS_FILE),
    (POLICY_FILE, LEGACY_POLICY_FILE),
    (MCP_FILE, LEGACY_MCP_FILE),
    (f"{SKILLS_DIR}/", f"{LEGACY_SKILLS_DIR}/"),
    (f"{DRAWTOOLS_DIR}/", f"{LEGACY_DRAWTOOLS_DIR}/"),
    (f"{TOOLS_DIR}/", f"{LEGACY_TOOLS_DIR}/"),
    (f"{DEFAULT_NODES_DIR}/", f"{NODES_DIR}/"),
    (DEFAULT_RUNTIME_FILE, RUNTIME_FILE),
)


def policy_names(rel_path: str) -> tuple[str, ...]:
    """判 policy 时这个工作区相对路径要按哪几个名字各判一遍：它自己在前，旧名字在后。"""
    names = [rel_path]
    for current, legacy in _POLICY_ALIASES:
        if current.endswith("/"):
            if rel_path.startswith(current):
                names.append(legacy + rel_path[len(current):])
        elif rel_path == current:
            names.append(legacy)
    return tuple(dict.fromkeys(names))


def policy_literal_aliases(literal: str) -> tuple[str, ...]:
    """命令行里查敏感路径用的字面片段，按旧名字写的规则也要认出新位置：data/ 连带 config/models.yaml 这几份。"""
    text = str(literal or "").strip()
    if not text:
        return ()
    found: list[str] = []
    for current, legacy in _POLICY_ALIASES:
        covered = legacy.startswith(text) if text.endswith("/") else legacy == text or legacy.rstrip("/") == text
        if covered:
            found.append(current)
    return tuple(dict.fromkeys(found))


# ---------------------------------------------------------------------------
#  写入
# ---------------------------------------------------------------------------

class ConfigConflict(RuntimeError):
    """写前比对的指纹对不上：打开编辑器之后，这份配置已经被别处改过。"""


class ConfigBackupError(OSError):
    """覆盖前留 .bak 失败，原文件一个字节都没动。没有退路的覆盖比保存失败危险得多，所以宁可不写。"""


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    """文件内容的指纹，不存在是空串。编辑器拿着它写回，写入方据此认出中途被改过。"""
    try:
        return bytes_hash(path.read_bytes())
    except FileNotFoundError:
        return ""


def file_stamp(path: Path) -> tuple[int, int, int] | None:
    """不读内容的版本指纹：修改时间、大小和 inode，不存在是 None。按文件做缓存的地方都拿它比对。

    保存都是同目录临时文件替换，每次换 inode：同一个时钟刻度里写进等长内容，只有 inode 认得出来。
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


def _lock_path(path: Path) -> Path:
    # .env 这类本来就以点开头的，不再叠一个点。
    name = path.name
    return path.with_name(f"{name}.lock" if name.startswith(".") else f".{name}.lock")


@contextlib.contextmanager
def locked(path: Path):
    """独占写这份配置。锁文件挨着它放：共享组里几个实例改同一份，拿的是同一把锁。"""
    with exclusive_lock(_lock_path(path)):
        yield


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_locked(
    path: Path,
    content: str,
    *,
    base_hash: str | None = None,
    backup: bool = False,
    mode: int | None = None,
) -> str:
    """调用方已经拿着 locked(path) 时用的写入，返回写完的指纹。其余同 write_config_text。"""
    current = file_hash(path)
    if base_hash is not None and base_hash != current:
        raise ConfigConflict(f"{path.name} 已被别处改过，刷新后再改")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    with contextlib.suppress(OSError):
        existing_mode = path.stat().st_mode & 0o777
    if backup and current:
        try:
            shutil.copy2(path, path.with_name(f"{path.name}.bak"))
        except OSError as exc:
            raise ConfigBackupError(f"备份 {path.name} 失败，未写入：{exc}") from exc
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        # 在写句柄上 fsync：Windows 对只读句柄做 fsync 会报 EBADF。
        with open(tmp, "wb") as stream:
            stream.write(encode_as(content, newline_of(path)))
            stream.flush()
            os.fsync(stream.fileno())
        target_mode = existing_mode if existing_mode is not None else mode
        if target_mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, target_mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)
    return file_hash(path)


def write_config_text(
    path: Path,
    content: str,
    *,
    base_hash: str | None = None,
    backup: bool = False,
    mode: int | None = None,
) -> str:
    """整份写回一份配置，返回写完的指纹。

    跨进程加锁、同目录临时文件替换：bot 随时在读，读到半份就是整份配置塌掉；共享组的目录软链里替换也不会断链。
    已有文件的换行符和权限照旧；新文件按 mode 给权限。给了 base_hash 就先比对，对不上抛 ConfigConflict，一个字节都不写。
    backup 在覆盖前留一份 .bak。
    """
    with locked(path):
        return write_locked(path, content, base_hash=base_hash, backup=backup, mode=mode)


def private_mode_for(workspace_root: Path | str, path: Path) -> int | None:
    """这份配置新建时该给的权限：装密钥的只给属主，其余交给 umask。"""
    try:
        rel = Path(path).relative_to(Path(workspace_root)).as_posix()
    except ValueError:
        return None
    return _PRIVATE_MODE if is_secret_bearing(rel) else None


def remove_config_file(path: Path, *, base_hash: str | None = None) -> bool:
    """删一份配置（恢复默认就是删掉实例那份），返回删没删。给了 base_hash 同样先比对。"""
    with locked(path):
        current = file_hash(path)
        if base_hash is not None and base_hash != current:
            raise ConfigConflict(f"{path.name} 已被别处改过，刷新后再改")
        if not current and not path.exists():
            return False
        path.unlink()
        _fsync_dir(path.parent)
        return True


# ---------------------------------------------------------------------------
#  搬家：改造前的位置 → config/
# ---------------------------------------------------------------------------

@dataclass
class LayoutReport:
    """启动时那次搬家做了什么。写进 data/.config_layout.json，健康检查据此报告。"""

    version: int = LAYOUT_VERSION
    migrated_at: str = ""
    copied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 搬家那一刻代码目录 tools/ 里不随仓库发布的脚本，相对 tools/ 写。旧文件留着给退回旧版本用，新版本不再认它们。
    legacy_tools: list[str] = field(default_factory=list)
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "migrated_at": self.migrated_at,
            "copied": list(self.copied),
            "warnings": list(self.warnings),
            "legacy_tools": list(self.legacy_tools),
        }


def _marker(workspace_root: Path) -> Path:
    return workspace_root / _LAYOUT_MARKER


def read_layout_marker(workspace_root: Path | str) -> dict[str, Any] | None:
    try:
        data = json.loads(_marker(Path(workspace_root)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _marker_version(workspace_root: Path) -> int:
    data = read_layout_marker(workspace_root) or {}
    version = data.get("version")
    return version if isinstance(version, int) and not isinstance(version, bool) else 0


def _rel(workspace_root: Path, path: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return path.as_posix()


def _copy_file(workspace_root: Path, source: Path, target: Path, report: LayoutReport) -> None:
    """新位置已经有了就不动：那是搬过之后改的，比旧文件新。"""
    if target.exists() or not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    report.copied.append(f"{_rel(workspace_root, source)} → {_rel(workspace_root, target)}")


def _copy_tree(workspace_root: Path, source: Path, target: Path, report: LayoutReport) -> None:
    """逐个文件照 _copy_file 搬，已经在新位置的那几个不覆盖。"""
    if not source.is_dir():
        return
    for item in sorted(source.rglob("*")):
        if item.is_file() and "__pycache__" not in item.parts:
            _copy_file(workspace_root, item, target / item.relative_to(source), report)


_PLATFORMS_LINE = re.compile(rf"^{LEGACY_PLATFORMS_KEY}\s*:")


def split_platform_secrets(text: str) -> tuple[str, dict[str, Any] | None]:
    """把顶层 platforms 段从模型配置原文里摘出来，返回 (剩下的原文, 那一段)。没有这一段原样返回。

    按行摘，其余的注释和换行不动；摘完解析出来和摘之前少了这一段对不上时，退回整份重新序列化。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text, None
    if not isinstance(data, dict) or LEGACY_PLATFORMS_KEY not in data:
        return text, None
    raw_section = data.pop(LEGACY_PLATFORMS_KEY)
    section = raw_section if isinstance(raw_section, dict) else None
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if _PLATFORMS_LINE.match(line)), None)
    if start is not None:
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line[0].isspace() and not line.lstrip().startswith("#"):
                break
            end += 1
        # 紧挨着下一段、顶格写的注释和空行是写给下一段的。
        while end > start + 1 and (not lines[end - 1].strip() or not lines[end - 1][0].isspace()):
            end -= 1
        # 这一段前后各有一行空行时摘掉一行，免得原地留下两行空白。
        if 0 < start and not lines[start - 1].strip() and end < len(lines) and not lines[end].strip():
            end += 1
        remaining = "".join(lines[:start] + lines[end:])
        try:
            if (yaml.safe_load(remaining) or {}) == data:
                return remaining, section
        except yaml.YAMLError:
            pass
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True), section


def _migrate_models(workspace_root: Path, report: LayoutReport) -> None:
    source = workspace_root / LEGACY_MODELS_FILE
    target = models_path(workspace_root)
    if target.exists() or not source.is_file():
        return
    # 按字节解码：read_text 会把 CRLF 折成 LF，搬过去的文件换行就跟原文件不一样了。
    text = source.read_bytes().decode("utf-8")
    remaining, section = split_platform_secrets(text)
    write_config_text(target, remaining, mode=_PRIVATE_MODE)
    report.copied.append(f"{LEGACY_MODELS_FILE} → {MODELS_FILE}")
    if section is None:
        return
    secrets_file = platform_secrets_path(workspace_root)
    if secrets_file.exists():
        report.warnings.append(f"{LEGACY_MODELS_FILE} 里有 platforms 段，但 {PLATFORM_SECRETS_FILE} 已经在了，没有覆盖，请手动核对")
        return
    body = yaml.safe_dump(section, sort_keys=False, allow_unicode=True)
    write_config_text(secrets_file, body, mode=_PRIVATE_MODE)
    report.copied.append(f"{LEGACY_MODELS_FILE} 的 platforms 段 → {PLATFORM_SECRETS_FILE}")


def _untracked_bundled_tools(report: LayoutReport) -> list[Path] | None:
    """代码目录 tools/ 下不随仓库发布的 .py：控制台和 agent 以前都把新工具写在那里，几个实例共用。认不出来返回 None。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(CODE_ROOT), "ls-files", "--others", "--exclude-standard", "-z", "--", LEGACY_TOOLS_DIR],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.warnings.append(f"看不出代码目录 tools/ 下哪些是自己加的工具（{exc}），没有搬，请手动挪进 {TOOLS_DIR}/")
        return None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"git 退出码 {result.returncode}"
        report.warnings.append(f"看不出代码目录 tools/ 下哪些是自己加的工具（{detail}），没有搬，请手动挪进 {TOOLS_DIR}/")
        return None
    found: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        rel = raw.decode("utf-8", "replace").strip()
        if not rel.endswith(".py") or rel.rsplit("/", 1)[-1] == "__init__.py":
            continue
        path = CODE_ROOT / rel
        if path.is_file():
            found.append(path)
    return found


def _is_legacy_workspace(workspace_root: Path) -> bool:
    return any((workspace_root / rel).exists() for rel in _LEGACY_WORKSPACE_MARKS)


def _migrate_custom_tools(workspace_root: Path, report: LayoutReport) -> None:
    """改造前的工作区把那批工具抄进自己的 config/tools/；改造后新建的实例默认全隔离，不继承别人的工具。"""
    found = _untracked_bundled_tools(report) or []
    report.legacy_tools = sorted(path.relative_to(bundled_tools_dir()).as_posix() for path in found)
    if not _is_legacy_workspace(workspace_root):
        return
    for path in found:
        _copy_file(workspace_root, path, custom_tools_dir(workspace_root) / path.relative_to(bundled_tools_dir()), report)


def legacy_tool_rels(workspace_root: Path | str) -> frozenset[str]:
    """仓库 tools/ 里不该当成仓库工具的那几个：搬家时已抄进 config/tools/，或者本来就是别的实例加的。"""
    raw = (read_layout_marker(workspace_root) or {}).get("legacy_tools")
    return frozenset(str(item) for item in raw if isinstance(item, str)) if isinstance(raw, list) else frozenset()


def _write_marker(workspace_root: Path, report: LayoutReport) -> None:
    path = _marker(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
    write_config_text(path, body)


def ensure_layout(workspace_root: Path | str) -> LayoutReport:
    """把改造前散在 data/、skills/、代码目录里的配置搬进 config/，每个工作区只搬一次。

    只复制不删：旧文件原样留着，退回旧版本时还能用；新位置已有的一概不覆盖。几个进程同时启动时只有一个在搬。
    """
    root = Path(workspace_root)
    check_config_dir(root)
    (root / "data").mkdir(parents=True, exist_ok=True)
    with exclusive_lock(root / _LAYOUT_LOCK):
        if _marker_version(root) >= LAYOUT_VERSION:
            return LayoutReport(skipped=True)
        report = LayoutReport(migrated_at=datetime.now(timezone.utc).isoformat())
        config_dir(root).mkdir(parents=True, exist_ok=True)
        _migrate_models(root, report)
        _copy_file(root, root / LEGACY_POLICY_FILE, policy_path(root), report)
        _copy_file(root, root / LEGACY_MCP_FILE, mcp_path(root), report)
        _copy_tree(root, root / LEGACY_SKILLS_DIR, skills_dir(root), report)
        for name in DRAWTOOLS_FILES:
            _copy_file(root, drawtools_code_dir() / name, drawtools_file(root, name), report)
        _migrate_custom_tools(root, report)
        _write_marker(root, report)
    for line in report.copied:
        _log.info("配置搬到新位置：%s", line)
    for line in report.warnings:
        _log.warning("配置搬家：%s", line)
    return report


def _legacy_pairs(workspace_root: Path) -> list[tuple[Path, Path]]:
    root = Path(workspace_root)
    pairs = [
        (root / LEGACY_MODELS_FILE, models_path(root)),
        (root / LEGACY_POLICY_FILE, policy_path(root)),
        (root / LEGACY_MCP_FILE, mcp_path(root)),
    ]
    pairs.extend((drawtools_code_dir() / name, drawtools_file(root, name)) for name in DRAWTOOLS_FILES)
    return pairs


def layout_status(workspace_root: Path | str) -> dict[str, Any]:
    """健康检查用：搬过没有、搬家时留下的提醒，以及搬完之后旧位置又被改过的文件（多半是退回旧版本那阵子改的）。"""
    root = Path(workspace_root)
    marker = read_layout_marker(root)
    stale: list[str] = []
    for legacy, current in _legacy_pairs(root):
        try:
            if legacy.is_file() and current.is_file() and legacy.stat().st_mtime > current.stat().st_mtime + 1:
                stale.append(f"{_rel(root, legacy)} 比 {_rel(root, current)} 新")
        except OSError:
            continue
    return {
        "migrated": marker is not None and _marker_version(root) >= LAYOUT_VERSION,
        "migrated_at": (marker or {}).get("migrated_at", ""),
        "warnings": [str(item) for item in (marker or {}).get("warnings", []) if str(item).strip()],
        "stale_legacy": stale,
    }
