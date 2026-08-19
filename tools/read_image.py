from __future__ import annotations

"""Image understanding tool for text-only models.

Provides vision capabilities to non-multimodal models (like DeepSeek) by calling
a multimodal model (Gemini) via OpenAI-compatible API to describe image content.

ONLY for text-only models. Multimodal models should read images directly.

Requires GEMINI_API_KEY or OPENAI_API_KEY environment variable.
"""

SPEC = {
    "name": "read_image",
    # Image understanding is a dependency of the current answer, not a detached
    # deliverable. Keep it synchronous so the model must observe the real success
    # or failure before it can finish the turn.
    "async_mode": False,
    "description": (
        "[ONLY for text-only models like DeepSeek. Do NOT use if you can see images natively.] "
        "Analyze image(s) and return a comprehensive text description including all visible text (OCR), "
        "layout, colors, objects, people, style, and context. "
        "Provide local image paths to analyze. Uses a Gemini vision model internally "
        "(configurable via CLONOTH_IMAGE_MODEL)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of local image paths (relative to workspace root) to analyze.",
            },
            "image_path": {
                "type": "string",
                "description": "Single local image path to analyze (convenience shorthand).",
            },
            "focus": {
                "type": "string",
                "description": "Optional focus area: 'OCR', 'style', 'objects', 'layout', or free text. Default: comprehensive.",
            },
        },
        "required": [],
    },
}

TIMEOUT_SEC = 120

if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path
    from urllib import request as urllib_request
    from urllib.error import HTTPError, URLError

    _input = json.loads(sys.stdin.read())

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        # [AutoC 2026-05-31] Why: read_image may fail before it can return a
        # description, but the engine still expects data.result for readable tool
        # history. How: include ERROR text under data.result in the failure JSON.
        # Purpose: keep vision-tool failures understandable after schema migration.
        print(json.dumps({
            "ok": False,
            "error": str(error),
            "data": {
                "result": f"ERROR: {error}",
                "vision_failed": True,
                "must_not_guess": True,
            },
        }, ensure_ascii=False))
        sys.exit(1)

    args = _input

    # ---- Collect image paths ----
    raw_paths = args.get("image_paths") or args.get("image_path") or []
    if isinstance(raw_paths, str):
        image_paths = [raw_paths]
    elif isinstance(raw_paths, list):
        image_paths = raw_paths
    else:
        image_paths = []

    if not image_paths:
        fail("No image_paths provided. Supply image_path or image_paths.")

    focus = str(args.get("focus") or "").strip()

    # ---- API Configuration ----
    # 读图渠道先看 system_models.image；没配就跟随主渠道 —— 常见部署是同一个中转站
    # 换个模型名。模型名不跟随：主渠道多半正是那个看不了图的纯文本模型。
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _channel import OPENAI_COMPATIBLE, Channel
    from _image import ImagePayloadError, build_image_part, family_for_base_url

    channel = Channel("image")
    main = channel.main()

    base_url = channel.own("base_url", "BASE_URL")
    following = not base_url
    if following:
        # 跟随时格式也得对得上：这里发的是 OpenAI 的 /chat/completions。
        if main.base_url and main.provider not in OPENAI_COMPATIBLE:
            fail(
                "主渠道 " + main.provider + " 不收 OpenAI 格式的 /chat/completions，read_image 没法跟随。"
                "请在 data/config.yaml 的 system_models.image 里单独配 base_url 和 api_key。"
            )
        base_url = main.base_url or channel.env("OPENAI_BASE_URL")

    api_key = channel.pick(
        "api_key", "API_KEY",
        main.api_key if following else "",
        channel.env("GEMINI_API_KEY"),
        channel.env("OPENAI_API_KEY"),
    )
    if not api_key:
        fail("No API key found in config.yaml / env / .env file")

    base_url = base_url.rstrip("/")
    if not base_url:
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1" if "/v1" not in base_url else base_url

    model = channel.pick("model", "MODEL", "gemini-3.5-flash")
    url = f"{base_url}/chat/completions"
    family = family_for_base_url(base_url)

    # ---- Build content parts ----
    content_parts = []

    for img_path in image_paths:
        p = Path.cwd() / str(img_path).strip()
        if not p.exists():
            fail(f"Image not found: {img_path}")

        try:
            part = build_image_part(p, family)
        except ImagePayloadError as e:
            fail(str(e))
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": part.data_url()},
        })

    # ---- System & user prompt ----
    focus_instruction = ""
    if focus:
        focus_instruction = f"\nThe user specifically wants you to focus on: {focus}."

    system_prompt = (
        "You are an expert image analyst. Your job is to describe images in exhaustive detail "
        "so that someone who cannot see the image gets a complete understanding of its content.\n\n"
        "For EVERY image, you MUST cover ALL of the following:\n"
        "1. **OCR / Text**: Transcribe ALL visible text exactly as written — titles, labels, captions, "
        "watermarks, UI text, code snippets, error messages, chat bubbles, etc. Preserve formatting.\n"
        "2. **Layout & Composition**: Describe spatial arrangement — what is where, columns, rows, "
        "panels, split screens, overlays, margins, alignment.\n"
        "3. **Visual Style**: Art style, color palette, lighting, contrast, filters, resolution quality.\n"
        "4. **Objects & Entities**: Every distinct object, icon, logo, symbol, UI element, chart, graph.\n"
        "5. **People & Actions**: Faces, expressions, poses, gestures, clothing, interactions.\n"
        "6. **Context & Meaning**: What is this image about? Is it a screenshot, photo, diagram, meme, "
        "chart, code, conversation? What platform/app is shown? What is the overall message?\n\n"
        "Be thorough. Miss nothing. If the image contains code or terminal output, reproduce it verbatim.\n"
        "If there are multiple images, describe each one separately with clear numbering."
        + focus_instruction
    )

    content_parts.append({
        "type": "text",
        "text": "Describe this image in complete detail following your instructions."
    })

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts}
        ],
        "temperature": 0.1,
        "max_tokens": 4096
    }

    req_data = json.dumps(body).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST",
    )

    # ---- Send request ----
    try:
        with urllib_request.urlopen(req, timeout=100) as resp:
            response_status = int(getattr(resp, "status", 200) or 200)
            response_type = str(resp.headers.get("Content-Type") or "unknown")
            response_text = resp.read().decode("utf-8", errors="replace")
        try:
            resp_json = json.loads(response_text)
        except json.JSONDecodeError as exc:
            # Do not echo an upstream body into the tool transcript: proxy login
            # pages and gateway diagnostics may contain cookies, request metadata,
            # or reflected secrets. Status, type, size and parse location are enough
            # for safe diagnosis.
            fail(
                "API returned invalid JSON "
                f"(status={response_status}, content_type={response_type}, "
                f"body_bytes={len(response_text.encode('utf-8'))}, parse_error={exc})"
            )
    except HTTPError as e:
        content_type = "unknown"
        body_bytes = 0
        try:
            content_type = str(e.headers.get("Content-Type") or "unknown")
            body_bytes = len(e.read())
        except Exception:
            pass
        fail(f"API HTTP {e.code} (content_type={content_type}, body_bytes={body_bytes})")
    except URLError as e:
        fail(f"API connection error: {e.reason}")
    except Exception as e:
        fail(f"API request failed: {e}")

    # ---- Parse response ----
    choices = resp_json.get("choices")
    if not isinstance(choices, list) or not choices:
        fail(f"No choices in response: {json.dumps(resp_json)[:500]}")

    message = choices[0].get("message", {})
    description = message.get("content", "").strip()

    if not description:
        fail(f"Empty response from vision model: {json.dumps(resp_json)[:500]}")

    # [AutoC 2026-05-31] Why: read_image's description is the human-readable tool
    # result and should live at data.result. How: move description and metadata
    # under data while keeping the same values. Purpose: conform to ok/data/error
    # without losing model and image count metadata.
    output({
        "ok": True,
        "data": {
            "result": description,
            "description": description,
            "model": model,
            "images_analyzed": len(image_paths)
        }
    })
