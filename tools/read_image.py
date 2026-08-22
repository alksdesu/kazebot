from __future__ import annotations

"""Image understanding tool for text-only models.

Provides vision capabilities to non-multimodal models (like DeepSeek) by calling
a multimodal model to describe image content. The channel is whatever
system_models.image points at; the wire format follows that channel's provider.

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
        "Provide local image paths to analyze. Uses the vision channel configured in "
        "system_models.image (or CLONOTH_IMAGE_MODEL)."
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
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _channel import resolve_vision_channel
    from _image import ImagePayloadError, build_image_part
    from _wire import build_request, error_detail, parse_text, relax_body, truncated

    vision = resolve_vision_channel()
    if vision.error:
        fail(vision.error)

    model = vision.model

    # ---- Build content parts ----
    image_urls = []

    for img_path in image_paths:
        p = Path.cwd() / str(img_path).strip()
        if not p.exists():
            fail(f"Image not found: {img_path}")

        try:
            part = build_image_part(p, vision.family)
        except ImagePayloadError as e:
            fail(str(e))
        image_urls.append(part.data_url())

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

    request = build_request(
        wire=vision.wire,
        base_url=vision.base_url,
        api_key=vision.api_key,
        model=model,
        image_urls=image_urls,
        system=system_prompt,
        prompt="Describe this image in complete detail following your instructions.",
        max_tokens=4096,
        temperature=0.1,
    )

    # ---- Send request ----
    def send(payload):
        """POST and return (status, content_type, raw_text). Never raises on 4xx/5xx."""
        req = urllib_request.Request(
            request.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=request.headers,
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=100) as resp:
                return (
                    int(getattr(resp, "status", 200) or 200),
                    str(resp.headers.get("Content-Type") or "unknown"),
                    resp.read().decode("utf-8", errors="replace"),
                )
        except HTTPError as exc:
            try:
                return (
                    exc.code,
                    str(exc.headers.get("Content-Type") or "unknown"),
                    exc.read().decode("utf-8", errors="replace"),
                )
            except Exception:
                return (exc.code, "unknown", "")
        except URLError as exc:
            fail(f"API connection error: {exc.reason}")
        except Exception as exc:
            fail(f"API request failed: {exc}")

    status, content_type, raw = send(request.body)
    if status != 200:
        # Read the body to decide whether a retry is worth it, but never echo it:
        # proxy login pages and gateway diagnostics may carry cookies or reflected
        # secrets. Status, type and size are enough for safe diagnosis.
        try:
            detail = error_detail(json.loads(raw))
        except Exception:
            detail = ""
        retry = relax_body(vision.wire, request.body, detail)
        if retry is not None:
            status, content_type, raw = send(retry)
    if status != 200:
        fail(f"API HTTP {status} (content_type={content_type}, body_bytes={len(raw.encode('utf-8'))})")

    try:
        resp_json = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(
            "API returned invalid JSON "
            f"(status={status}, content_type={content_type}, "
            f"body_bytes={len(raw.encode('utf-8'))}, parse_error={exc})"
        )

    # ---- Parse response ----
    description = parse_text(vision.wire, resp_json)

    if truncated(vision.wire, resp_json) and not description:
        fail("Vision model hit max_tokens before producing any text.")

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
