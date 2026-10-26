"""工具碰到的目录往上找 AGENTS.md / CLAUDE.md，过 read_file 策略后在下一步推理前注入，作为该目录下项目的约定。

策略 auto 的读进来（单份、整个任务各有字数上限），approval_required 的只提一句在哪，deny 的什么都不说。
系统任务和写了 project_instructions: false 的节点不做。
"""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import config_layout
from clonoth_runtime import classify_path, load_policy_config, parse_bool, parse_extra_roots
from engine.inference.loop_state import build_tool_context
from engine.tool_step import tool_outcome

from .result import hook_result

logger = logging.getLogger(__name__)

PLUGIN_META = {
    "handler_class": "ProjectInstructions",
    "hook_points": [
        ("after_tool_call", "collect"),
        ("before_step", "inject"),
        ("on_task_end", "forget"),
        ("on_task_error", "forget"),
    ],
    # before_step 里排在抢占注入（100）和压缩（50）之后、待办提醒（10）之前
    "priority": 20,
}

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
FILE_CHAR_LIMIT = 8_000
TOTAL_CHAR_LIMIT = 16_000
# 总量只剩这么点时不再截一小段塞进来，改成只提一句
_MIN_USEFUL_CHARS = 500
# UTF-8 一个字最多 4 字节，多读一点才看得出超没超上限
_READ_BYTE_LIMIT = FILE_CHAR_LIMIT * 4 + 4
MESSAGE_KEY = "_project_instructions"
LOADED_EVENT = "project_instructions_loaded"
NODE_SWITCH = "project_instructions"

_FILE_TOOLS = frozenset({"read_file", "write_file", "apply_diff", "edit_lines"})
_LISTING_TOOLS = frozenset({"read_file", "list_dir"})
_SEARCH_TOOLS = frozenset({"glob", "search_in_files"})
_GLOB_CHARS = frozenset("*?[{")
_INSTRUCTION_NAMES = frozenset(name.lower() for name in INSTRUCTION_FILES)

_HEADER = (
    "[PROJECT_INSTRUCTIONS]\n"
    "以下是本任务碰到的目录里的项目说明文件，写的是所在目录下项目的约定；在这些目录里读代码、改文件、执行命令时遵守。"
)
_FOOTER = "[/PROJECT_INSTRUCTIONS]"


@dataclass(frozen=True)
class Candidate:
    """一份找到的说明文件。key 是解析到底的绝对路径，display 是给策略判定和模型看的路径。"""

    key: str
    path: Path
    display: str
    directory: str


@dataclass(frozen=True)
class Block:
    key: str
    path: str
    status: str
    chars: int
    text: str


@dataclass
class TaskState:
    """一个任务里的进度：等着处理的 (路径, 是否直接点名这个文件)、已经定过的文件、注入过的段落和用掉的字数。"""

    pending: list[tuple[str, bool]] = field(default_factory=list)
    handled: set[str] = field(default_factory=set)
    blocks: list[Block] = field(default_factory=list)
    used_chars: int = 0


# 任务挂起恢复后换了新的 loop state，同一个进程里靠这里接上：注入过的不重读，消息丢了再补回去
_TASK_STATES: OrderedDict[str, TaskState] = OrderedDict()
_TASK_STATES_LIMIT = 256


def _enabled(ls: Any) -> bool:
    if ls is None:
        return False
    rctx = getattr(ls, "rctx", None)
    if bool((getattr(rctx, "task_context", None) or {}).get("is_system_task")):
        return False
    extra = getattr(getattr(ls, "node", None), "extra", None)
    return bool(parse_bool((extra if isinstance(extra, dict) else {}).get(NODE_SWITCH), True))


def task_state(ls: Any, *, create: bool) -> TaskState | None:
    task_id = str(getattr(getattr(ls, "rctx", None), "task_id", "") or "")
    if not task_id:
        state = getattr(ls, "_project_instruction_state", None)
        if state is None and create:
            state = TaskState()
            ls._project_instruction_state = state
        return state
    state = _TASK_STATES.get(task_id)
    if state is None:
        if not create:
            return None
        state = TaskState()
        _TASK_STATES[task_id] = state
        while len(_TASK_STATES) > _TASK_STATES_LIMIT:
            _TASK_STATES.popitem(last=False)
    else:
        _TASK_STATES.move_to_end(task_id)
    return state


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _argument_paths(name: str, arguments: dict[str, Any]) -> list[str]:
    if name == "read_file":
        files = arguments.get("files")
        if isinstance(files, list) and files:
            return [_text(entry.get("path") if isinstance(entry, dict) else entry) for entry in files]
        return [_text(arguments.get("path"))]
    if name == "list_dir":
        paths = arguments.get("paths")
        if isinstance(paths, list) and paths:
            return [_text(item) for item in paths]
        return [_text(arguments.get("path")) or "."]
    if name in _SEARCH_TOOLS:
        base = _text(arguments.get("path")) or "."
        prefix = _pattern_directory(arguments.get("pattern"))
        if not prefix:
            return [base]
        # glob 的 pattern 相对 path；search_in_files 的 pattern 和工作区相对路径比，不能再拼到 path 后面
        return [base, prefix if name == "search_in_files" else posixpath.join(base, prefix)]
    return [_text(arguments.get("path"))]


def _pattern_directory(pattern: Any) -> str:
    """文件 glob 开头不带通配符的那几级目录，例如 engine/**/*.py 是 engine。"""
    if not isinstance(pattern, str):
        return ""
    parts = pattern.replace("\\", "/").split("/")
    literal: list[str] = []
    for part in parts[:-1]:
        if not part or any(ch in part for ch in _GLOB_CHARS):
            break
        literal.append(part)
    return "/".join(literal)


def _succeeded(paths: list[str], result: Any) -> list[str]:
    """调用整体成功就全算；部分失败时按结果里逐项的 success 挑出真读到、列出的那些。"""
    if tool_outcome(result).ok:
        return paths
    data = result.get("data") if isinstance(result, dict) else None
    entries = data.get("results") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    done = {
        str(entry.get("path") or "").strip()
        for entry in entries
        if isinstance(entry, dict) and entry.get("success") is True and not entry.get("omitted")
    }
    return [path for path in paths if path in done]


def touched_paths(name: str, arguments: Any, result: Any) -> list[tuple[str, bool]]:
    """一次成功的工具调用碰到的路径，第二项表示这次调用直接点名了这个文件。"""
    if name not in _FILE_TOOLS and name not in _LISTING_TOOLS and name not in _SEARCH_TOOLS:
        return []
    args = arguments if isinstance(arguments, dict) else {}
    paths = [path for path in _argument_paths(name, args) if path]
    if name in _LISTING_TOOLS:
        paths = _succeeded(paths, result)
    elif not tool_outcome(result).ok:
        return []
    return [(path, name in _FILE_TOOLS) for path in dict.fromkeys(paths)]


def _file_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


@dataclass(frozen=True)
class _Walk:
    """碰到的路径解析到哪，以及从它所在目录往上到所在允许根目录的 (目录, 显示用的目录名)，最深的在前。"""

    resolved: Path
    chain: list[tuple[Path, str]]
    inside_workspace: bool


def _walk_up(workspace_root: Path, extra_roots: list[Path], raw: str) -> _Walk | None:
    """不在允许根目录（工作区或 policy 的 extra_roots）里的返回 None。"""
    resolved, _display, is_external = classify_path(workspace_root, extra_roots, raw)
    if resolved is None or is_external:
        return None
    is_dir = resolved.is_dir()
    logical = config_layout.workspace_logical_path(workspace_root, resolved)
    if logical is not None:
        # 按工作区里的名字往上走：经受信链接通到外面的目录也停在工作区根
        relative = PurePosixPath(logical)
        parts = (relative if is_dir else relative.parent).parts
        chain = []
        for depth in range(len(parts), -1, -1):
            name = PurePosixPath(*parts[:depth]).as_posix() if depth else ""
            chain.append((Path(workspace_root) / name if name else Path(workspace_root), name))
        return _Walk(resolved, chain, True)
    roots = [root for root in extra_roots if resolved == root or root in resolved.parents]
    if not roots:
        return None
    root = min(roots, key=lambda item: len(item.parts))
    directory = resolved if is_dir else resolved.parent
    chain = []
    for current in (directory, *directory.parents):
        chain.append((current, current.as_posix()))
        if current == root:
            break
    return _Walk(resolved, chain, False)


def _candidates_in(walk: _Walk) -> Iterator[Candidate]:
    # 根目录的排在前面：先给通用的约定，再给更具体的目录
    for directory, label in reversed(walk.chain):
        for name in INSTRUCTION_FILES:
            path = directory / name
            try:
                if not path.is_file():
                    continue
                key = _file_key(path)
            except OSError:
                continue
            if walk.inside_workspace:
                display = f"{label}/{name}" if label else name
            else:
                display = path.as_posix()
            yield Candidate(key=key, path=path, display=display, directory=label)


def discover(workspace_root: Path, pending: list[tuple[str, bool]], handled: set[str]) -> tuple[list[str], list[Candidate]]:
    """返回 (直接点名过的说明文件, 还没定过的候选)。模型自己读写过的那份不再另外加载。"""
    workspace_root = Path(workspace_root)
    policy = load_policy_config(workspace_root)
    extra_roots = parse_extra_roots(workspace_root, policy.get("extra_roots"))
    walks: list[_Walk] = []
    direct: list[str] = []
    for raw, named in pending:
        try:
            walk = _walk_up(workspace_root, extra_roots, raw)
            if walk is None:
                continue
            walks.append(walk)
            if named and walk.resolved.name.lower() in _INSTRUCTION_NAMES and walk.resolved.is_file():
                direct.append(_file_key(walk.resolved))
        except OSError as exc:
            logger.debug("project instructions: cannot resolve %s: %s", raw, exc)
    seen = set(handled) | set(direct)
    candidates: list[Candidate] = []
    for walk in walks:
        for candidate in _candidates_in(walk):
            if candidate.key in seen:
                continue
            seen.add(candidate.key)
            candidates.append(candidate)
    return direct, candidates


def read_instruction(path: Path, limit: int) -> tuple[str, bool] | None:
    """读出说明正文，截到 limit 字；返回 (正文, 有没有截断)，读不了或是空的返回 None。"""
    try:
        with open(path, "rb") as stream:
            data = stream.read(_READ_BYTE_LIMIT + 1)
    except OSError as exc:
        logger.debug("project instructions: cannot read %s: %s", path, exc)
        return None
    cut = len(data) > _READ_BYTE_LIMIT
    text = data[:_READ_BYTE_LIMIT].decode("utf-8", errors="replace").lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    if len(text) > limit:
        return text[:limit].rstrip(), True
    return text, cut


def _scope_label(candidate: Candidate) -> str:
    return f"{candidate.directory.rstrip('/')}/ 目录" if candidate.directory else "工作区根目录"


def _loaded_block(candidate: Candidate, text: str, truncated: bool, limit: int) -> Block:
    lines = [f"── {candidate.display}（{_scope_label(candidate)}下项目的约定）──", text]
    if truncated:
        lines.append(f"…（这份说明超过 {limit} 字，后面的没有加载；需要时用 read_file 读 {candidate.display}）")
    return Block(
        key=candidate.key, path=candidate.display, status="truncated" if truncated else "loaded",
        chars=len(text), text="\n".join(lines),
    )


def _hint_block(candidate: Candidate, *, over_budget: bool = False) -> Block:
    if over_budget:
        text = f"发现 {candidate.display}（项目说明已加载到上限，这份没有自动加载），需要时用 read_file 读取。"
    else:
        text = f"发现 {candidate.display}，需要时用 read_file 读取。"
    return Block(key=candidate.key, path=candidate.display, status="over_budget" if over_budget else "hinted", chars=0, text=text)


def render_message(blocks: list[Block]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": "\n\n".join([_HEADER, *(block.text for block in blocks), _FOOTER]),
        "_dynamic": True,
        MESSAGE_KEY: [block.key for block in blocks],
    }


def injected_keys(messages: list[Any]) -> set[str]:
    keys: set[str] = set()
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get(MESSAGE_KEY), list):
            keys.update(str(key) for key in message[MESSAGE_KEY])
    return keys


async def _read_verdict(ls: Any, display: str) -> str | None:
    """read_file 策略对这份说明的结论，只问不送审；问不到返回 None，这份留到下次碰到再问。"""
    try:
        verdict = await build_tool_context(ls).evaluate_op("read_file", {"path": display})
    except Exception as exc:
        logger.warning("project instructions: read policy check for %s failed: %s", display, exc)
        return None
    return str(verdict.get("safety_level") or "") if isinstance(verdict, dict) else ""


async def _load(ls: Any, state: TaskState, pending: list[tuple[str, bool]]) -> list[Block]:
    direct, candidates = await asyncio.to_thread(discover, ls.rctx.workspace_root, pending, set(state.handled))
    state.handled.update(direct)
    blocks: list[Block] = []
    for candidate in candidates:
        if candidate.key in state.handled:
            continue
        verdict = await _read_verdict(ls, candidate.display)
        if verdict is None:
            continue
        state.handled.add(candidate.key)
        if verdict == "approval_required":
            blocks.append(_hint_block(candidate))
            continue
        if verdict != "auto":
            continue
        remaining = TOTAL_CHAR_LIMIT - state.used_chars
        if remaining < _MIN_USEFUL_CHARS:
            blocks.append(_hint_block(candidate, over_budget=True))
            continue
        limit = min(FILE_CHAR_LIMIT, remaining)
        loaded = await asyncio.to_thread(read_instruction, candidate.path, limit)
        if loaded is None:
            continue
        text, truncated = loaded
        state.used_chars += len(text)
        blocks.append(_loaded_block(candidate, text, truncated, limit))
    return blocks


class ProjectInstructions:
    """after_tool_call 记下碰到的路径；before_step 找说明、过策略、注入，之前注入过却不在消息里的补回去。"""

    name = "project_instructions"
    priority = 20

    async def collect(self, ctx: Any) -> None:
        extra = ctx.extra if isinstance(getattr(ctx, "extra", None), dict) else {}
        ls = extra.get("loop_state")
        call = getattr(ctx, "tool_call", None)
        if call is None or not _enabled(ls):
            return None
        paths = touched_paths(str(getattr(call, "name", "") or ""), getattr(call, "arguments", None), extra.get("tool_result"))
        if paths:
            state = task_state(ls, create=True)
            if state is not None:
                state.pending.extend(paths)
        return None

    async def inject(self, ctx: Any) -> Any | None:
        extra = ctx.extra if isinstance(getattr(ctx, "extra", None), dict) else {}
        ls = extra.get("loop_state")
        if not _enabled(ls):
            return None
        state = task_state(ls, create=False)
        if state is None:
            return None
        present = injected_keys(ls.messages)
        # 抢占注入会清掉所有动态消息，挂起恢复后消息也是重新组装的：注入过的不在了就补回去
        missing = [block for block in state.blocks if block.key not in present]
        added: list[Block] = []
        if state.pending:
            pending, state.pending = list(dict.fromkeys(state.pending)), []
            added = await _load(ls, state, pending)
            state.blocks.extend(added)
        blocks = missing + added
        if not blocks:
            return None
        ls.messages.append(render_message(blocks))
        if added:
            await ls.rctx.emit_event(LOADED_EVENT, {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": ctx.step,
                "files": [{"path": block.path, "status": block.status, "chars": block.chars} for block in added],
            })
        return hook_result(modified=True)

    async def forget(self, ctx: Any) -> None:
        task_id = str(getattr(getattr(ctx, "rctx", None), "task_id", "") or "")
        if task_id:
            _TASK_STATES.pop(task_id, None)
