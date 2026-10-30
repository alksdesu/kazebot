"""隔离副本用到的 git 命令和目录操作：建、删 worktree，算相对基准提交的补丁，在原仓库上检查和应用补丁。

删目录不顺着软链和目录联接走：副本里链过去的依赖目录指向的是主工作区。
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Iterable

import config_layout
from real_paths import real_path
from toolbox._common import safe_subprocess_env

log = logging.getLogger(__name__)

GIT_TIMEOUT_SEC = 300.0
BRANCH_PREFIX = "clonoth/"
_LOCK_RETRIES = 4
_LOCK_RETRY_SEC = 0.5
# 报错按英文解析，冲突的文件和行号才认得出来
_GIT_LOCALE = {"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"}
_GIT_ENV_DROP = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
    "GIT_PREFIX", "GIT_NAMESPACE", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES",
})
_LONG_PATH_MARKERS = ("filename too long", "file name too long", "name too long")
_LOCK_MARKERS = (".lock': file exists", "another git process seems to be running", "unable to create '")
_HEX_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_KEY_NAME = re.compile(r"[^0-9A-Za-z._-]+")
_KEY_NAME_CHARS = 32
_KEY_HASH_CHARS = 8
_GITLINK_MODE = "160000"
_COPY_CHUNK = 1024 * 1024


class GitError(RuntimeError):
    """git 命令失败。message 可以直接给人看；long_path 为真表示是 Windows 上路径太长。"""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "", long_path: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.stderr = stderr
        self.long_path = long_path


@dataclass(frozen=True)
class GitOutput:
    returncode: int
    stdout: bytes
    stderr: str

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


def _base_command() -> list[str]:
    # .git/config 里配成程序路径的这些项，隔离副本的 git 操作一跑就执行它，所以每次都用命令行盖掉。
    # clean/smudge 过滤器要配合 .gitattributes，命令行关不掉在仓库文件里的属性，靠 .git/** 的写策略挡住过滤器定义。
    return [
        "git",
        # 不跑仓库里的钩子：worktree add 会触发 post-checkout，删分支会触发 reference-transaction
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "core.longpaths=true",
        # fsmonitor 配成一个程序，status/add/diff 都会去跑它，且不需要任何属性文件配合
        "-c", "core.fsmonitor=false",
    ]


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = safe_subprocess_env()
    for key in list(env):
        if key.upper() in _GIT_ENV_DROP:
            env.pop(key, None)
    env.update(_GIT_LOCALE)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if extra:
        env.update(extra)
    return env


def is_long_path_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _LONG_PATH_MARKERS)


def long_path_message(path: Path | str) -> str:
    return (
        f"路径太长，git 在 {Path(path).as_posix()} 下建不出文件（Windows 默认单条路径最多 260 个字符）："
        "把 engine.worktree.root 配到更短的目录，或者在系统里开启长路径支持（LongPathsEnabled）后重试"
    )


def _is_lock_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _LOCK_MARKERS)


def _describe(args: list[str], stderr: str, returncode: int) -> str:
    detail = " ".join(line.strip() for line in stderr.strip().splitlines()[-6:] if line.strip())
    return f"git {args[0] if args else ''} 失败（退出码 {returncode}）：{detail or '没有输出'}"


def run_git(
    args: list[str],
    *,
    cwd: Path | str,
    env: dict[str, str] | None = None,
    timeout: float = GIT_TIMEOUT_SEC,
    stdout_file: IO[bytes] | None = None,
    check: bool = True,
    input_data: bytes | None = None,
) -> GitOutput:
    """跑一条 git 命令。别的 git 进程正拿着锁时稍等重试几次；check 为真时失败抛 GitError。input_data 是喂给标准输入的内容。"""
    command = [*_base_command(), *args]
    full_env = git_env(env)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    stdin_args: dict[str, object] = {"input": input_data} if input_data is not None else {"stdin": subprocess.DEVNULL}
    for attempt in range(_LOCK_RETRIES):
        if stdout_file is not None:
            stdout_file.seek(0)
            stdout_file.truncate()
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                env=full_env,
                stdout=stdout_file if stdout_file is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                creationflags=creationflags,
                **stdin_args,
            )
        except FileNotFoundError as exc:
            raise GitError(f"找不到 git 命令（{exc}）") from None
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0] if args else ''} 超过 {int(timeout)} 秒没有结束") from None
        except OSError as exc:
            raise GitError(f"git 起不来：{exc}") from None
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        output = GitOutput(proc.returncode, proc.stdout if isinstance(proc.stdout, bytes) else b"", stderr)
        if proc.returncode == 0 or not check:
            return output
        if _is_lock_error(stderr) and attempt + 1 < _LOCK_RETRIES:
            time.sleep(_LOCK_RETRY_SEC * (attempt + 1))
            continue
        raise GitError(
            _describe(args, stderr, proc.returncode),
            returncode=proc.returncode, stderr=stderr, long_path=is_long_path_error(stderr),
        )
    raise GitError(f"git {args[0] if args else ''} 一直等不到仓库的锁")


def _resolved(path: Path | str) -> Path:
    candidate = Path(path)
    try:
        return real_path(candidate)
    except (OSError, RuntimeError):
        return candidate.absolute()


def toplevel(path: Path | str) -> Path | None:
    """path 所在 git 仓库的根，解析到底；不在仓库里、git 用不了都给 None。"""
    directory = Path(path)
    if not directory.is_dir():
        directory = directory.parent
    if not directory.is_dir():
        return None
    try:
        out = run_git(["rev-parse", "--show-toplevel"], cwd=directory, check=False, timeout=60.0)
    except GitError:
        return None
    text = out.text.strip()
    if out.returncode != 0 or not text:
        return None
    return _resolved(text)


def common_dir(repo: Path | str) -> Path | None:
    """仓库的公共 .git 目录（各个 worktree 共用的那一个）。"""
    try:
        out = run_git(["rev-parse", "--git-common-dir"], cwd=repo, check=False, timeout=60.0)
    except GitError:
        return None
    text = out.text.strip()
    if out.returncode != 0 or not text:
        return None
    candidate = Path(text)
    return _resolved(candidate if candidate.is_absolute() else Path(repo) / candidate)


def head_commit(repo: Path | str) -> str:
    out = run_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=repo, check=False, timeout=60.0)
    sha = out.text.strip().lower()
    if out.returncode != 0 or not _HEX_COMMIT.match(sha):
        raise GitError(f"仓库 {Path(repo).as_posix()} 还没有提交（HEAD 不指向任何提交），没法建隔离副本")
    return sha


def is_commit(text: str) -> bool:
    return bool(_HEX_COMMIT.match(str(text or "").strip().lower()))


def repo_key(top: Path | str) -> str:
    """仓库在副本根下的目录名：可读的仓库名加上绝对路径的短哈希，同名的两个仓库不会撞在一起。"""
    resolved = _resolved(top)
    name = _KEY_NAME.sub("-", resolved.name or "repo").strip("-.") or "repo"
    digest = hashlib.sha1(os.path.normcase(resolved.as_posix()).encode("utf-8")).hexdigest()[:_KEY_HASH_CHARS]
    return f"{name[:_KEY_NAME_CHARS]}-{digest}"


def branch_name(worktree_id: str) -> str:
    return f"{BRANCH_PREFIX}{worktree_id}"


def branch_exists(repo: Path | str, branch: str) -> bool:
    out = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False, timeout=60.0)
    return out.returncode == 0


def add_worktree(repo: Path | str, path: Path, branch: str, base: str) -> None:
    """基于 base 新开分支 branch，检出到 path。分支已存在（上次建到一半）时先删掉再建一次。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add", "-b", branch, str(path), base]
    try:
        run_git(args, cwd=repo)
        return
    except GitError as exc:
        if exc.long_path:
            raise GitError(long_path_message(path), returncode=exc.returncode, stderr=exc.stderr, long_path=True) from None
        if "already exists" not in exc.stderr or not branch_exists(repo, branch) or path.exists():
            raise
    log.warning("branch %s left over from an earlier attempt; recreating it", branch)
    run_git(["branch", "-D", branch], cwd=repo)
    try:
        run_git(args, cwd=repo)
    except GitError as exc:
        if exc.long_path:
            raise GitError(long_path_message(path), returncode=exc.returncode, stderr=exc.stderr, long_path=True) from None
        raise


def remove_worktree(repo: Path | str, path: Path) -> bool:
    """git worktree remove --force；git 删不掉（没登记、文件被占用）时返回 False，由调用方自己删目录。"""
    out = run_git(["worktree", "remove", "--force", "--force", str(path)], cwd=repo, check=False)
    if out.returncode != 0:
        log.warning("git worktree remove %s failed: %s", path, out.stderr.strip())
        return False
    return True


def prune_worktrees(repo: Path | str) -> None:
    run_git(["worktree", "prune"], cwd=repo, check=False)


def delete_branch(repo: Path | str, branch: str) -> bool:
    if not branch.startswith(BRANCH_PREFIX) or not branch_exists(repo, branch):
        return False
    out = run_git(["branch", "-D", branch], cwd=repo, check=False)
    if out.returncode != 0:
        log.warning("deleting branch %s failed: %s", branch, out.stderr.strip())
        return False
    return True


def worktree_belongs_to(path: Path, repo: Path | str) -> bool:
    """path 是不是 repo 的 worktree：里面的 .git 是个文件，gitdir 指到 repo 公共目录下的 worktrees/。"""
    marker = path / ".git"
    try:
        if not marker.is_file() or config_layout.is_dir_link(marker):
            return False
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    if not text.startswith("gitdir:"):
        return False
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = path / gitdir
    common = common_dir(repo)
    if common is None:
        return False
    target = _resolved(gitdir)
    worktrees = common / "worktrees"
    return target.parent == worktrees


@dataclass(frozen=True)
class DiffFile:
    """一个改动的文件。path 是仓库根下的 / 分隔路径；added、deleted 是行数，二进制文件为 None。"""

    path: str
    status: str
    added: int | None = None
    deleted: int | None = None
    binary: bool = False
    gitlink: bool = False

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {"path": self.path, "status": self.status}
        if self.binary:
            data["binary"] = True
        else:
            data["added"] = self.added or 0
            data["deleted"] = self.deleted or 0
        if self.gitlink:
            data["gitlink"] = True
        return data


@dataclass(frozen=True)
class DiffSummary:
    files: tuple[DiffFile, ...]
    patch_bytes: int
    digest: str

    @property
    def added(self) -> int:
        return sum(item.added or 0 for item in self.files)

    @property
    def deleted(self) -> int:
        return sum(item.deleted or 0 for item in self.files)

    @property
    def gitlinks(self) -> list[str]:
        return [item.path for item in self.files if item.gitlink]


def _pathspec(worktree: Path, excluded: Iterable[str], env: dict[str, str]) -> list[str]:
    """add 和 diff 的路径范围：整个副本，去掉链进来的目录。"""
    names: list[str] = []
    for name in excluded:
        clean = PurePosixPath(str(name).replace("\\", "/")).as_posix().strip("/")
        if clean and clean != "." and clean not in names:
            names.append(clean)
    spec = ["--", "."]
    if not names:
        return spec
    # 被忽略的路径写进 pathspec（排除也算）会让 git add 报错退出；它们本来就不会被加进去，不用再排除
    for name in names:
        out = run_git(["check-ignore", "-q", "--", name], cwd=worktree, env=env, check=False, timeout=60.0)
        if out.returncode != 0:
            spec.append(f":(top,exclude){name}")
    return spec


def _parse_raw(data: bytes) -> list[tuple[str, str, str, str]]:
    """git diff --raw -z 的输出：每项 (旧模式, 新模式, 状态, 路径)。"""
    items: list[tuple[str, str, str, str]] = []
    parts = data.split(b"\0")
    index = 0
    while index < len(parts):
        head = parts[index].decode("utf-8", errors="replace")
        index += 1
        if not head.startswith(":"):
            continue
        fields = head[1:].split()
        if len(fields) < 5 or index >= len(parts):
            continue
        path = parts[index].decode("utf-8", errors="replace")
        index += 1
        items.append((fields[0], fields[1], fields[4][:1], path))
    return items


def _parse_numstat(data: bytes) -> dict[str, tuple[int | None, int | None]]:
    stats: dict[str, tuple[int | None, int | None]] = {}
    for record in data.split(b"\0"):
        if not record:
            continue
        fields = record.decode("utf-8", errors="replace").split("\t", 2)
        if len(fields) != 3:
            continue
        added, deleted, path = fields
        stats[path] = (
            int(added) if added.isdigit() else None,
            int(deleted) if deleted.isdigit() else None,
        )
    return stats


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(_COPY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _replace_patch(staging: Path, patch_path: Path) -> None:
    # Windows 上补丁文件正被人读着（模型在看完整补丁）时换不掉，稍等再试
    for attempt in range(_LOCK_RETRIES):
        try:
            os.replace(staging, patch_path)
            return
        except PermissionError:
            if attempt + 1 >= _LOCK_RETRIES:
                raise
            time.sleep(_LOCK_RETRY_SEC * (attempt + 1))


def compute_diff(
    worktree: Path, base: str, patch_path: Path, *, excluded: Iterable[str] = (),
) -> DiffSummary:
    """worktree 的工作区相对 base 的全部改动（含未提交、未跟踪的文件，被 .gitignore 忽略的除外），完整补丁写到 patch_path。

    用一份临时索引来算，副本自己的索引和暂存区不动。excluded 是链到主工作区的目录，不算改动。
    """
    if not is_commit(base):
        raise GitError(f"基准提交写得不对：{base!r}")
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="clonoth-wt-"))
    try:
        index = scratch / "index"
        env = {"GIT_INDEX_FILE": str(index)}
        # 不拿副本自己的索引起头：和检出同一刻写下、大小没变的文件，按它记的时间戳会被当成没改
        run_git(["read-tree", base], cwd=worktree, env=env)
        spec = _pathspec(worktree, excluded, env)
        run_git(["add", "-A", *spec], cwd=worktree, env=env)
        common = ["diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color"]
        raw = run_git([*common, "--raw", "-z", "--abbrev=40", base, *spec], cwd=worktree, env=env).stdout
        numstat = run_git([*common, "--numstat", "-z", base, *spec], cwd=worktree, env=env).stdout
        fd, staging_name = tempfile.mkstemp(prefix=f".{patch_path.name}.", suffix=".tmp", dir=patch_path.parent)
        staging = Path(staging_name)
        try:
            with os.fdopen(fd, "w+b") as stream:
                run_git(
                    [*common, "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/", base, *spec],
                    cwd=worktree, env=env, stdout_file=stream,
                )
            _replace_patch(staging, patch_path)
        finally:
            with contextlib.suppress(OSError):
                staging.unlink()
    except GitError as exc:
        if exc.long_path:
            raise GitError(long_path_message(worktree), returncode=exc.returncode, stderr=exc.stderr, long_path=True) from None
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    stats = _parse_numstat(numstat)
    files = []
    for old_mode, new_mode, status, path in _parse_raw(raw):
        added, deleted = stats.get(path, (None, None))
        files.append(DiffFile(
            path=path,
            status=status or "M",
            added=added,
            deleted=deleted,
            binary=path in stats and added is None and deleted is None,
            gitlink=_GITLINK_MODE in (old_mode, new_mode),
        ))
    digest, size = _sha256_file(patch_path)
    return DiffSummary(tuple(files), size, digest)


def read_head(path: Path, limit: int) -> tuple[str, bool]:
    """补丁文件开头最多 limit 个字符，以及后面还有没有。"""
    if limit <= 0:
        return "", True
    with open(path, "rb") as stream:
        data = stream.read(limit * 4 + 4)
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit and len(data) < limit * 4 + 4:
        return text, False
    cut = text.rfind("\n", 0, limit)
    head = text[: cut + 1] if cut >= limit // 2 else text[:limit]
    return head, True


@dataclass(frozen=True)
class ApplyConflict:
    """合回时对不上的地方：path 是仓库根下的路径（认不出时为空），line 是补丁那一段在原文件里的起始行。"""

    path: str
    line: int | None
    reason: str

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {"path": self.path, "reason": self.reason}
        if self.line is not None:
            data["line"] = self.line
        return data


_APPLY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^error: patch failed: (?P<path>.+):(?P<line>\d+)$"), "这一段和原仓库现在的内容对不上"),
    (re.compile(r"^error: (?P<path>.+): patch does not apply$"), "补丁套不上"),
    (re.compile(r"^error: (?P<path>.+): already exists in working directory$"), "原仓库里已经有这个文件了"),
    (re.compile(r"^error: (?P<path>.+): No such file or directory$"), "原仓库里没有这个文件了"),
    (re.compile(r"^error: (?P<path>.+): does not exist in index$"), "原仓库里没有这个文件了"),
    (re.compile(r"^error: (?P<path>.+): does not match index$"), "原仓库里的这个文件和索引对不上"),
    (re.compile(r"^error: (?P<path>.+): wrong type$"), "文件类型变了"),
    (re.compile(r"^error: (?P<path>.+) has type \d+, expected \d+$"), "文件类型变了"),
)


def parse_apply_errors(stderr: str) -> list[ApplyConflict]:
    """把 git apply 的报错整理成按文件的冲突；同一个文件的「这一段对不上」和「套不上」并成带行号的一条。"""
    conflicts: list[ApplyConflict] = []
    failed_paths: set[str] = set()
    others: list[str] = []
    for raw_line in str(stderr or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("error:"):
            continue
        for pattern, reason in _APPLY_PATTERNS:
            match = pattern.match(line)
            if match is None:
                continue
            path = match.group("path").strip()
            number = match.groupdict().get("line")
            if number is not None:
                conflicts.append(ApplyConflict(path, int(number), reason))
                failed_paths.add(path)
            elif reason == "补丁套不上":
                if path not in failed_paths:
                    conflicts.append(ApplyConflict(path, None, reason))
                    failed_paths.add(path)
            else:
                conflicts.append(ApplyConflict(path, None, reason))
            break
        else:
            others.append(line[len("error:"):].strip())
    if not conflicts and others:
        conflicts.append(ApplyConflict("", None, "；".join(others)))
    return conflicts


def check_apply(repo: Path | str, patch_path: Path) -> list[ApplyConflict]:
    """在原仓库上试套补丁，什么也不改；套得上给空表。"""
    out = run_git(["apply", "--check", "--binary", "--whitespace=nowarn", str(patch_path)], cwd=repo, check=False)
    if out.returncode == 0:
        return []
    if is_long_path_error(out.stderr):
        raise GitError(long_path_message(repo), returncode=out.returncode, stderr=out.stderr, long_path=True)
    conflicts = parse_apply_errors(out.stderr)
    return conflicts or [ApplyConflict("", None, out.stderr.strip() or "git apply --check 失败，没有说明原因")]


def apply_patch(repo: Path | str, patch_path: Path) -> None:
    """把补丁套到原仓库的工作区上（不动索引）。"""
    try:
        run_git(["apply", "--binary", "--whitespace=nowarn", str(patch_path)], cwd=repo)
    except GitError as exc:
        if exc.long_path:
            raise GitError(long_path_message(repo), returncode=exc.returncode, stderr=exc.stderr, long_path=True) from None
        raise


def patch_paths(repo: Path | str, patch_path: Path) -> list[str]:
    """补丁改了哪些文件（仓库根下 / 分隔的路径）；只读补丁，什么也不改。"""
    if not patch_path.is_file():
        raise GitError(f"补丁 {patch_path.as_posix()} 已经不在了")
    out = run_git(["apply", "--numstat", "-z", str(patch_path)], cwd=repo, timeout=60.0)
    return list(_parse_numstat(out.stdout))


# 带着合回内容的基准提交是 supervisor 自己做的：不借仓库配置的用户身份，也不按 commit.gpgSign 签名
_CARRY_IDENTITY = {
    "GIT_AUTHOR_NAME": "clonoth",
    "GIT_AUTHOR_EMAIL": "clonoth@localhost",
    "GIT_COMMITTER_NAME": "clonoth",
    "GIT_COMMITTER_EMAIL": "clonoth@localhost",
}


def _carry_paths(paths: Iterable[str]) -> list[str]:
    names: list[str] = []
    for raw in paths:
        clean = PurePosixPath(str(raw or "").replace("\\", "/")).as_posix().strip("/")
        if not clean or clean == "." or clean.startswith("../") or clean == ".." or ":" in clean.split("/", 1)[0]:
            continue
        if clean not in names:
            names.append(clean)
    return names


def carried_base(repo: Path | str, head: str, paths: Iterable[str], *, label: str = "") -> str:
    """head 加上 paths 这些文件在 repo 工作区里现在的内容，做成一个提交给新副本当基准；内容和 head 一样时就给 head。

    只用一份临时索引：原仓库的索引、工作区和分支都不动，工作区里已经没有的文件按删掉算。
    这个提交不挂在任何分支上，副本建在它上面以后由副本的分支引用着。
    """
    if not is_commit(head):
        raise GitError(f"基准提交写得不对：{head!r}")
    names = _carry_paths(paths)
    if not names:
        return head
    scratch = Path(tempfile.mkdtemp(prefix="clonoth-carry-"))
    try:
        env = {"GIT_INDEX_FILE": str(scratch / "index")}
        run_git(["read-tree", head], cwd=repo, env=env)
        run_git(
            ["update-index", "--add", "--remove", "-z", "--stdin"], cwd=repo, env=env,
            input_data=b"".join(name.encode("utf-8") + b"\0" for name in names),
        )
        tree = run_git(["write-tree"], cwd=repo, env=env).text.strip()
        head_tree = run_git(["rev-parse", "--verify", f"{head}^{{tree}}"], cwd=repo, timeout=60.0).text.strip()
        if tree == head_tree:
            return head
        message = f"clonoth: changes merged from worktree {label}" if label else "clonoth: merged changes"
        commit = run_git(
            ["commit-tree", tree, "-p", head, "--no-gpg-sign", "-m", message],
            cwd=repo, env={**env, **_CARRY_IDENTITY},
        ).text.strip().lower()
    except GitError as exc:
        if exc.long_path:
            raise GitError(long_path_message(repo), returncode=exc.returncode, stderr=exc.stderr, long_path=True) from None
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if not is_commit(commit):
        raise GitError(f"git commit-tree 没有给出提交号：{commit[:80]!r}")
    return commit


def _long(path: Path | str) -> str:
    """Windows 上加 \\\\?\\ 前缀，删深层文件时不受 260 个字符的限制。"""
    text = os.path.abspath(str(path))
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def is_link(path: Path | str) -> bool:
    return config_layout.is_dir_link(_long(path))


def make_dir_link(link: Path, target: Path) -> None:
    """建一个指向 target 的目录链接：Windows 用目录联接（不要管理员权限），别处用软链。"""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    os.symlink(target, link, target_is_directory=True)


def remove_link(link: Path | str) -> None:
    """只拆链接本身，指向的目录原样不动。"""
    path = _long(link)
    if os.name == "nt":
        try:
            os.rmdir(path)
            return
        except OSError:
            pass
    os.unlink(path)


def links_under(root: Path) -> list[Path]:
    """root 下所有的软链和目录联接，不顺着它们往里走。"""
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(_long(current)))
        except OSError:
            continue
        for entry in entries:
            path = current / entry.name
            if is_link(path):
                found.append(path)
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
            except OSError:
                continue
    return found


def _unlink_file(path: Path) -> None:
    target = _long(path)
    try:
        os.unlink(target)
    except PermissionError:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        os.unlink(target)


def remove_tree(root: Path) -> None:
    """删掉 root 整棵目录。碰到链接只拆链接，绝不进到链接指向的目录里去删。"""
    if not os.path.lexists(_long(root)):
        return
    if is_link(root):
        remove_link(root)
        return
    order: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        order.append(current)
        with os.scandir(_long(current)) as entries:
            for entry in entries:
                path = current / entry.name
                if is_link(path):
                    remove_link(path)
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                else:
                    _unlink_file(path)
    for directory in reversed(order):
        try:
            os.rmdir(_long(directory))
        except PermissionError:
            os.chmod(_long(directory), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            os.rmdir(_long(directory))
