from __future__ import annotations

from typing import Any

from toolbox.feature_client import request


async def draft_execution_plan(args: dict[str, Any], ctx: Any) -> dict:
    try:
        plan = await request(ctx, "POST", "/v1/execution/plans", body=args)
        return {"ok": True, "data": {"result": f"计划 {plan['id']} 已生成草稿，等待用户确认版本 {plan['revision']} 后执行。", "plan": plan}}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": {"result": str(exc)}}


async def inspect_execution_plan(args: dict[str, Any], ctx: Any) -> dict:
    try:
        plan_id = str(args.get("plan_id") or "")
        path = f"/v1/execution/plans/{plan_id}" if plan_id else "/v1/execution/plans"
        data = await request(ctx, "GET", path)
        return {"ok": True, "data": {"result": "执行计划状态", **data}}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": {"result": str(exc)}}


async def revise_execution_plan(args: dict[str, Any], ctx: Any) -> dict:
    try:
        body = dict(args)
        plan_id = str(body.pop("plan_id"))
        data = await request(ctx, "PATCH", f"/v1/execution/plans/{plan_id}", body=body)
        return {"ok": True, "data": {"result": f"计划 {plan_id} 已修改，需要用户确认新版本。", "plan": data}}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": {"result": str(exc)}}


def register_tools(registry: Any) -> None:
    from supervisor.execution.models import PlanDraft

    schema = PlanDraft.model_json_schema()
    registry.register_builtin_tool("draft_execution_plan", "生成可修改的步骤计划或多链接/文件批处理草稿。只有用户确认后才执行。工具步骤仍受原节点权限和操作审批约束。", schema, draft_execution_plan)
    registry.register_builtin_tool("inspect_execution_plan", "查看本人计划的真实步骤、逐项结果、错误与产物。", {"type": "object", "properties": {"plan_id": {"type": "string"}}}, inspect_execution_plan)
    edit_schema = {**schema, "properties": {**schema["properties"], "plan_id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 1}}, "required": ["goal", "plan_id", "expected_revision"]}
    registry.register_builtin_tool("revise_execution_plan", "在用户确认前修改计划目标、范围、输入和步骤；修改后旧确认失效。", edit_schema, revise_execution_plan)
