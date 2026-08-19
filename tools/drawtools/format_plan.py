from __future__ import annotations

import re
from typing import Any

import yaml

from common import comma_join, load_settings, require_preset, selected_preset, size_to_dimensions

GRID_X = {"A": 0.1, "B": 0.3, "C": 0.5, "D": 0.7, "E": 0.9}
GRID_Y = {"1": 0.1, "2": 0.3, "3": 0.5, "4": 0.7, "5": 0.9}


def strip_code_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:yaml|yml)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def parse_plan(plan_text: str) -> dict[str, Any]:
    value = strip_code_fence(plan_text)
    data = yaml.safe_load(value)
    if not isinstance(data, dict):
        raise ValueError("plan must be a YAML object")
    images = data.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("plan.images must be a non-empty list")
    return data


def center_from_grid(value: Any) -> dict[str, float]:
    text = str(value or "C3").strip().upper()
    if len(text) >= 2:
        x = GRID_X.get(text[0], 0.5)
        y = GRID_Y.get(text[1], 0.5)
        return {"x": x, "y": y}
    return {"x": 0.5, "y": 0.5}


# 中日文/全角字符检测：character_tags.yaml 里角色名多为中文，
# 一旦被当成 danbooru tag 塞进 prompt，NovelAI 会画错。这里用于兜底剔除。
_CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")


def _strip_cjk_tags(value: Any) -> str:
    """逐个 tag 过滤掉含 CJK 字符（中文名/日文名）的项，避免把角色中文名当 tag。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("，", ",")
    kept = [seg.strip() for seg in text.split(",") if seg.strip() and not _CJK_RE.search(seg)]
    return ", ".join(kept)


def character_prompt(ch: dict[str, Any]) -> dict[str, Any]:
    # 字段兼容：既支持计划里 AI 常用的 appear/costume，也支持角色库原始的
    # appearance/outfits 字段名，避免 AI 直接透传角色库对象时字段对不上导致丢失。
    appear = ch.get("appear")
    if not appear:
        appear = ch.get("appearance")

    costume = ch.get("costume")
    if not costume:
        # outfits 是列表 [{name, tags}]，取其 tags 合并；也兼容直接给 outfit 字符串。
        outfits = ch.get("outfits")
        if isinstance(outfits, list):
            costume = comma_join(*[
                o.get("tags") if isinstance(o, dict) else o for o in outfits
            ])
        elif ch.get("outfit"):
            costume = ch.get("outfit")

    # danbooru：过滤掉中文名，避免 AI 误把中文名写进 danbooru 字段。
    danbooru = _strip_cjk_tags(ch.get("danbooru"))

    parts = [
        danbooru,
        ch.get("type"),
        appear,
        costume,
        ch.get("action"),
        ch.get("interact"),
    ]
    # 逐段过滤 CJK，最终保险：整条 prompt 不允许残留中文名当 tag。
    prompt = _strip_cjk_tags(comma_join(*parts))
    return {
        "prompt": prompt,
        "uc": comma_join(ch.get("uc")),
        "center": center_from_grid(ch.get("center")),
    }


def _coerce_seed(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_image_task(image: dict[str, Any], index: int, settings: dict[str, Any], base_seed: int | None = None) -> dict[str, Any]:
    preset_ref = str(image.get("preset") or image.get("preset_id") or image.get("style_preset") or "").strip()
    # 预设名写错就报错，别拿默认预设的风格顶包出图。
    preset = require_preset(settings, preset_ref) if preset_ref else selected_preset(settings)
    width, height, size_label = size_to_dimensions(str(image.get("size_label") or "竖图"), settings)
    params = dict(preset.get("params") or {})
    params["width"] = width
    params["height"] = height
    # seed 有两种来源，用 seed_from_image 区分，供下游决定是否递增：
    #   1) image 级显式 seed：用户想固定这张图，用原值、不加偏移（可让多张 tag 不同但同 seed）。
    #   2) plan 级基准 seed：整批共享一个基准，由下游按图序递增，保证整批可复现且每张不同。
    # 缺省时保持预设的 -1（随机）。
    seed_from_image = False
    image_seed = _coerce_seed(image.get("seed"))
    if image_seed is None:
        image_seed = _coerce_seed(image.get("seed_value"))
    if image_seed is not None:
        params["seed"] = image_seed
        seed_from_image = True
    elif base_seed is not None:
        params["seed"] = base_seed

    characters_raw = image.get("characters")
    characters = [c for c in characters_raw if isinstance(c, dict)] if isinstance(characters_raw, list) else []
    char_prompts = [character_prompt(c) for c in characters]

    scene = comma_join(preset.get("positive_prefix"), image.get("scene"))
    negative = comma_join(preset.get("negative_prefix"), image.get("negative_prompt"))
    char_negative = comma_join(*(cp.get("uc") for cp in char_prompts))
    if char_negative:
        negative = comma_join(negative, char_negative)

    return {
        "index": int(image.get("index") or index),
        "anchor": str(image.get("anchor") or "").strip(),
        "size_label": size_label,
        "width": width,
        "height": height,
        "seed": params.get("seed"),
        "seed_from_image": seed_from_image,
        "prompt": scene,
        "negative_prompt": negative,
        "character_prompts": char_prompts,
        "params": params,
        "preset_id": str(preset.get("id") or ""),
        "preset_name": str(preset.get("name") or ""),
        "raw": image,
    }


def build_tasks_from_plan(plan_text: str) -> list[dict[str, Any]]:
    settings = load_settings()
    plan = parse_plan(plan_text)
    images = plan["images"]
    # plan 顶层基准 seed：兼容 seed / base_seed / seed_value 三种写法。
    base_seed = _coerce_seed(plan.get("seed"))
    if base_seed is None:
        base_seed = _coerce_seed(plan.get("base_seed"))
    if base_seed is None:
        base_seed = _coerce_seed(plan.get("seed_value"))
    preset = selected_preset(settings)
    max_images = int(preset.get("max_images") or 0)
    if max_images > 0:
        images = images[:max_images]
    return [normalize_image_task(img, i + 1, settings, base_seed) for i, img in enumerate(images) if isinstance(img, dict)]
