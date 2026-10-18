# 应用级插件示例

将 `plugin.json`、`backend.py`、`frontend.js`、`style.css` 放在 ZIP 根目录，在控制台的“插件”页导入后启用。也可以把整个目录放到实例的 `config/plugins/system-workspace/`。

示例在 Supervisor 提供计数服务、后台任务和鉴权接口，在 Engine 注册 `system_plugin_echo` 工具。前端增加工作区并包装聊天区域；停用后移除界面贡献、停止任务并撤销接口和工具。

接口：`GET /v1/plugins/system-workspace/status` 查看计数，`POST /v1/plugins/system-workspace/reset` 重置计数。多实例部署沿用实例路径前缀。工具仍需包含在调用节点允许的工具列表中。

每次更新以完整 ZIP 替换，正在执行的请求继续使用旧版本。使用的 Python 服务和前端脚本都是受信应用代码；SDK 管理的注册可撤销，插件直接写入外部数据的副作用不会因卸载而撤销。
