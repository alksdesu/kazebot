"""异步结果回调入站的字段名：supervisor 写进入站事件和任务 input，engine 照它们落盘、给模型拼前缀。

合并交回的一轮里每条结果用和单条交回相同的 inbound_* 键；交回说明只拼进给模型的前缀，不进对话历史。
"""
from __future__ import annotations

DISPATCH_RESULTS_PAYLOAD_KEY = "dispatch_results"
INBOUND_RESULTS_KEY = "inbound_results"

CONTINUES_TASK_PAYLOAD_KEY = "continues_task_id"
INBOUND_CONTINUES_TASK_KEY = "inbound_continues_task_id"

DELIVERY_NOTE_PAYLOAD_KEY = "delivery_note"
INBOUND_DELIVERY_NOTE_KEY = "inbound_delivery_note"

# 异步任务入站时记下的回落落点：发起它的任务记录日后没了，结果照这里交
RESULT_FALLBACK_KEY = "_result_fallback"
