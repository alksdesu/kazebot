from __future__ import annotations

import logging

from plugin_sdk import PluginAPI, RequestContext

logger = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "Request metadata",
    "version": "1.0.0",
    "description": "Adds a request ID header and records status and event counts.",
    "processes": ["engine"],
    "priority": 100,
    "params_schema": [
        {"key": "add_request_header", "type": "boolean", "default": True},
    ],
}


def setup(api: PluginAPI) -> None:
    @api.before_request
    def before_request(ctx: RequestContext, request: dict) -> None:
        ctx.state["events"] = 0
        if ctx.config["add_request_header"]:
            request["headers"]["X-Clonoth-Plugin-Request"] = ctx.request_id

    @api.after_response
    def after_response(ctx: RequestContext, response: dict) -> None:
        ctx.state["status_code"] = response["status_code"]

    @api.on_stream_event
    def on_stream_event(ctx: RequestContext, event: dict) -> None:
        ctx.state["events"] += 1

    @api.on_request_end
    def on_request_end(ctx: RequestContext, outcome: dict) -> None:
        logger.info(
            "Request %s completed: ok=%s status=%s events=%d",
            ctx.request_id, outcome["ok"], ctx.state.get("status_code"), ctx.state.get("events", 0),
        )
