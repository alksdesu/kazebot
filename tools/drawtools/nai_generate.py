from __future__ import annotations

"""NovelAI image generation tool for Clonoth drawtools.

This tool is intentionally placed under tools/drawtools/; toolbox.registry scans
subdirectories recursively and registers SPEC.name as the public tool name.

Input supports either:
- prompt / negative_prompt / width / height (low-level direct generation)
- character_prompts for NovelAI V4+ per-character captions

Configuration is loaded from tools/drawtools/settings.yaml.
"""

SPEC = {
    "name": "nai_generate",
    "description": "使用 NovelAI 生成 anime 图片。读取 tools/drawtools/settings.yaml 中的 API 和默认参数，支持 V4/V4.5 角色级 prompt，返回图片附件路径。",
    "async_mode": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "正面提示词 / base scene prompt"},
            "negative_prompt": {"type": "string", "description": "负面提示词 / base negative prompt"},
            "character_prompts": {
                "type": "array",
                "description": "NovelAI V4+ 角色提示词数组，每项含 prompt/uc/center{x,y}",
                "items": {"type": "object"},
            },
            "size_label": {"type": "string", "description": "横图 / 竖图 / 方图", "enum": ["横图", "竖图", "方图"]},
            "width": {"type": "integer", "description": "图片宽度；不填时由 size_label 决定"},
            "height": {"type": "integer", "description": "图片高度；不填时由 size_label 决定"},
            "preset": {"type": "string", "description": "预设 id/name/alias；覆盖 settings.yaml 的 selected_preset_id"},
            "preset_id": {"type": "string", "description": "同 preset"},
            "model": {"type": "string", "description": "NovelAI 模型，默认使用配置预设"},
            "sampler": {"type": "string"},
            "scheduler": {"type": "string"},
            "steps": {"type": "integer"},
            "scale": {"type": "number", "description": "CFG"},
            "qualityToggle": {"type": "boolean", "description": "质量增强"},
            "ucPreset": {"type": "integer", "description": "负面预设：0=Heavy,1=Light,2=Human Focus,3=None"},
            "autoSmea": {"type": "boolean", "description": "自动 SMEA"},
            "cfg_rescale": {"type": "number", "description": "CFG 重缩放 0~1"},
            "variety_boost": {"type": "boolean", "description": "多样性增强 (V4.5)"},
            "seed": {"type": "integer"},
            "filename": {"type": "string", "description": "输出文件名（不含路径），默认自动生成"},
            "params": {"type": "object", "description": "覆盖配置预设中的底层 NovelAI 参数"},
        },
        "required": ["prompt"],
    },
    # filename 直接拼进落盘路径，逃逸出图目录就能覆写任意文件，交给 write_file 规则判。
    "guard": {"op": "write_file", "params": {"path": "filename"}},
}

TIMEOUT_SEC = 180.0


if __name__ == "__main__":
    import base64
    import hashlib
    import json
    import contextlib
    import math
    import os
    import random
    import shutil
    import subprocess
    import sys
    import time
    import uuid
    import zipfile
    from pathlib import Path

    from cleanup import maybe_cleanup_novelai_attachments
    from common import (
        PresetNotFoundError,
        load_settings,
        require_preset,
        resolve_api_key,
        resolve_base_url,
        selected_preset,
        size_to_dimensions,
    )

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    args = json.loads(sys.stdin.read() or "{}")
    settings = load_settings()
    maybe_cleanup_novelai_attachments(settings)
    preset_ref = str(args.get("preset") or args.get("preset_id") or "").strip()
    # 预设名写错就别出图：按默认预设画出来的风格不是用户要的，模型能读到错误自己改。
    try:
        preset = require_preset(settings, preset_ref) if preset_ref else selected_preset(settings)
    except PresetNotFoundError as exc:
        fail(f"{exc}；可用预设：" + "、".join(exc.available))
    preset_params = dict(preset.get("params") or {})
    params = dict(preset_params)
    if isinstance(args.get("params"), dict):
        params.update(args["params"])

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        fail("prompt is required")

    negative_prompt = str(args.get("negative_prompt") or preset.get("negative_prefix") or "").strip()
    size_label = str(args.get("size_label") or "").strip()
    width, height, resolved_label = size_to_dimensions(size_label or "竖图", settings)
    width = int(args.get("width") or params.get("width") or width)
    height = int(args.get("height") or params.get("height") or height)

    model = str(args.get("model") or params.get("model") or "nai-diffusion-4-5-full").strip()
    params["model"] = model
    sampler = str(args.get("sampler") or params.get("sampler") or "k_euler_ancestral")
    scheduler = str(args.get("scheduler") or params.get("scheduler") or params.get("noise_schedule") or "karras")
    steps = int(args.get("steps") or params.get("steps") or 28)
    scale = float(args.get("scale") if args.get("scale") is not None else params.get("scale", 6))
    for bool_key in ("qualityToggle", "autoSmea", "variety_boost", "sm", "sm_dyn", "decrisper"):
        if bool_key in args:
            params[bool_key] = bool(args.get(bool_key))
    for num_key in ("ucPreset", "cfg_rescale"):
        if num_key in args:
            params[num_key] = args.get(num_key)
    seed = int(args.get("seed") if args.get("seed") is not None else params.get("seed", -1))
    if seed < 0:
        seed = random.randint(0, 2**32 - 1)

    character_prompts = args.get("character_prompts") if isinstance(args.get("character_prompts"), list) else []
    clean_char_prompts = []
    for cp in character_prompts:
        if not isinstance(cp, dict):
            continue
        center = cp.get("center") if isinstance(cp.get("center"), dict) else {"x": 0.5, "y": 0.5}
        clean_char_prompts.append({
            "prompt": str(cp.get("prompt") or "").strip(),
            "uc": str(cp.get("uc") or "").strip(),
            "center": {
                "x": float(center.get("x") or 0.5),
                "y": float(center.get("y") or 0.5),
            },
            "enabled": True,
        })

    api_key = resolve_api_key(settings)
    if not api_key:
        fail("NovelAI API key is missing. Set NOVELAI_API_KEY or tools/drawtools/settings.yaml api.api_key")
    base_url = resolve_base_url(settings)
    api_url = f"{base_url}/ai/generate-image"

    is_v3 = "nai-diffusion-3" in model or "furry-3" in model
    is_v45 = "nai-diffusion-4-5" in model

    if is_v3:
        all_char_prompt = ", ".join(cp["prompt"] for cp in clean_char_prompts if cp.get("prompt"))
        full_prompt = f"{prompt}, {all_char_prompt}" if all_char_prompt else prompt
        all_negative = ", ".join(x for x in [negative_prompt, *(cp.get("uc", "") for cp in clean_char_prompts)] if x)
        payload = {
            "action": "generate",
            "input": full_prompt,
            "model": model,
            "parameters": {
                "width": width,
                "height": height,
                "scale": scale,
                "seed": seed,
                "sampler": sampler,
                "noise_schedule": scheduler,
                "steps": steps,
                "n_samples": 1,
                "negative_prompt": all_negative,
                "ucPreset": int(params.get("ucPreset", 0)),
                "qualityToggle": bool(params.get("qualityToggle", True)),
                "sm": bool(params.get("sm", False)),
                "sm_dyn": bool(params.get("sm_dyn", False)),
                "dynamic_thresholding": bool(params.get("decrisper", False)),
            },
        }
    else:
        use_coords = any(cp.get("center") and (cp["center"].get("x") != 0.5 or cp["center"].get("y") != 0.5) for cp in clean_char_prompts)
        skip_cfg_above_sigma = None
        if is_v45 and params.get("variety_boost"):
            skip_cfg_above_sigma = math.pow((width * height) / 1011712, 0.5) * 58
        char_captions = [{"char_caption": cp.get("prompt", ""), "centers": [cp.get("center") or {"x": 0.5, "y": 0.5}]} for cp in clean_char_prompts]
        neg_char_captions = [{"char_caption": cp.get("uc", ""), "centers": [cp.get("center") or {"x": 0.5, "y": 0.5}]} for cp in clean_char_prompts]
        payload = {
            "action": "generate",
            "input": prompt,
            "model": model,
            "parameters": {
                "params_version": 3,
                "width": width,
                "height": height,
                "scale": scale,
                "seed": seed,
                "sampler": sampler,
                "noise_schedule": scheduler,
                "steps": steps,
                "n_samples": 1,
                "ucPreset": int(params.get("ucPreset", 0)),
                "qualityToggle": bool(params.get("qualityToggle", True)),
                "autoSmea": bool(params.get("autoSmea", False)),
                "cfg_rescale": float(params.get("cfg_rescale", 0) or 0),
                "dynamic_thresholding": False,
                "controlnet_strength": 1,
                "legacy": False,
                "legacy_v3_extend": False,
                "use_coords": use_coords,
                "legacy_uc": False,
                "normalize_reference_strength_multiple": True,
                "deliberate_euler_ancestral_bug": False,
                "prefer_brownian": True,
                "image_format": "png",
                "skip_cfg_above_sigma": skip_cfg_above_sigma,
                "characterPrompts": clean_char_prompts,
                "v4_prompt": {
                    "caption": {"base_caption": prompt, "char_captions": char_captions},
                    "use_coords": use_coords,
                    "use_order": True,
                },
                "v4_negative_prompt": {
                    "caption": {"base_caption": negative_prompt, "char_captions": neg_char_captions},
                    "legacy_uc": False,
                },
                "negative_prompt": negative_prompt,
            },
        }


    workspace_root = Path.cwd()
    out_dir = workspace_root / "data" / "attachments" / "novelai"
    tmp_dir = workspace_root / "data" / "temp" / "novelai"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    filename = str(args.get("filename") or "").strip()
    if not filename:
        digest = hashlib.md5(f"{prompt}:{seed}".encode("utf-8")).hexdigest()[:10]
        filename = f"nai_{digest}_{uuid.uuid4().hex[:6]}.png"
    if not filename.lower().endswith(".png"):
        filename += ".png"

    tmp_zip = tmp_dir / f"nai_{seed}_{uuid.uuid4().hex}.zip"
    tmp_payload = tmp_dir / f"nai_{seed}_{uuid.uuid4().hex}.json"
    extract_dir = tmp_dir / f"out_{seed}_{uuid.uuid4().hex}"
    lock_file = tmp_dir / "novelai_generate.lock"
    extract_dir.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def acquire_queue_lock(lock_path: Path, timeout_sec: float):
        start = time.time()
        fd = None
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                break
            except FileExistsError:
                if time.time() - start >= timeout_sec:
                    raise TimeoutError(f"NovelAI queue lock timeout after {timeout_sec}s")
                # 若锁文件异常残留超过 1 小时，自动清理。
                try:
                    if time.time() - lock_path.stat().st_mtime > max(timeout_sec, 3600):
                        lock_path.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass
                time.sleep(0.2)
        try:
            yield
        finally:
            try:
                if fd is not None:
                    os.close(fd)
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass

    def read_error_body() -> str:
        try:
            return tmp_zip.read_text(encoding="utf-8", errors="replace")[:2000]
        except Exception:
            return ""

    def is_concurrent_lock(code: str, body: str) -> bool:
        # NovelAI 并发锁：HTTP 429 且响应体含 "Concurrent generation is locked"。
        # 这类错误必须退避更久（服务端配额释放需要时间），不能用普通短重试间隔。
        if code != "429":
            return False
        return "concurrent generation is locked" in (body or "").lower()

    def should_retry(code: str, stderr: str) -> bool:
        if code in {"429", "500", "502", "503", "504"}:
            return True
        if not code or code == "000":
            return True
        text = (stderr or "").lower()
        return any(key in text for key in ["timeout", "timed out", "connection", "reset"])

    try:
        tmp_payload.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        timeout_sec = int((settings.get("api") or {}).get("timeout_sec") or 130)
        gen_cfg = settings.get("generation") if isinstance(settings.get("generation"), dict) else {}
        retry_wait_sec = float(gen_cfg.get("retry_wait_sec", 3) or 0)
        retry_max_attempts = int(gen_cfg.get("retry_max_attempts", 5) or 0)
        request_delay_sec = float(gen_cfg.get("request_delay_sec", 0) or 0)
        lock_timeout_sec = float(gen_cfg.get("lock_timeout_sec", 3600) or 3600)
        # NovelAI 服务端并发锁（HTTP 429 Concurrent generation is locked）的专用退避参数。
        # 原因：本地队列锁只能保证同一进程串行，但无法避免 NovelAI 账号层面上一张图
        # 服务端尚未释放并发名额的情况。做法：遇到并发锁时，在用户设定的秒数区间
        # [lock_retry_wait_min_sec, lock_retry_wait_max_sec] 内随机取一个等待时间再重试，
        # 与普通 retry_wait_sec 互斥、且并发锁判定优先。
        lock_retry_max_attempts = int(gen_cfg.get("lock_retry_max_attempts", 8) or 0)
        lock_retry_wait_min_sec = float(gen_cfg.get("lock_retry_wait_min_sec", 6) or 0)
        lock_retry_wait_max_sec = float(gen_cfg.get("lock_retry_wait_max_sec", 8) or 0)
        # 兜底：若用户把区间写反或缺省，纠正为合法区间。
        if lock_retry_wait_max_sec < lock_retry_wait_min_sec:
            lock_retry_wait_min_sec, lock_retry_wait_max_sec = lock_retry_wait_max_sec, lock_retry_wait_min_sec

        with acquire_queue_lock(lock_file, lock_timeout_sec):
            last_error = ""
            # 并发锁重试与普通重试共用一个循环，但并发锁（429 concurrent lock）
            # 不占用普通重试名额，而是单独计数、用更长的退避等待。
            attempt = 0
            lock_attempt = 0
            max_total_loops = retry_max_attempts + lock_retry_max_attempts + 2
            for _ in range(max_total_loops):
                try:
                    tmp_zip.unlink(missing_ok=True)
                except Exception:
                    pass
                result = subprocess.run(
                    [
                        "curl", "-s", "-m", str(timeout_sec),
                        api_url,
                        "-H", f"Authorization: Bearer {api_key}",
                        "-H", "Content-Type: application/json",
                        "-d", f"@{tmp_payload}",
                        "-o", str(tmp_zip),
                        "-w", "%{http_code}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec + 10,
                )
                http_code = (result.stdout or "").strip()
                # 兼容中转/反代返回 201 Created 的情况：
                # 只要是 2xx 就视为成功，响应体（zip/PNG）后续统一解析。
                if http_code.startswith("2") and len(http_code) == 3:
                    break
                body = read_error_body()
                last_error = f"NovelAI API returned HTTP {http_code}: {body or result.stderr}"
                # 并发锁：单独退避，在用户设定的区间内随机等待后重试。
                if is_concurrent_lock(http_code, body):
                    if lock_attempt >= lock_retry_max_attempts:
                        fail(last_error)
                    wait = random.uniform(lock_retry_wait_min_sec, lock_retry_wait_max_sec)
                    lock_attempt += 1
                    time.sleep(wait)
                    continue
                if attempt >= retry_max_attempts or not should_retry(http_code, result.stderr):
                    fail(last_error)
                attempt += 1
                time.sleep(retry_wait_sec)
            else:
                fail(last_error or "NovelAI API request failed")

            if request_delay_sec > 0:
                time.sleep(request_delay_sec)

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            zf.extractall(extract_dir)
        images = [p for p in extract_dir.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
        if not images:
            fail("No image found in NovelAI response zip")
        dst = out_dir / filename
        shutil.move(str(images[0]), str(dst))
        rel_path = dst.relative_to(workspace_root).as_posix()
        attachment = {"type": "image", "path": rel_path, "mime_type": "image/png", "name": filename}
        cleanup_result = maybe_cleanup_novelai_attachments(settings)
        output({
            "ok": True,
            "data": {
                "result": f"✅ NovelAI image generated: {rel_path}",
                "success": True,
                "path": rel_path,
                "attachments": [attachment],
                "seed": seed,
                "width": width,
                "height": height,
                "size_label": resolved_label,
                "steps": steps,
                "sampler": sampler,
                "scale": scale,
                "model": model,
                "preset_id": str(preset.get("id") or ""),
                "preset_name": str(preset.get("name") or ""),
                "prompt": prompt,
                "negative_prompt": negative_prompt,
            },
            "attachments": [attachment],
        })
    finally:
        try:
            tmp_zip.unlink(missing_ok=True)
            tmp_payload.unlink(missing_ok=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
        except Exception:
            pass
