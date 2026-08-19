from __future__ import annotations

"""Helpers for user-editable drawtools config files.

Only *.example.* files should be versioned as defaults. Runtime files
(settings.yaml, character_tags.yaml, prompts/*.md) are user-owned and can be
created from examples when missing.
"""

from pathlib import Path

DRAWTOOLS_DIR = Path(__file__).resolve().parent
PROMPT_DIR = DRAWTOOLS_DIR / "prompts" / "novelai"


def ensure_user_configs() -> list[str]:
    pairs = [
        (DRAWTOOLS_DIR / "settings.example.yaml", DRAWTOOLS_DIR / "settings.yaml"),
        (DRAWTOOLS_DIR / "character_tags.example.yaml", DRAWTOOLS_DIR / "character_tags.yaml"),
        (PROMPT_DIR / "top-system.example.md", PROMPT_DIR / "top-system.md"),
        (PROMPT_DIR / "output-format.example.md", PROMPT_DIR / "output-format.md"),
        (PROMPT_DIR / "tag-guide.example.md", PROMPT_DIR / "tag-guide.md"),
    ]
    created: list[str] = []
    for src, dst in pairs:
        if dst.exists() or not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(str(dst.relative_to(DRAWTOOLS_DIR.parents[1]).as_posix()))
    return created
