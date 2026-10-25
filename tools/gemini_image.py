from __future__ import annotations

"""Gemini native image generation tool (Nano Banana).

调 generateContent（responseModalities 带 IMAGE）出图，存进 data/attachments/ 作为附件返回；
发图交给 supervisor 的 dispatch_attachment 路由，工具自己不推图，免得同一张图发两遍。
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
            "material_artifact_id": {"type": "string", "description": "Existing image artifact to extend."},
            "material_base_version": {"type": "string", "description": "Immutable reference version for this edit."},
            "material_expected_version": {"type": "string", "description": "Current artifact version observed before generation."},
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

# 内部最坏耗时 2×120+5 = 245s，要留在工具总超时 300s 以内。
TIMEOUT_SEC = 300

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
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    args = _input

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

    # 优先级：models.yaml system_models.image_gemini > CLONOTH_IMAGE_GEMINI_* > GEMINI_*
    # > OPENAI_* > 主渠道。主渠道只在它本身就是 Gemini 时才借 —— 这里走的是原生
    # generateContent，别家的地址接不住。model 不跟着借：那个是聊天模型。
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _channel import resolve_image_channel
    from _image import ImagePayloadError, build_image_part
    from _wire import WIRE_GEMINI, endpoint, headers as auth_headers

    channel = resolve_image_channel("image_gemini", model_override=str(args.get("model") or ""))
    model = channel.model
    api_key = channel.api_key
    base_url = channel.base_url

    if not api_key:
        fail("No API key found in config/models.yaml / env / .env file")

    # 密钥放请求头、不进 URL：中转站的访问日志和各种报错里都会带上完整 URL。官方域名和中转站的鉴权头与 provider 同一套。
    url = endpoint(WIRE_GEMINI, base_url, model)
    request_headers = auth_headers(WIRE_GEMINI, base_url, api_key)

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

    def _retryable_http(code: int) -> bool:
        return code in (408, 409, 425, 429, 500, 502, 503, 504)

    resp_data = None
    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        req = urllib_request.Request(
            url,
            data=req_data,
            headers=request_headers,
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

    candidates = resp_data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        fail(f"No candidates in response: {json.dumps(resp_data)[:500]}")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not isinstance(parts, list):
        fail("No parts in response")

    text_parts = []
    image_saved = []

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

    # 附件在顶层也留一份：最终发图的附件收集还从顶层读。
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
