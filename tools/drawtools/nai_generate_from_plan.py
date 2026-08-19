from __future__ import annotations

"""Generate NovelAI images from Scene Planner YAML.

This is the formatting/generation half of the LWB-like pipeline:
planner node -> YAML(scene/chars/costume/uc/center/size_label) -> this tool -> NovelAI images.
"""

SPEC = {
    "name": "nai_generate_from_plan",
    "description": "读取绘图分析节点输出的 YAML 计划，格式化为 NovelAI V4/V4.5 prompt 与角色级 caption，并逐张调用 NovelAI 生图。",

    # 等待真实工具结果。成功才返回附件；失败就把 HTTP/key/超时原因直接交给模型和用户，不再出现双分支假成功。
    "async_mode": False,
    "input_schema": {
        "type": "object",
        "properties": {
            "plan_yaml": {"type": "string", "description": "绘图分析节点输出的 YAML 文本，包含 images 列表。推荐且标准字段。"},
            "yaml": {"type": "string", "description": "兼容字段：等同 plan_yaml"},
            "plan": {"description": "兼容字段：可以是 YAML 字符串，也可以是已解析的计划对象"},
            "images": {"type": "array", "description": "兼容字段：模型误把 images 单独传入时会自动包装为 YAML 计划"},
        },
        "required": ["plan_yaml"],
    },
}

TIMEOUT_SEC = 900.0


if __name__ == "__main__":
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from urllib import request as urllib_request

    from common import PresetNotFoundError
    from format_plan import build_tasks_from_plan

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    def emit_intermediate(text, attachments=None):
        supervisor_url = os.environ.get("CLONOTH_SUPERVISOR_URL", "").rstrip("/")
        session_id = os.environ.get("CLONOTH_PARENT_SESSION_ID") or os.environ.get("CLONOTH_SESSION_ID") or ""
        if not supervisor_url or not session_id:
            return
        payload = {
            "type": "intermediate_reply",
            "payload": {
                "node_id": os.environ.get("CLONOTH_NODE_ID", ""),
                "task_id": os.environ.get("CLONOTH_TASK_ID", ""),
                "text": str(text or ""),
                "attachments": attachments or [],
            },
        }
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib_request.Request(
                f"{supervisor_url}/v1/sessions/{session_id}/events",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib_request.urlopen(req, timeout=3).read()
        except Exception:
            pass

    args = json.loads(sys.stdin.read() or "{}")
    raw_plan = args.get("plan_yaml") or args.get("yaml") or args.get("plan")
    if not raw_plan and isinstance(args.get("images"), list):
        raw_plan = {"mindful_prelude": {"visual_plan": {"reasoning": "auto-wrapped from images field"}}, "images": args.get("images")}
    if isinstance(raw_plan, dict):
        try:
            import yaml
            plan_yaml = yaml.safe_dump(raw_plan, allow_unicode=True, sort_keys=False)
        except Exception:
            plan_yaml = json.dumps(raw_plan, ensure_ascii=False)
    else:
        plan_yaml = str(raw_plan or "").strip()
    if not plan_yaml:
        fail("plan_yaml is required; accepted aliases: yaml, plan, images")

    try:
        tasks = build_tasks_from_plan(plan_yaml)
    except PresetNotFoundError as exc:
        fail(f"{exc}；可用预设：{'、'.join(exc.available)}")
    except Exception as exc:
        fail(f"failed to parse plan_yaml: {exc}")
    if not tasks:
        fail("no image task parsed from plan_yaml")

    tool_path = Path(__file__).with_name("nai_generate.py")
    attachments = []
    results = []
    failures = []

    for task in tasks:
        payload = {
            "prompt": task["prompt"],
            "negative_prompt": task["negative_prompt"],
            "character_prompts": task["character_prompts"],
            "width": task["width"],
            "height": task["height"],
            "size_label": task["size_label"],
            "params": task["params"],
            "preset_id": task.get("preset_id", ""),
        }
        # seed 策略（与 format_plan 配合）：
        #   - image 级显式 seed（seed_from_image=True）：用原值、不加偏移，
        #     便于“多张 tag 不同但用完全相同的同一个 seed”。
        #   - plan 级基准 seed：按图序递增（seed + (index-1)），整批可复现且每张不同。
        #   - None/缺省或负数：不传，交给 nai_generate 随机生成。
        seed_raw = task.get("seed")
        if seed_raw is not None:
            try:
                seed_int = int(seed_raw)
            except (TypeError, ValueError):
                seed_int = -1
            if seed_int >= 0:
                if task.get("seed_from_image"):
                    payload["seed"] = seed_int
                else:
                    offset = int(task.get("index") or 1) - 1
                    payload["seed"] = seed_int + offset
        try:
            proc = subprocess.run(
                [sys.executable, str(tool_path)],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=190,
            )
            parsed = json.loads((proc.stdout or "").strip() or "{}")
            if proc.returncode != 0 or parsed.get("ok") is False:
                parsed_data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
                error_text = parsed.get("error") or parsed_data.get("result") or proc.stderr or f"exit {proc.returncode}"
                failures.append({"index": task["index"], "error": str(error_text)})
                continue
            data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
            att = data.get("attachments") or parsed.get("attachments") or []
            image_path = str(data.get("path") or "").strip()
            if isinstance(att, list):
                attachments.extend(att)
            # 逐张自动发图不能依赖工具子进程直接 POST
            # 让 QQ/OneBot、Web 等平台都从同一条附件路由发送图片。
            if not image_path and not att:
                failures.append({"index": task["index"], "error": "NovelAI 工具返回成功但没有图片路径或附件"})
                continue
            results.append({
                "index": task["index"],
                "anchor": task.get("anchor", ""),
                "size_label": task["size_label"],
                "width": task["width"],
                "height": task["height"],
                "preset_id": task.get("preset_id", ""),
                "preset_name": task.get("preset_name", ""),
                "path": image_path,
                "seed": data.get("seed"),
            })
        except Exception as exc:
            failures.append({"index": task["index"], "error": str(exc)})

    if not results:
        failure_text = "; ".join(f"#{f['index']} {f['error']}" for f in failures) or "unknown error"
        fail(f"NovelAI 生图失败：{failure_text}")

    # [2026-07-08] 修复重复发图：
    #   图片附件在子任务完成时已由 supervisor 的 dispatch_attachment 直接发送给平台，
    #   这里绝不能再暴露真实磁盘路径、也不能诱导上游 LLM 用 reply(attachment_paths=...)
    #   重新发送，否则会造成同一批图片发送两次（生图直发 + LLM 重发）。
    #   因此成功文案只描述“已自动发送、无需再发”，路径细节仅保留在结构化 images 字段里
    #   供程序使用，不再拼进 result 文本喂给模型。
    lines = [f"✅ NovelAI 生图成功：{len(results)} 张。"]
    lines.append("图片已自动发送给用户，无需再次发送，也不要调用 reply/finish 携带这些图片路径。")
    for item in results:
        lines.append(f"- #{item['index']} {item['size_label']} 已发送")
    if failures:
        lines.append("⚠️ 部分图片失败：" + "; ".join(f"#{f['index']} {f['error']}" for f in failures))

    output({
        "ok": True,
        "data": {
            "result": "\n".join(lines),
            "success": True,
            "success_count": len(results),
            "failure_count": len(failures),
            "images": results,
            "failures": failures,
            "attachments": attachments,
        },
        "attachments": attachments,
    })
