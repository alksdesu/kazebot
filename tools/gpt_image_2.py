from __future__ import annotations

"""
GPT Image 2 生图工具 (Clonoth external tool)

运行方式：通过 stdin 接收 JSON 参数，stdout 输出 JSON 结果

发图机制（完全对齐 NovelAI 插件）：
  - async_mode=False：同步执行，出图后只返回 attachments。
  - 图片发送统一交给 supervisor 的 dispatch_attachment 路由（子节点任务完成时
    自动把 attachments 发给用户，不占用 bot 最终回复）。
  - 【重要】工具本身**不再** POST intermediate_reply 推图，否则会与 dispatch_attachment
    双发（同一张图发两次）——这正是 NovelAI 插件当年修复过的坑。

API 渠道配置（与 read_image / system_models 一致）：
  优先读 data/config.yaml 的 system_models.image_gpt（model/base_url/api_key），
  其次 slot 专属环境变量 CLONOTH_IMAGE_GPT_*，留空则回退主渠道
  （OPENAI_BASE_URL / OPENAI_API_KEY，model 默认 gpt-image-2）。
"""

SPEC = {
    'name': 'gpt_image_2',
    'description': '使用 gpt-image-2 模型生成图片。支持自定义分辨率（宽高需被16整除）、quality 参数。图片生成后会自动发送给用户。',
    'async_mode': False,
    'input_schema': {
        'type': 'object',
        'required': ['prompt'],
        'properties': {
            'prompt': {
                'type': 'string',
                'description': '图片描述文本（支持中英文）'
            },
            'size': {
                'type': 'string',
                'default': '1024x1024',
                'description': '分辨率，格式 WxH（宽高均需被16整除）。常用值：1024x1536（竖版）、1920x1088（横版）'
            },
            'quality': {
                'type': 'string',
                'default': 'low',
                'description': '生成质量：low, medium, high, auto（注意：部分网关对 high/auto 有约90秒超时，推荐用 low）'
            },
            'filename': {
                'type': 'string',
                'description': '输出文件名（不含路径），默认自动生成'
            },
            'image_paths': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '可选，本地图片路径列表，作为参考垫图传入模型'
            }
        }
    }
}

# [2026-07-19] 超时/重试对齐：单次请求 120s（正常生图 30-90s，超过基本是挂了），
# 重试 2 次，退避 5s；内部最坏耗时 ≈ 2×120+5 = 245s < 工具总超时 300s，
# 两个超时对齐，避免子进程挂死 15 分钟。
TIMEOUT_SEC = 300.0

# API 请求重试参数（参考 NovelAI：429/5xx/超时/网络错误自动重试）
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SEC = (5.0,)  # 第 1 次失败后等待秒数
_REQUEST_TIMEOUT = 120


if __name__ == "__main__":
    import json, sys
    # Fix Windows GBK stdin encoding - must read as UTF-8
    _input = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import base64, json, urllib.request, urllib.error, os, time, hashlib
    from pathlib import Path

    # [2026-07-19] 方案 X：工具不再自己推图，发图统一交给 supervisor 的
    # dispatch_attachment 路由（和 NovelAI 一致），避免与它双发同一张图。

    prompt = args.get('prompt', '')
    size = args.get('size', '1024x1024')
    quality = args.get('quality', 'auto')
    filename = args.get('filename', '')
    raw_image_paths = args.get('image_paths') or []
    if isinstance(raw_image_paths, str):
        raw_image_paths = [raw_image_paths]

    if not prompt:
        fail('prompt is required')

    # === 分辨率校验 ===
    try:
        w, h = size.split('x')
        w, h = int(w), int(h)
        if w % 16 != 0 or h % 16 != 0:
            fail(f'Width ({w}) and height ({h}) must both be divisible by 16')
    except ValueError:
        fail(f'Invalid size format: {size}. Use WxH, e.g. 1024x1536')

    # 生图渠道独立于主渠道：这是个专用端点，跟着主渠道换家没有意义。
    # 优先级：config.yaml system_models.image_gpt > CLONOTH_IMAGE_GPT_* > OPENAI_*
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _channel import Channel
    from _image import ImagePayloadError, build_image_part, family_for_base_url

    channel = Channel("image_gpt")
    api_key = channel.pick("api_key", "API_KEY", channel.env("OPENAI_API_KEY"))
    base_url = channel.pick("base_url", "BASE_URL", channel.env("OPENAI_BASE_URL")).rstrip("/")
    model_name = channel.pick("model", "MODEL", "gpt-image-2")

    if not api_key:
        fail('No API key found in config.yaml / env / .env file')
    if not base_url:
        fail('No base_url configured (system_models.image_gpt / OPENAI_BASE_URL)')

    if base_url.endswith("/chat/completions"):
        url = base_url
    else:
        url = base_url.rstrip("/") + "/chat/completions"

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }

    # === 构建消息体（支持垫图） ===
    content_parts = []
    for img_path in raw_image_paths:
        img_path = str(img_path).strip()
        if not img_path:
            continue
        img_file = Path.cwd() / img_path
        if not img_file.exists():
            fail(f'Input image not found: {img_path}')
        try:
            part = build_image_part(img_file, family_for_base_url(base_url))
        except ImagePayloadError as e:
            fail(str(e))
        content_parts.append({'type': 'image_url', 'image_url': {'url': part.data_url()}})
    content_parts.append({'type': 'text', 'text': prompt})

    msg_content = content_parts if len(content_parts) > 1 else prompt

    payload = {
        'model': model_name,
        'messages': [{'role': 'user', 'content': msg_content}],
        'size': size,
        'quality': quality
    }
    payload_json = json.dumps(payload)

    # === 发起请求（带自动重试） ===
    def _is_retryable(exc) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in (408, 409, 425, 429, 500, 502, 503, 504)
        # URLError / timeout / 网络错误 → 可重试
        return True

    res_data = None
    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=payload_json.encode('utf-8'), headers=headers, method='POST')
        try:
            resp = urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT)
            res_data = json.loads(resp.read().decode('utf-8'))
            break
        except Exception as e:
            err_body = ''
            if hasattr(e, 'read'):
                try:
                    err_body = e.read().decode()[:500]
                except Exception:
                    pass
            last_error = f'{e} {err_body}'.strip()
            if attempt < _MAX_ATTEMPTS and _is_retryable(e):
                wait = _RETRY_BACKOFF_SEC[min(attempt - 1, len(_RETRY_BACKOFF_SEC) - 1)]
                time.sleep(wait)
                continue
            fail(f'API request failed after {attempt} attempt(s): {last_error}')

    if res_data is None:
        fail(f'API request failed: {last_error}')

    # === 从响应中提取图片数据 ===
    msg = res_data.get('choices', [{}])[0].get('message', {})
    images = msg.get('images', [])

    img_data = None
    if images:
        raw = images[0]
        img_data = base64.b64decode(raw.split(',')[-1] if ',' in raw else raw)
    else:
        content = msg.get('content', '')
        if content:
            import re
            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', str(content))
            if m:
                img_data = base64.b64decode(m.group(1))

    if not img_data:
        fail('No image data in response: ' + json.dumps(res_data)[:500])

    # === 保存图片 ===
    if not filename:
        ts = str(time.time()).encode()
        h = hashlib.md5(ts + prompt[:50].encode()).hexdigest()[:10]
        filename = f'gpt_image_{h}.png'

    out_dir = os.path.join('data', 'attachments', 'gpt_image')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename).replace('\\', '/')
    with open(out_path, 'wb') as f:
        f.write(img_data)

    try:
        from PIL import Image
        img = Image.open(out_path)
        actual_size = f'{img.size[0]}x{img.size[1]}'
    except Exception:
        actual_size = size

    # 附件结构（与 gemini_image / 平台附件路由兼容）
    attachment = {
        "type": "image",
        "path": out_path,
        "mime_type": "image/png",
        "name": os.path.basename(out_path),
    }

    # === 只返回 attachments，发图交给 supervisor 的 dispatch_attachment 统一处理 ===
    output({
        'ok': True,
        'data': {
            'result': (
                'Image generated and will be sent to the user automatically. '
                'Do NOT resend it via reply/finish.'
            ),
            'path': out_path,
            'attachments': [attachment],
            'size': actual_size,
            'quality': quality,
            'bytes': len(img_data),
        },
        'attachments': [attachment]
    })
