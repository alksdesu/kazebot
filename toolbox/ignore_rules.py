"""遍历目录的工具共用的忽略规则：默认只列名字、不进去的大目录，以及按 ripgrep 默认口径认的 .gitignore。

只认 .gitignore，不认 .ignore、.rgignore、.git/info/exclude 和全局排除文件；search_in_files 调 rg 时传的参数与此对应。"""
from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 版本库、依赖安装、字节码和检查工具的缓存、IDE 元数据、构建产物：都是工具生成的，体积大，没人手改
DEFAULT_IGNORED_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea",
    ".next", "dist", "build", "target", ".gradle",
})

# 用 walk_files 遍历的工具最多走这么多个文件：巨大的目录树里光逐个 stat 就要很久
MAX_WALK_FILES = 200_000

GITIGNORE_NAME = ".gitignore"
_REPO_MARKER = ".git"
_MAX_GITIGNORE_BYTES = 4 * 1024 * 1024
_BOM = "\ufeff"


class GlobError(ValueError):
    """glob 写得不合法：没闭合的 [ 或 {、嵌套的 {、反斜杠悬在结尾、倒过来的范围。"""


def _find_class_end(glob: str, start: int) -> int:
    """start 指着 [，返回配对的 ] 的下标；没闭合抛 GlobError。"""
    i = start + 1
    if i < len(glob) and glob[i] in "!^":
        i += 1
    if i < len(glob) and glob[i] == "]":
        i += 1
    while i < len(glob):
        if glob[i] == "\\":
            i += 2
            continue
        if glob[i] == "]":
            return i
        i += 1
    raise GlobError(f"unclosed character class in {glob!r}")


def _char_class(glob: str, start: int) -> tuple[str, int]:
    end = _find_class_end(glob, start)
    i = start + 1
    negated = i < end and glob[i] in "!^"
    if negated:
        i += 1
    chars: list[str] = []
    while i < end:
        if glob[i] == "\\" and i + 1 < end:
            i += 1
        chars.append(glob[i])
        i += 1
    items: list[str] = []
    k = 0
    while k < len(chars):
        if k + 2 < len(chars) and chars[k + 1] == "-":
            low, high = chars[k], chars[k + 2]
            if low > high:
                raise GlobError(f"invalid range {low}-{high} in {glob!r}")
            items.append(f"{re.escape(low)}-{re.escape(high)}")
            k += 3
            continue
        items.append(re.escape(chars[k]))
        k += 1
    body = "".join(items)
    # 字符类和 * ? 一样不跨目录
    return (f"[^/{body}]" if negated else f"(?!/)[{body}]"), end + 1


def _find_brace_end(glob: str, start: int) -> int:
    i = start + 1
    while i < len(glob):
        ch = glob[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            i = _find_class_end(glob, i) + 1
            continue
        if ch == "{":
            raise GlobError(f"nested alternation in {glob!r}")
        if ch == "}":
            return i
        i += 1
    raise GlobError(f"unclosed alternation in {glob!r}")


def _split_alternatives(body: str) -> list[str]:
    parts: list[str] = []
    current = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            i = _find_class_end(body, i) + 1
            continue
        if ch == ",":
            parts.append(body[current:i])
            current = i + 1
        i += 1
    parts.append(body[current:])
    return parts


def _translate(glob: str, *, in_alternation: bool = False) -> str:
    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        ch = glob[i]
        if ch == "\\":
            if i + 1 >= n:
                raise GlobError(f"dangling backslash in {glob!r}")
            out.append(re.escape(glob[i + 1]))
            i += 2
        elif ch == "*":
            if glob.startswith("**", i):
                after = i + 2
                whole_component = (i == 0 or glob[i - 1] == "/") and (after == n or glob[after] == "/")
                if whole_component and after == n:
                    out.append(".*")
                    i = after
                elif whole_component:
                    out.append("(?:.*/)?")
                    i = after + 1
                else:
                    # 不是整段的 ** 和 * 一样，不跨目录
                    out.append("[^/]*")
                    i = after
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            piece, i = _char_class(glob, i)
            out.append(piece)
        elif ch == "{":
            if in_alternation:
                raise GlobError(f"nested alternation in {glob!r}")
            end = _find_brace_end(glob, i)
            alternatives = [_translate(part, in_alternation=True) for part in _split_alternatives(glob[i + 1:end])]
            out.append("(?:" + "|".join(alternatives) + ")")
            i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def glob_regex(glob: str) -> re.Pattern[str]:
    """把 glob 编成正则，用 fullmatch 和 / 分隔的相对路径比：* ? 和字符类不跨目录，整段的 ** 跨任意层，
    {a,b} 是多选一，反斜杠转义下一个字符。写坏了抛 GlobError。"""
    return re.compile(_translate(glob), re.DOTALL)


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    whitelist: bool
    dir_only: bool


def _rule_for(line: str) -> _Rule | None:
    """一行 .gitignore 变成一条规则，空行和注释是 None。写法和 ripgrep 的 ignore 库逐条对齐。"""
    if line.startswith("#"):
        return None
    if not line.endswith("\\ "):
        line = line.rstrip()
    if not line:
        return None
    whitelist = False
    anchored = False
    if line.startswith(("\\!", "\\#")):
        line = line[1:]
    else:
        if line.startswith("!"):
            whitelist = True
            line = line[1:]
        if line.startswith("/"):
            line = line[1:]
            anchored = True
    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
        if line.endswith("\\"):
            line = line[:-1]
    if not anchored and "/" not in line and not (line.startswith("**/") or line == "**"):
        line = f"**/{line}"
    if line.endswith("/**"):
        line = f"{line}/*"
    return _Rule(glob_regex(line), whitelist, dir_only)


class GitignoreFile:
    """一个目录里 .gitignore 的全部规则；路径按相对这个目录、用 / 分隔来比，区分大小写。"""

    def __init__(self, rules: tuple[_Rule, ...]) -> None:
        self._rules = rules

    @classmethod
    def parse(cls, text: str) -> "GitignoreFile":
        if text.startswith(_BOM):
            text = text[len(_BOM):]
        rules: list[_Rule] = []
        for raw in text.split("\n"):
            line = raw[:-1] if raw.endswith("\r") else raw
            try:
                rule = _rule_for(line)
            except GlobError as exc:
                # ripgrep 也是跳过写坏的这一行，其余照常生效
                logger.debug("skipping .gitignore line %r: %s", line, exc)
                continue
            if rule is not None:
                rules.append(rule)
        return cls(tuple(rules))

    def __bool__(self) -> bool:
        return bool(self._rules)

    def match(self, rel: str, is_dir: bool) -> bool | None:
        """True 忽略，False 被 ! 规则放行，None 没有规则命中。最后一条命中的规则说了算，只认目录的规则不管文件。"""
        for rule in reversed(self._rules):
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.fullmatch(rel):
                return not rule.whitelist
        return None


@dataclass(frozen=True)
class _Level:
    directory: Path
    rules: GitignoreFile | None
    has_git: bool


def _read_gitignore(directory: Path) -> GitignoreFile | None:
    path = directory / GITIGNORE_NAME
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        with open(path, "rb") as stream:
            data = stream.read(_MAX_GITIGNORE_BYTES)
    except OSError:
        return None
    rules = GitignoreFile.parse(data.decode("utf-8", errors="replace"))
    return rules or None


def _has_repo_marker(directory: Path) -> bool:
    try:
        return os.path.exists(directory / _REPO_MARKER)
    except OSError:
        return False


Levels = tuple[_Level, ...]


class GitignoreScope:
    """一次遍历里 .gitignore 怎么判，口径同 ripgrep 默认（require_git）：

    只在 git 工作区里生效，即从这一级往上哪一级目录带 .git；从条目所在目录往上逐级看 .gitignore，最近一处
    有规则命中的说了算；过了带 .git 的那一级就不再往上（嵌套的仓库不吃外层的规则）。遍历根目录以上的
    .gitignore 一样算，遍历根本身不拿来比。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._levels: dict[str, _Level] = {}
        inherited: Levels | None = None
        chain: list[_Level] = []
        for parent in root.parents:
            level = self._level(parent)
            chain.append(level)
            if level.has_git:
                inherited = tuple(chain)
                break
        self.root_levels = self.descend(inherited, root)

    def _level(self, directory: Path) -> _Level:
        key = str(directory)
        level = self._levels.get(key)
        if level is None:
            level = _Level(directory, _read_gitignore(directory), _has_repo_marker(directory))
            self._levels[key] = level
        return level

    def descend(self, inherited: Levels | None, directory: Path) -> Levels | None:
        """进到 directory 里时要看的那几级，最近的在前；None 表示这里不在 git 工作区里，.gitignore 不生效。"""
        level = self._level(directory)
        if level.has_git:
            return (level,)
        if inherited is None:
            return None
        return (level, *inherited)

    @staticmethod
    def ignored(levels: Levels | None, path: Path, is_dir: bool) -> bool:
        if not levels:
            return False
        for level in levels:
            if level.rules is None:
                continue
            try:
                rel = path.relative_to(level.directory).as_posix()
            except ValueError:
                continue
            verdict = level.rules.match(rel, is_dir)
            if verdict is not None:
                return verdict
        return False


def gitignore_applies(root: Path) -> bool:
    """root 下面的条目会不会按 .gitignore 过滤：root 或它上面哪一级带 .git。"""
    return GitignoreScope(root).root_levels is not None


def walk_files(
    root: Path,
    *,
    gitignore: bool = True,
    skip_dir: Callable[[str], bool] = lambda _name: False,
) -> Iterator[tuple[os.DirEntry[str], str]]:
    """按 os.walk 自上而下的顺序给出 root 下的普通文件和它相对 root 的路径（/ 分隔）：每个目录先给它自己的文件，
    再按名字逐个进子目录，同一层都按名字排。

    软链接（文件和目录）都跳过、设备文件之类也跳过，和 ripgrep 不加 -L 时一样；skip_dir 命中的目录名和
    .gitignore 忽略的目录不进，.gitignore 忽略的文件不给。读不了的目录跳过。"""
    scope = GitignoreScope(root) if gitignore else None
    stack: list[tuple[Path, str, Levels | None]] = [(root, "", scope.root_levels if scope else None)]
    while stack:
        directory, prefix, levels = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError:
            continue
        files: list[os.DirEntry[str]] = []
        dirs: list[os.DirEntry[str]] = []
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry)
                elif entry.is_file(follow_symlinks=False):
                    files.append(entry)
            except OSError:
                continue
        files.sort(key=lambda item: item.name)
        dirs.sort(key=lambda item: item.name)
        for entry in files:
            if levels and GitignoreScope.ignored(levels, Path(entry.path), False):
                continue
            yield entry, prefix + entry.name
        children: list[tuple[Path, str, Levels | None]] = []
        for entry in dirs:
            if skip_dir(entry.name):
                continue
            child = Path(entry.path)
            if levels and GitignoreScope.ignored(levels, child, True):
                continue
            child_levels = scope.descend(levels, child) if scope is not None else None
            children.append((child, f"{prefix}{entry.name}/", child_levels))
        stack.extend(reversed(children))
