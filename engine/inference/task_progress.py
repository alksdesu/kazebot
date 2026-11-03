"""按需查询任务状态，进度变化时最多要求重写一次。"""
from __future__ import annotations

import json
from typing import Any

from clonoth_sdk.task_status import MAX_STATUS_TASKS, dated_progress, read_snapshot


async def query_status(ls: Any, args: dict[str, Any]) -> str:
    ids = args.get("task_ids", [])
    if not isinstance(ids, list) or len(ids) > MAX_STATUS_TASKS or any(not isinstance(t, str) or not t.strip() for t in ids):
        return "task_ids 必须是任务编号列表，一次最多 20 个。"
    try:
        response = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/tasks/{ls.rctx.task_id}/family/status",
            json={"task_ids": ids}, timeout=10,
        )
        response.raise_for_status()
        snapshot = read_snapshot(response.json())
    except Exception:
        return "查询任务状态失败，当前进度未知；不要把旧记录当成最新状态。"
    ls.status_snapshot = snapshot
    return json.dumps(snapshot, ensure_ascii=False) + (
        "\n这是查询时的状态，任务结束不代表文件送达。delivery.records 的 sent 才是发送端确认；"
        "unknown 表示不能确认，failed 是某次投递失败，部分 sent 不能当成全部成功；counts 按投递批次计数。"
        "model_request 表示等待模型响应，不能据此断言只剩最后收尾。汇报进度时 finish 的 status_query_id 填 query_id；"
        "只是追加指令或回答其他问题时不填。不要向用户暴露内部编号。"
    )


async def prepare_progress_reply(ls: Any, query_id: Any, text: str) -> tuple[str, str]:
    snapshot = getattr(ls, "status_snapshot", None)
    if not isinstance(query_id, str) or not isinstance(snapshot, dict) or snapshot.get("query_id") != query_id:
        return text, "status_query_id 必须来自本轮 get_task_status 的最近一次查询，请先查询。"
    try:
        response = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/tasks/{ls.rctx.task_id}/family/status/check",
            json={"query_id": query_id}, timeout=10,
        )
        response.raise_for_status()
        checked = response.json()
        if not isinstance(checked, dict) or not isinstance(checked.get("changed"), bool):
            raise ValueError("状态复核回执无效")
        latest = read_snapshot(checked.get("snapshot"))
    except Exception:
        return dated_progress(text, snapshot["observed_at"]), ""
    if checked["changed"] and not getattr(ls, "status_refresh_count", 0) and getattr(ls, "status_can_refresh", True):
        ls.status_refresh_count = 1
        ls.status_snapshot = latest
        return text, (
            "任务状态已变化，这条进度回复尚未发送。按以下状态更新回复，并使用新的 query_id；"
            "不要重新执行发送文件、追加或取消等操作。\n" + json.dumps(latest, ensure_ascii=False)
        )
    # 模型生成与平台发送无法共用锁，时间限定也覆盖复核后的竞态。
    return dated_progress(text, snapshot["observed_at"]), ""
