"""把存量部署的会话键摘要从无盐迁移到加盐：离线、可 dry-run、失败可中止。

只改 bot 拥有的东西（记忆目录名、.hit_cache.json 前缀、路由状态键），
按 real_conversation_keys 逐条重算摘要，不动 supervisor 的 sessions.json。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import secrets
import sys
from dataclasses import dataclass, field
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

from engine.conversation_migration import (  # noqa: E402
    Rename, apply_migration, plan_migration,
)

_SECRET_BYTES = 32


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
        return [r for r in self.renames if r.changed]


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


def _write_secret(secret_file: Path, secret: str) -> None:
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = secret_file.with_name(f"{secret_file.name}.{os.getpid()}.tmp")
    tmp.write_text(secret, encoding="utf-8")
    os.replace(tmp, secret_file)
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass


def run_migration(
    *, workspace: Path, apply: bool, secret: str | None = None,
) -> MigrationReport:
    workspace = Path(workspace)
    secret_file = workspace / "data" / "onebot_conversation_hash_secret"

    report = MigrationReport(workspace=workspace, apply=apply)
    report.target_secret, report.already_salted = _resolve_target_secret(secret_file, secret)

    plan = plan_migration(
        workspace=workspace,
        restable=lambda old_stable, real: (
            f"{old_stable.split(':', 1)[0]}:{digest(real, report.target_secret)}"
        ),
    )
    report.renames = plan.renames
    report.missing_source_dirs = plan.missing_source_dirs
    report.unknown_conv_dirs = plan.unknown_conv_dirs

    if not apply:
        return report

    outcome = apply_migration(workspace=workspace, plan=plan)
    report.moved_dirs = outcome.moved_dirs
    report.skipped_target_exists = outcome.skipped_target_exists
    report.hit_keys_rewritten = outcome.hit_keys_rewritten
    report.route_keys_changed = outcome.route_keys_changed
    report.backup_dir = outcome.backup_dir

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
