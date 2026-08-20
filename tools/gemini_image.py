from __future__ import annotations

"""Gemini native image generation tool (Nano Banana).

Uses the Gemini generativeLanguage REST API with responseModalities=["TEXT","IMAGE"]
to generate images. The generated image is saved under data/attachments/ and returned
as an attachment path compatible with Clonoth's multimodal pipeline.

发图机制（参考 NovelAI 插件）：
  - async_mode=False：同步执行，出图后直接返回 attachments，引擎据此发图，
    finish_guard 也能正确记录“工具成功”。
  - 图片发送统一交给 supervisor 的 dispatch_attachment 路由（子节点任务完成时
    自动发送），工具本身不再 POST intermediate_reply，避免双发同一张图。

API 渠道配置见 _channel.resolve_image_channel：system_models.image_gemini >
CLONOTH_IMAGE_GEMINI_* > GEMINI_* > OPENAI_* > 主渠道（仅当主渠道本身是 Gemini）。
model 不跟主渠道走，默认 gemini-3-pro-image-preview。
"""

SPEC = {
    "name": "gemini_image",
    "async_mode": False,
    "description": (
        "Generate an image using Gemini (Nano Banana). "
        "Provide a text prompt describing the desired image. "
        "Optionally specify aspect_ratio (1:1, 3:4, 4:3, 9:16, 16:9) and "
        "model (gemini-3-pro-image-preview, gemini-2.5-flash-image, gemini-3.1-flash-image-preview). "
        "The generated image is sent to the user automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text description of the image to generate.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Aspect ratio: 1:1, 3:4, 4:3, 9:16, 16:9. Default: 1:1",
                "enum": ["1:1", "3:4", "4:3", "9:16", "16:9"],
            },
            "model": {
                "type": "string",
                "description": "Gemini image model to use. Default: gemini-3-pro-image-preview",
            },
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of paths to input images (relative to workspace root) to use as references.",
            },
        },
        "required": ["prompt"],
    },
}

# [2026-07-19] 超时/重试对齐：单次请求 120s，重试 2 次，退避 5s；
# 内部最坏耗时 ≈ 2×120+5 = 245s < 工具总超时 300s，两个超时对齐。
TIMEOUT_SEC = 300

# API 请求重试参数（参考 NovelAI：429/5xx/超时/网络错误自动重试）
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SEC = (5.0,)
_REQUEST_TIMEOUT = 120

if __name__ == "__main__":
    import json
    import sys
    import time
    import base64
    import uuid
    from pathlib import Path
    from urllib import request as urllib_request
    from urllib.error import HTTPError, URLError

    _input = json.loads(sys.stdin.read())

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        # [AutoC 2026-05-31] Why: image generation errors must be visible through
        # data.result as well as error. How: emit the unified failure wrapper before
        # exiting non-zero. Purpose: let the registry preserve detailed API errors.
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    # [2026-07-19] 方案 X：工具不再自己推图，发图统一交给 supervisor 的
    # dispatch_attachment 路由（和 NovelAI 一致），避免与它双发同一张图。

    args = _input

    # ---- 参数 ----
    prompt_text = str(args.get("prompt") or "").strip()
    if not prompt_text:
        fail("prompt is required")

    aspect_ratio = str(args.get("aspect_ratio") or "1:1").strip()
    if aspect_ratio not in {"1:1", "3:4", "4:3", "9:16", "16:9"}:
        aspect_ratio = "1:1"

    raw_image_paths = args.get("image_paths") or args.get("image_path") or []
    if isinstance(raw_image_paths, str):
        image_paths = [raw_image_paths]
    elif isinstance(raw_image_paths, list):
        image_paths = raw_image_paths
    else:
        image_paths = []

    # 优先级：config.yaml system_models.image_gemini > CLONOTH_IMAGE_GEMINI_* > GEMINI_*
    # > OPENAI_* > 主渠道。主渠道只在它本身就是 Gemini 时才借 —— 这里走的是原生
    # generateContent，别家的地址接不住。model 不跟着借：那个是聊天模型。
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _channel import resolve_image_channel
    from _image import ImagePayloadError, build_image_part

    channel = resolve_image_channel("image_gemini", model_override=str(args.get("model") or ""))
    model = channel.model
    api_key = channel.api_key
    base_url = channel.base_url

    if not api_key:
        fail("No API key found in config.yaml / env / .env file")

    # ---- 构建请求 ----
    url = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"

    parts = []
    for img_path in image_paths:
        img_path = str(img_path).strip()
        if not img_path:
            continue

        img_file = Path.cwd() / img_path
        if not img_file.exists():
            fail(f"Input image not found: {img_path}")

        # 中转站转发的也还是 Gemini 模型，格式限制照 Gemini 那份算。
        try:
            part = build_image_part(img_file, "gemini")
        except ImagePayloadError as e:
            fail(str(e))
        parts.append({
            "inlineData": {
                "mimeType": part.mime,
                "data": part.b64,
            }
        })

    parts.append({"text": prompt_text})

    body = {
        "contents": [
            {
                "role": "user",
                "parts": parts
            }
        ],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
            },
        },
    }

    req_data = json.dumps(body).encode("utf-8")

    # ---- 发送请求（带自动重试） ----
    def _retryable_http(code: int) -> bool:
        return code in (408, 409, 425, 429, 500, 502, 503, 504)

    resp_data = None
    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        req = urllib_request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                pass
            last_error = f"Gemini API HTTP {e.code}: {error_body}"
            retryable = _retryable_http(e.code)
        except URLError as e:
            last_error = f"Gemini API connection error: {e.reason}"
            retryable = True
        except Exception as e:
            last_error = f"Gemini API request failed: {e}"
            retryable = True
        else:
            retryable = False
        if resp_data is not None:
            break
        if attempt < _MAX_ATTEMPTS and retryable:
            time.sleep(_RETRY_BACKOFF_SEC[min(attempt - 1, len(_RETRY_BACKOFF_SEC) - 1)])
            continue
        fail(last_error)

    # ---- 解析响应 ----
    candidates = resp_data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        fail(f"No candidates in response: {json.dumps(resp_data)[:500]}")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not isinstance(parts, list):
        fail("No parts in response")

    text_parts = []
    image_saved = []

    # 确定保存目录
    # 工具运行时 cwd 是 workspace_root
    workspace_root = Path.cwd()
    attachments_dir = workspace_root / "data" / "attachments" / "gemini_image"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    for part in parts:
        if not isinstance(part, dict):
            continue

        # 文本部分
        if "text" in part and isinstance(part["text"], str):
            text_parts.append(part["text"])

        # 图片部分
        inline_data = part.get("inlineData")
        if isinstance(inline_data, dict):
            b64_data = inline_data.get("data", "")
            mime_type = str(inline_data.get("mimeType") or "image/png")

            if not b64_data:
                continue

            # 确定扩展名
            ext = ".png"
            if "jpeg" in mime_type or "jpg" in mime_type:
                ext = ".jpg"
            elif "webp" in mime_type:
                ext = ".webp"
            elif "gif" in mime_type:
                ext = ".gif"

            filename = f"{uuid.uuid4().hex}{ext}"
            file_path = attachments_dir / filename

            try:
                img_bytes = base64.b64decode(b64_data)
                file_path.write_bytes(img_bytes)
            except Exception as e:
                fail(f"Failed to decode/save image: {e}")

            rel_path = file_path.relative_to(workspace_root).as_posix()
            image_saved.append({
                "type": "image",
                "path": rel_path,
                "mime_type": mime_type,
                "name": filename,
            })

    if not image_saved:
        fail(f"Gemini did not return any image. Text response: {' '.join(text_parts)[:500]}")

    text = "\n".join(text_parts).strip()

    # === 只返回 attachments，发图交给 supervisor 的 dispatch_attachment 统一处理 ===
    # [AutoC 2026-05-31] Why: generated image tools now expose their primary text
    # and attachment metadata under data, but legacy attachment collection still
    # reads the top-level field. How: store text, attachments, and image metadata in
    # data and mirror attachments at the top level. Purpose: migrate schema without
    # breaking final image delivery.
    output({
        "ok": True,
        "data": {
            "result": (
                "Image generated and will be sent to the user automatically. "
                "Do NOT resend it via reply/finish."
            ),
            "text": text,
            "attachments": image_saved,
            "image_path": image_saved[0]["path"] if image_saved else "",
            "image_count": len(image_saved),
        },
        "attachments": image_saved,
    })
