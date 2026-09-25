from __future__ import annotations

import re
from typing import Any

COMMANDS = frozenset({"计划", "批处理", "任务", "计划确认", "计划修改", "计划取消", "失败重试", "步骤核对"})
CONTROL_COMMANDS = frozenset({"任务", "计划取消"})

STATUS = {"draft": "待确认", "queued": "排队中", "running": "执行中", "awaiting_approval": "等待审批", "completed": "完成", "succeeded": "完成", "failed": "失败", "partial": "部分完成", "blocked": "等待依赖", "cancelled": "已取消", "cancelling": "正在停止", "outcome_unknown": "需核对实际结果", "pending": "待执行"}


def render(plan: dict) -> str:
    lines = [f"计划 {plan['id']} · 版本 {plan['revision']} · {STATUS.get(plan['status'], plan['status'])}", f"目标：{plan['goal']}"]
    if plan.get("work_scope"):
        lines.append(f"范围：{plan['work_scope']}")
    lines.extend(f"{index + 1}. {step['title']}：{STATUS.get(step['status'], step['status'])}" + (f"（{step['error']}）" if step.get("error") else "") for index, step in enumerate(plan["steps"]))
    if plan["status"] == "draft":
        lines.append(f"确认执行：/计划确认 {plan['id']} {plan['revision']}\n修改目标：/计划修改 {plan['id']} 目标 新目标\n修改范围：/计划修改 {plan['id']} 范围 新范围")
    else:
        lines.append(f"查看：/任务 {plan['id']}\n停止：/计划取消 {plan['id']}\n仅重试失败步骤：/失败重试 {plan['id']}")
    return "\n".join(lines)


async def handle(client: Any, actor: dict, text: str, reply_ref: str = "") -> dict | None:
    parts = text.strip().lstrip("/").split(maxsplit=1)
    if not parts or parts[0] not in COMMANDS:
        return None
    command, rest = parts[0], parts[1] if len(parts) > 1 else ""
    try:
        if command in {"计划", "批处理"}:
            goal, *sources = [part.strip() for part in rest.split("|")]
            if not goal:
                return {"text": "用法：/计划 目标 | 资料或链接\n批量示例：/批处理 总结要点 | https://example.com/a | https://example.com/b"}
            inputs = [{"kind": "url" if re.match(r"^https?://", value) else "text", "value": value, "label": f"资料 {index + 1}"} for index, value in enumerate(sources) if value]
            plan = await client.request_feature("POST", "/v1/execution/plans", body={"goal": goal, "inputs": inputs}, actor=actor)
            return {"text": render(plan)}
        if command == "任务" and not rest:
            response = await client.request_feature("GET", "/v1/execution/plans", actor=actor)
            return {"text": "\n".join(f"{plan['id']} {STATUS.get(plan['status'], plan['status'])} {plan['goal']}" for plan in response["plans"]) or "暂无计划。"}
        tokens = rest.split(maxsplit=2)
        plan_id = tokens[0] if tokens else reply_ref
        if not re.fullmatch(r"P[0-9a-f]{12}", plan_id):
            return {"text": "请给出明确计划编号，例如 /任务 P123456789abc。"}
        plan = await client.request_feature("GET", f"/v1/execution/plans/{plan_id}", actor=actor)
        if command == "任务":
            return {"text": render(plan)}
        if command == "计划确认":
            if len(tokens) < 2 or not tokens[1].isdigit():
                return {"text": f"请确认具体版本：/计划确认 {plan_id} {plan['revision']}"}
            plan = await client.request_feature("POST", f"/v1/execution/plans/{plan_id}/confirm", body={"expected_revision": int(tokens[1])}, actor=actor)
        elif command == "计划修改":
            if len(tokens) < 3 or tokens[1] not in {"目标", "范围"}:
                return {"text": "用法：/计划修改 计划编号 目标/范围 修改后的内容。输入和步骤也可在控制台修改。"}
            body = {key: plan[key] for key in ("goal", "work_scope", "inputs", "steps")}
            body["goal" if tokens[1] == "目标" else "work_scope"] = tokens[2]
            body["expected_revision"] = plan["revision"]
            plan = await client.request_feature("PATCH", f"/v1/execution/plans/{plan_id}", body=body, actor=actor)
        elif command == "计划取消":
            plan = await client.request_feature("POST", f"/v1/execution/plans/{plan_id}/cancel", actor=actor)
        elif command == "失败重试":
            plan = await client.request_feature("POST", f"/v1/execution/plans/{plan_id}/retry", body={"step_ids": []}, actor=actor)
        elif command == "步骤核对":
            return {"text": "请在控制台展开需核对步骤，确认实际是否执行并填写核对说明；核对前不会自动重试。"}
        return {"text": render(plan)}
    except Exception as exc:
        return {"text": f"计划操作未完成：{exc}"}
