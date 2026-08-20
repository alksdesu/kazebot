"""把工具写的记忆从 manual 回填成 auto：离线、可 dry-run、自动备份。

source 曾按节点名前缀判定，qq.orchestrator 写的全被标成 manual，于是 bot 自己记的
东西自己删不掉、dream 也清不掉。判定改对之后，存量得跟着回填才解得开锁。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.builtin.knowledge_inject import _memory_write_lock  # noqa: E402

_MANUAL = "manual"
_AUTO = "auto"


def _load_books(memory_root: Path) -> list[tuple[Path, dict]]:
    books: list[tuple[Path, dict]] = []
    for path in sorted(memory_root.rglob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as error:
            print(f"跳过无法解析的记忆本 {path}: {error}", file=sys.stderr)
            continue
        if isinstance(data.get("entries"), list):
            books.append((path, data))
    return books


def run_migration(*, workspace: Path, apply: bool) -> int:
    memory_root = workspace / "data" / "memory"
    if not memory_root.exists():
        print(f"没有记忆目录：{memory_root}")
        return 0

    # 与 save_memory / delete_memory 同一把文件锁，不必停 bot。
    with _memory_write_lock(workspace):
        targets = []
        for path, data in _load_books(memory_root):
            hits = [
                entry for entry in data["entries"]
                if isinstance(entry, dict) and str(entry.get("source") or "").strip() == _MANUAL
            ]
            if hits:
                targets.append((path, data, hits))

        total = sum(len(hits) for _, _, hits in targets)
        print(f"命中 {total} 条 manual 记忆，分布在 {len(targets)} 个记忆本：")
        for path, _, hits in targets:
            for entry in hits:
                mark = "（constant，仍受常驻保护）" if entry.get("constant") else ""
                print(f"  {path.relative_to(memory_root)}  {entry.get('id')}{mark}")

        if not total:
            return 0
        if not apply:
            print("\n以上为 dry-run，加 --apply 才真正写入。")
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = memory_root.parent / f"memory.bak-{stamp}"
        shutil.copytree(memory_root, backup)
        print(f"\n已备份到 {backup}")

        for path, data, hits in targets:
            for entry in hits:
                entry["source"] = _AUTO
            text = yaml.safe_dump(
                data, sort_keys=False, allow_unicode=True, default_flow_style=False,
            )
            tmp = path.with_name(f"{path.name}.migrate.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        print(f"已回填 {total} 条。重启 engine 让记忆缓存失效。")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default=os.environ.get("CLONOTH_WORKSPACE", str(_ROOT)),
        help="Clonoth 工作区根目录（默认取 CLONOTH_WORKSPACE 或仓库根）。",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正执行回填；不加则只 dry-run 打印命中项。",
    )
    args = parser.parse_args(argv)

    try:
        run_migration(workspace=Path(args.workspace).resolve(), apply=args.apply)
    except Exception as error:
        print(f"迁移中止（可从 data/memory.bak-* 恢复后重跑）: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
