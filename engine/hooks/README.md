# Hook System

`engine/hooks/` 是 Clonoth 的推理循环钩子系统。它提供 `HookRegistry`、`Handler`、`HookContext`、`HookResult` 和外部插件加载器。

## 主要文件

| 文件 | 说明 |
| --- | --- |
| `types.py` | 定义 `Handler`、`HookContext`、`HookResult`。 |
| `registry.py` | 定义 `HookRegistry`，负责注册、排序、触发 handler。 |
| `loader.py` | 将旧版 Hook 插件交给工作区插件运行时加载。 |
| `../builtin/` | 保存内置 handler，并由 `auto_discover_and_register()` 根据 `PLUGIN_META` 自动注册。 |

## 当前 hook point

- `before_prompt_build`
- `before_step`
- `before_tool_call`
- `after_tool_call`
- `before_response`
- `after_llm_call`
- `on_task_end`
- `on_task_error`

## 执行规则

同一个 hook point 下的 handler 按 `priority` 从高到低运行。同一插件内按 `handler.name` 去重；不同插件与内置 handler 的所有权互相隔离。并发节点使用各自的注册表，插件版本由工作区运行时统一管理，停用后不会残留注册。

handler 返回 `None` 表示不介入；返回 `HookResult(modified=True)` 表示修改了上下文但继续执行；返回 `block`、`skip_step` 或 `action` 会停止当前 hook 链。

新插件可通过 [`plugin_sdk`](../../plugin_sdk/README.md) 注册请求、响应和 SSE 中间件；旧版 `register(registry)` 与 `PLUGIN_META.handler_class/hook_points` 继续可用。
