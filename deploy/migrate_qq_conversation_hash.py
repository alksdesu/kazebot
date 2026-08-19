"""把存量部署的会话键摘要从无盐迁移到加盐：离线、可 dry-run、失败可中止。

只改 bot 拥有的三样东西（记忆目录名、.hit_cache.json 前缀、路由状态键），
按 real_conversation_keys 逐条重算摘要，不动 supervisor 的 sessions.json。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 直接按文件加载 conversation_hash：adapters.onebot 的包 __init__ 会 import nonebot，
# 迁移脚本跑在没有 NoneBot 驱动的环境里。摘要算法必须与运行时同一份。
_spec = importlib.util.spec_from_file_location(
    "_migrate_conversation_hash", _ROOT / "adapters" / "onebot" / "conversation_hash.py",
)
assert _spec and _spec.loader
_conversation_hash = importlib.util.module_from_spec(_spec)
sys.modules["_migrate_conversation_hash"] = _conversation_hash
_spec.loader.exec_module(_conversation_hash)
digest = _conversation_hash.digest
LEGACY_MARKER = _conversation_hash.LEGACY_MARKER
_SECRET_RE = _conversation_hash._SECRET_RE

import engine.memory_hit_cache as hit_cache  # noqa: E402
from engine.eventlog_rotation import eventlog_file_lock  # noqa: E402

_SECRET_BYTES = 32
_HIT_KEY_SEP = "/"


def _namespace_dir(stable_key: str) -> str:
    return "conv_" + sha256(stable_key.encode("utf-8")).hexdigest()[:24]


@dataclass
class Rename:
    real: str
    old_stable: str
    new_stable: str
    old_ns: str
    new_ns: str
    dir_exists: bool
    target_exists: bool


@dataclass
class MigrationReport:
    workspace: Path
    apply: bool
    target_secret: str = ""
    already_salted: bool = False
    renames: list[Rename] = field(default_factory=list)
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    skipped_target_exists: list[tuple[str, str]] = field(default_factory=list)
    missing_source_dirs: list[str] = field(default_factory=list)
    unknown_conv_dirs: list[str] = field(default_factory=list)
    route_keys_changed: int = 0
    hit_keys_rewritten: int = 0
    secret_written: bool = False
    backup_dir: Path | None = None

    @property
    def changed_renames(self) -> list[Rename]:
        return [r for r in self.renames if r.old_stable != r.new_stable]


def _load_route_map(route_state_file: Path) -> dict[str, str]:
    if not route_state_file.exists():
        return {}
    try:
        data = json.loads(route_state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    real_map = data.get("real_conversation_keys")
    if not isinstance(real_map, dict):
        return {}
    return {str(k): str(v) for k, v in real_map.items() if str(k) and str(v)}


def _resolve_target_secret(secret_file: Path, override: str | None) -> tuple[str, bool]:
    """返回 (target_secret, already_salted)。override > 文件里的已有密钥 > 新生成。"""
    already = False
    if secret_file.exists():
        try:
            content = secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        if content and content != LEGACY_MARKER and _SECRET_RE.fullmatch(content):
            already = True
            if override is None:
                # 复用已钉住的密钥，让重复运行成为幂等的空操作。
                return content, already
    if override is not None:
        return override, already
    return secrets.token_hex(_SECRET_BYTES), already


def _rewrite_hit_cache(workspace: Path, ns_renames: dict[str, str]) -> int:
    """把 .hit_cache.json 里被改名 namespace 的前缀换成新目录名，复用侧车的锁与原子写。"""
    if not ns_renames:
        return 0
    path = hit_cache.hit_cache_path(workspace)
    if not path.exists():
        return 0
    with eventlog_file_lock(path):
        cache = hit_cache.read_hit_cache(workspace)
        rewritten: dict[str, str] = {}
        changed = 0
        for key, stamp in cache.items():
            ns_part, sep, rest = key.partition(_HIT_KEY_SEP)
            if sep and ns_part in ns_renames:
                rewritten[f"{ns_renames[ns_part]}{_HIT_KEY_SEP}{rest}"] = stamp
                changed += 1
            else:
                rewritten[key] = stamp
        if not changed:
            return 0
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rewritten), encoding="utf-8")
        os.replace(tmp, path)
        return changed


def _rewrite_route_state(route_state_file: Path, renames: list[Rename]) -> int:
    remap = {r.old_stable: r.new_stable for r in renames if r.old_stable != r.new_stable}
    if not remap:
        return 0
    data = json.loads(route_state_file.read_text(encoding="utf-8"))
    real_map = data.get("real_conversation_keys")
    if not isinstance(real_map, dict):
        return 0
    rebuilt = {remap.get(str(k), str(k)): str(v) for k, v in real_map.items()}
    data["real_conversation_keys"] = rebuilt
    tmp = route_state_file.with_suffix(route_state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, route_state_file)
    return len(remap)


def _write_secret(secret_file: Path, secret: str) -> None:
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = secret_file.with_name(f"{secret_file.name}.{os.getpid()}.tmp")
    tmp.write_text(secret, encoding="utf-8")
    os.replace(tmp, secret_file)
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass


def _fresh_backup_dir(workspace: Path) -> Path:
    base = workspace / "data" / f"migration_backup_{time.strftime('%Y%m%d_%H%M%S')}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{suffix}")
        suffix += 1
    return candidate


def _backup(backup_dir: Path, memory_root: Path, route_state_file: Path, renames: list[Rename]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=False)
    if route_state_file.exists():
        shutil.copy2(route_state_file, backup_dir / route_state_file.name)
    hit_file = hit_cache.hit_cache_path(memory_root.parent.parent)
    if hit_file.exists():
        shutil.copy2(hit_file, backup_dir / hit_file.name)
    for rename in renames:
        if rename.old_stable == rename.new_stable or not rename.dir_exists:
            continue
        src = memory_root / rename.old_ns
        if src.is_dir():
            shutil.copytree(src, backup_dir / rename.old_ns)


def run_migration(
    *, workspace: Path, apply: bool, secret: str | None = None,
) -> MigrationReport:
    workspace = Path(workspace)
    route_state_file = workspace / "data" / "onebot_plugin_state.json"
    memory_root = workspace / "data" / "memory"
    secret_file = workspace / "data" / "onebot_conversation_hash_secret"

    report = MigrationReport(workspace=workspace, apply=apply)
    report.target_secret, report.already_salted = _resolve_target_secret(secret_file, secret)

    real_map = _load_route_map(route_state_file)
    known_old_ns: set[str] = set()
    known_new_ns: set[str] = set()
    for old_stable, real in real_map.items():
        prefix = old_stable.split(":", 1)[0]
        new_stable = f"{prefix}:{digest(real, report.target_secret)}"
        old_ns = _namespace_dir(old_stable)
        new_ns = _namespace_dir(new_stable)
        known_old_ns.add(old_ns)
        known_new_ns.add(new_ns)
        dir_exists = (memory_root / old_ns).is_dir()
        target_exists = old_ns != new_ns and (memory_root / new_ns).exists()
        report.renames.append(
            Rename(real, old_stable, new_stable, old_ns, new_ns, dir_exists, target_exists),
        )
        if old_stable != new_stable and not dir_exists:
            report.missing_source_dirs.append(old_ns)

    if memory_root.is_dir():
        for child in sorted(memory_root.glob("conv_*")):
            if child.is_dir() and child.name not in known_old_ns and child.name not in known_new_ns:
                report.unknown_conv_dirs.append(child.name)

    if not apply:
        return report

    if report.changed_renames:
        report.backup_dir = _fresh_backup_dir(workspace)
        _backup(report.backup_dir, memory_root, route_state_file, report.renames)

        ns_renames: dict[str, str] = {}
        for rename in report.renames:
            if rename.old_stable == rename.new_stable:
                continue
            if not rename.dir_exists:
                continue
            if rename.target_exists:
                report.skipped_target_exists.append((rename.old_ns, rename.new_ns))
                continue
            shutil.move(str(memory_root / rename.old_ns), str(memory_root / rename.new_ns))
            report.moved_dirs.append((rename.old_ns, rename.new_ns))
            ns_renames[rename.old_ns] = rename.new_ns

        report.hit_keys_rewritten = _rewrite_hit_cache(workspace, ns_renames)
        report.route_keys_changed = _rewrite_route_state(route_state_file, report.renames)

    # 密钥写在最后：前面任何一步抛异常都不会到这里，摘要模式保持原样，脚本可重跑。
    current = ""
    if secret_file.exists():
        try:
            current = secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
    if current != report.target_secret:
        _write_secret(secret_file, report.target_secret)
        report.secret_written = True
    return report


def _print_report(report: MigrationReport) -> None:
    mode = "APPLY" if report.apply else "DRY-RUN"
    print(f"[{mode}] workspace: {report.workspace}")
    if report.already_salted and not report.changed_renames:
        print("会话键摘要已是加盐状态，无需迁移。")
    changed = report.changed_renames
    print(f"待重算会话数: {len(changed)}")
    for rename in changed:
        state = "moved" if (rename.old_ns, rename.new_ns) in report.moved_dirs else (
            "target-exists" if rename.target_exists else (
                "missing-dir" if not rename.dir_exists else "planned"))
        print(f"  {rename.real}: {rename.old_ns} -> {rename.new_ns} [{state}]")
    if report.unknown_conv_dirs:
        print("路由状态里查不到、保持原样的记忆目录（无法重算，请勿手工改名）:")
        for name in report.unknown_conv_dirs:
            print(f"  {name}")
    if report.skipped_target_exists:
        print("目标目录已存在、跳过改名:")
        for old_ns, new_ns in report.skipped_target_exists:
            print(f"  {old_ns} -> {new_ns}")
    if report.apply:
        print(f"备份目录: {report.backup_dir}")
        print(f"改名记忆目录: {len(report.moved_dirs)}；"
              f"重写命中缓存 key: {report.hit_keys_rewritten}；"
              f"更新路由状态 key: {report.route_keys_changed}；"
              f"密钥文件已写入: {report.secret_written}")
    else:
        print("这是 dry-run，未改动任何文件。加 --apply 真正执行。")
    print("supervisor 会话未改：这些群下次说话会各自新建 session，只丢一次短期上下文，"
          "长期记忆已随目录迁移。")
    print("在 bot 与 supervisor 都停止的状态下运行本脚本。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default=os.environ.get("CLONOTH_WORKSPACE", str(_ROOT)),
        help="Clonoth 工作区根目录（默认取 CLONOTH_WORKSPACE 或仓库根）。",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正执行迁移；不加则只 dry-run 打印计划。",
    )
    parser.add_argument(
        "--secret", default=None,
        help="迁移到的目标密钥（≥64 位 hex）；不给则复用已有密钥或随机生成。",
    )
    args = parser.parse_args(argv)

    secret = args.secret
    if secret is not None:
        secret = secret.strip()
        if not _SECRET_RE.fullmatch(secret):
            parser.error("--secret 必须是 ≥64 位十六进制字符串")

    try:
        report = run_migration(workspace=Path(args.workspace), apply=args.apply, secret=secret)
    except Exception as error:
        print(f"迁移中止（未写入密钥，可修复后重跑；如已部分改动请从备份恢复）: {error}", file=sys.stderr)
        return 1
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
