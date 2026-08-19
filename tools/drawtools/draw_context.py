from __future__ import annotations

"""Expose minimal draw context to the planner node.

The planner can see preset names/ids/aliases but not preset parameter contents.
Character details are returned only when user text matches character keywords,
mirroring LittleWhiteBox's on-demand character library injection.
"""

SPEC = {
    "name": "draw_context",
    "description": "获取绘图分析所需的最小上下文：可用预设名称列表，以及按用户请求关键词命中的角色标签信息。不会暴露预设参数内容。",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "用户原始绘图请求，用于角色关键词匹配"},
            "include_characters": {"type": "boolean", "description": "是否返回关键词命中的角色详情，默认 true"},
            "max_characters": {"type": "integer", "description": "最多返回命中角色数，默认 8"},
        },
        "required": ["query"],
    },
}

TIMEOUT_SEC = 10.0


if __name__ == "__main__":
    import json
    import re
    import sys
    from typing import Any

    from common import is_selected_preset, iter_presets, load_character_tags, load_settings

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    def norm(text: Any) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip().lower())

    def character_keywords(ch: dict[str, Any]) -> list[str]:
        keys = []
        for key in ("name", "danbooru"):
            value = str(ch.get(key) or "").strip()
            if value:
                keys.append(value)
                keys.append(value.replace("_", " "))
        aliases = ch.get("aliases") if isinstance(ch.get("aliases"), list) else []
        keys.extend(str(a).strip() for a in aliases if str(a).strip())
        return list(dict.fromkeys(keys))

    def matches_character(query_norm: str, ch: dict[str, Any]) -> bool:
        for key in character_keywords(ch):
            k = norm(key)
            if k and k in query_norm:
                return True
        return False

    args = json.loads(sys.stdin.read() or "{}")
    query = str(args.get("query") or "")
    include_characters = bool(args.get("include_characters", True))
    max_characters = int(args.get("max_characters") or 8)
    max_characters = max(1, min(50, max_characters))

    try:
        settings = load_settings()
        presets = []
        for p in iter_presets(settings):
            presets.append({
                "id": str(p.get("id") or ""),
                "name": str(p.get("name") or ""),
                "aliases": p.get("aliases") if isinstance(p.get("aliases"), list) else [],
                "selected": is_selected_preset(settings, p),
            })

        matched = []
        if include_characters:
            qn = norm(query)
            for ch in load_character_tags():
                if not matches_character(qn, ch):
                    continue
                matched.append({
                    "name": str(ch.get("name") or ""),
                    "aliases": ch.get("aliases") if isinstance(ch.get("aliases"), list) else [],
                    "danbooru": str(ch.get("danbooru") or ""),
                    "type": str(ch.get("type") or ""),
                    "appearance": str(ch.get("appearance") or ""),
                    "outfits": ch.get("outfits") if isinstance(ch.get("outfits"), list) else [],
                })
                if len(matched) >= max_characters:
                    break

        preset_lines = [f"- {p['name']} (id: {p['id']})" + (" [当前默认]" if p.get("selected") else "") for p in presets]
        char_lines = [f"- {c['name']} / {c['danbooru']}" for c in matched]
        output({
            "ok": True,
            "data": {
                "result": "可用预设：\n" + ("\n".join(preset_lines) or "（无）") + "\n命中角色：\n" + ("\n".join(char_lines) or "（无）"),
                "presets": presets,
                "matched_characters": matched,
                "character_match_count": len(matched),
            },
        })
    except Exception as exc:
        fail(exc)
