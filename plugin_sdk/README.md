# Python plugin SDK

For application routes, services, jobs, tools, frontend pages, and ZIP packages, see [application plugins](APPLICATION.md). The middleware API below remains compatible with single-file plugins.

Custom plugins are single Python files in `config/plugins/`. Uploading a plugin validates its syntax and metadata without importing it. New custom plugins start disabled; enabling one authorizes Python code execution in each process that uses the workspace. Bundled `plugins/*.py` files remain enabled by default. A custom file with the same filename overrides its bundled counterpart.

`config/plugins.yaml` stores enabled state, parameter values, and scopes. The console manages this file. Scope lists `nodes`, `channels`, `providers`, and `purposes` are combined with AND; an empty list or `*` accepts every value. Channels are configured channel names, providers are wire protocol identifiers, and purposes are `inference`, `system`, `fallback`, or `diagnostic`. `PLUGIN_META.processes` can restrict initialization to `["engine"]` or `["supervisor"]`; omitting it runs the plugin in both. Excluded processes report `not_applicable` and never import the file.

## Lifecycle

Define synchronous `setup(api)` and optional synchronous `teardown(api)`. Setup runs once per source/configuration/scope version per process and workspace. Register callbacks on the supplied `PluginAPI`; it provides `plugin_id`, `workspace_root`, `config`, and `register_hook(point, handler, priority=None)` for existing engine hooks. Legacy `register(registry)` and `PLUGIN_META.handler_class/hook_points` files remain supported.

Replacing or disabling a version prevents new requests from acquiring it. Requests already running keep their callback version until their end callbacks complete, then the old version is torn down. Failed setup discards every registration it made and attempts teardown. A failed version is retried only after its source or settings change. Plugins should release resources in teardown and avoid process globals for request-specific data.

## Middleware callbacks

Callbacks receive `(ctx, value)` and may be regular or async functions. `ctx` contains `plugin_id`, `workspace_root`, a per-request configuration copy, `request_id`, `node_id`, `channel`, `provider`, `task_id`, `purpose`, `streaming`, and `state`. Each plugin gets its own fresh `state` dictionary for every network attempt; concurrent requests never share it.

| Registration | Value | Return |
| --- | --- | --- |
| `api.before_request(callback)` | `{method, url, headers, body}` | Full replacement dictionary, or `None` after in-place mutation |
| `api.after_response(callback)` | `{status_code, headers, body}` | Full replacement dictionary, or `None` after in-place mutation |
| `api.on_stream_event(callback)` | `{event, data, id, retry}` | Dictionary to replace, list to expand, `[]` to drop, or `None` to keep |
| `api.on_request_end(callback)` | `{ok, cancelled, error, ...}` | Ignored |

Short aliases `request`, `response`, `stream_event`, and `end` work identically and can be used as decorators. Each phase runs in descending `PLUGIN_META.priority` order (integer, default `100`), with plugin ID as the tie breaker; a plugin's own callbacks run in registration order. Legacy Hook priorities remain unchanged. Request/response bodies are decoded JSON when possible, otherwise bytes. SSE `data` is text; the transport handles framing, CRLF, comments, multiline data, and chunk boundaries. SSE transformations happen before protocol-specific delta parsing. Stream response middleware sees response metadata and `body=None` before events; use event callbacks to modify streaming content.

End callbacks run once on success, HTTP failure, callback failure, or cancellation. A callback exception fails the current request, including exceptions in end callbacks; it does not silently continue or retry on another provider. Existing legacy engine hook exception behavior remains unchanged. Runtime diagnostics contain error type, redacted message, and stack locations; request bodies and headers are not logged. Avoid including credentials or message contents in your own exception messages or logs.

## Example

Save the following as a custom plugin, configure `temperature`, select its intended channels, and enable it:

```python
PLUGIN_META = {
    "name": "Request temperature",
    "version": "1.0.0",
    "processes": ["engine"],
    "description": "Applies a configured temperature to JSON requests.",
    "params_schema": [
        {"key": "temperature", "type": "number", "default": 0.7, "minimum": 0, "maximum": 2},
    ],
}


def setup(api):
    @api.before_request
    def configure_temperature(ctx, request):
        body = request["body"]
        if isinstance(body, dict):
            body["temperature"] = ctx.config["temperature"]
```

The inert [request metadata example](../examples/plugins/request_metadata.py) demonstrates all four callbacks without modifying message content. Upload it explicitly to try it; files in `examples/` are never auto-loaded.

Plugins are trusted application code, not sandboxed scripts. Scope filters decide when callbacks run; they do not limit Python's filesystem or network access. Each Supervisor and Engine process reports its own desired/applied generation and errors under `data/plugin_runtime/<pid>.json`.
