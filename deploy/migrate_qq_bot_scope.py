"""把会话与长期记忆整体搬到另一个 bot QQ 号名下：离线、可 dry-run、自动备份。

会话键摘要按 bot 账号分作用域，换号即换命名空间。存量数据是在没有作用域时攒的，
用 --from "" --to <当前号> 把它们一次性划给当前账号。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_adapter_module(name: str):
    """按文件加载 adapters.onebot 下的纯模块：包 __init__ 会 import nonebot，
    而迁移脚本跑在没有 NoneBot 驱动的环境里。算法必须与运行时同一份。"""
    spec = importlib.util.spec_from_file_location(
        f"_migrate_{name}", _ROOT / "adapters" / "onebot" / f"{name}.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"_migrate_{name}"] = module
    spec.loader.exec_module(module)
    return module


_conversation_hash = _load_adapter_module("conversation_hash")
_bot_scope = _load_adapter_module("bot_scope")
digest = _conversation_hash.digest
LEGACY_MARKER = _conversation_hash.LEGACY_MARKER

from engine.conversation_migration import (  # noqa: E402
    Rename, apply_migration, load_route_map, plan_migration, route_state_file,
)


class ScopeMismatch(RuntimeError):
    """--from 与盘上实际的会话键对不上，继续搬只会把数据搬进错误的命名空间。"""


@dataclass
class ScopeReport:
    workspace: Path
    apply: bool
    source_scope: str
    target_scope: str
    renames: list[Rename] = field(default_factory=list)
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    moved_attachment_dirs: list[tuple[str, str]] = field(default_factory=list)
    skipped_target_exists: list[tuple[str, str]] = field(default_factory=list)
    missing_source_dirs: list[str] = field(default_factory=list)
    unknown_conv_dirs: list[str] = field(default_factory=list)
    hit_keys_rewritten: int = 0
    route_keys_changed: int = 0
    sessions_changed: int = 0
    scope_written: bool = False
    backup_dir: Path | None = None

    @property
    def changed_renames(self) -> list[Rename]:
        return [r for r in self.renames if r.changed]


def read_secret(workspace: Path) -> str:
    """读已钉住的摘要密钥。无盐部署返回空串，与运行时的降级行为一致。"""
    path = Path(workspace) / "data" / "onebot_conversation_hash_secret"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return "" if content == LEGACY_MARKER else content


def verify_source_scope(workspace: Path, secret: str, source_scope: str) -> None:
    """用 --from 重算一遍，对不上就中止。

    搬迁本身只认盘上的 key，不需要 --from；但认错源等于认错了「现在这批数据属于谁」，
    搬完既找不回旧号也进不了新号。
    """
    mismatched = [
        old_stable
        for old_stable, real in load_route_map(route_state_file(workspace)).items()
        if old_stable != f"{old_stable.split(':', 1)[0]}:{digest(real, secret, bot_scope=source_scope)}"
    ]
    if mismatched:
        raise ScopeMismatch(
            f"{len(mismatched)} 个会话键不是按 --from={source_scope or '<无作用域>'} 算出来的，"
            "确认源账号后重跑",
        )


def run_scope_migration(
    *, workspace: Path, target_scope: str, source_scope: str | None = None,
    apply: bool = False, adopt: bool = True,
) -> ScopeReport:
    workspace = Path(workspace)
    secret = read_secret(workspace)
    source = _bot_scope.load_scope(workspace) if source_scope is None else _bot_scope.normalize(source_scope)
    target = _bot_scope.normalize(target_scope)
    if not target:
        raise ValueError("--to 必须是目标 bot 的 QQ 号（纯数字）")

    verify_source_scope(workspace, secret, source)

    report = ScopeReport(
        workspace=workspace, apply=apply, source_scope=source, target_scope=target,
    )
    plan = plan_migration(
        workspace=workspace,
        restable=lambda old_stable, real: (
            f"{old_stable.split(':', 1)[0]}:{digest(real, secret, bot_scope=target)}"
        ),
    )
    report.renames = plan.renames
    report.missing_source_dirs = plan.missing_source_dirs
    report.unknown_conv_dirs = plan.unknown_conv_dirs

    if not apply:
        return report

    # 会话历史与长期记忆共用一个会话键，两边一起搬才不会一半跟着走一半留在旧号。
    outcome = apply_migration(
        workspace=workspace, plan=plan, move_attachments=True, rewrite_sessions=True,
    )
    report.moved_dirs = outcome.moved_dirs
    report.moved_attachment_dirs = outcome.moved_attachment_dirs
    report.skipped_target_exists = outcome.skipped_target_exists
    report.hit_keys_rewritten = outcome.hit_keys_rewritten
    report.route_keys_changed = outcome.route_keys_changed
    report.sessions_changed = outcome.sessions_changed
    report.backup_dir = outcome.backup_dir

    # 作用域写在最后：前面抛异常就不会到这里，bot 仍按旧作用域启动，脚本可重跑。
    if adopt:
        report.scope_written = _bot_scope.save_scope(workspace, target)
    return report


def _print_report(report: ScopeReport) -> None:
    mode = "APPLY" if report.apply else "DRY-RUN"
    src = report.source_scope or "<无作用域>"
    print(f"[{mode}] workspace: {report.workspace}")
    print(f"源账号: {src}  ->  目标账号: {report.target_scope}")
    changed = report.changed_renames
    print(f"待搬迁会话数: {len(changed)}")
    for rename in changed:
        state = "moved" if (rename.old_ns, rename.new_ns) in report.moved_dirs else (
            "target-exists" if rename.target_exists else (
                "no-memory-dir" if not rename.dir_exists else "planned"))
        print(f"  {rename.real}: {rename.old_ns} -> {rename.new_ns} [{state}]")
    if report.unknown_conv_dirs:
        print("路由状态里查不到真实会话、无法重算的记忆目录（保持原样）:")
        for name in report.unknown_conv_dirs:
            print(f"  {name}")
    if report.skipped_target_exists:
        print("目标目录已存在、跳过搬迁:")
        for old_ns, new_ns in report.skipped_target_exists:
            print(f"  {old_ns} -> {new_ns}")
    if report.apply:
        print(f"备份目录: {report.backup_dir}")
        print(f"搬迁记忆目录: {len(report.moved_dirs)}；"
              f"附件目录: {len(report.moved_attachment_dirs)}；"
              f"命中缓存 key: {report.hit_keys_rewritten}；"
              f"路由状态 key: {report.route_keys_changed}；"
              f"会话注册表: {report.sessions_changed}；"
              f"当前账号已写入: {report.scope_written}")
        print("重启 bot 让它读到新的作用域，否则进程内还留着按旧账号算出的键。")
    else:
        print("这是 dry-run，未改动任何文件。加 --apply 真正执行。")
    print("在 bot 与 supervisor 都停止的状态下运行本脚本。")


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default=os.environ.get("CLONOTH_WORKSPACE", str(_ROOT)),
        help="Clonoth 工作区根目录（默认取 CLONOTH_WORKSPACE 或仓库根）。",
    )
    parser.add_argument("--to", required=True, help="目标 bot 的 QQ 号。")
    parser.add_argument(
        "--from", dest="source", default=None,
        help='源 bot 的 QQ 号；存量无作用域数据传空串 ""。默认取已记录的当前账号。',
    )
    parser.add_argument("--apply", action="store_true", help="真正执行；不加则只 dry-run。")
    parser.add_argument(
        "--no-adopt", action="store_true",
        help="搬完不把目标号记成当前账号（迁给一个还没登录的号时用）。",
    )
    args = parser.parse_args(argv)

    try:
        report = run_scope_migration(
            workspace=Path(args.workspace), target_scope=args.to, source_scope=args.source,
            apply=args.apply, adopt=not args.no_adopt,
        )
    except ScopeMismatch as error:
        print(f"源账号校验失败，未改动任何文件: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"迁移中止（如已部分改动请从备份恢复）: {error}", file=sys.stderr)
        return 1
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
