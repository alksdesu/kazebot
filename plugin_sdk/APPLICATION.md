# 应用级插件

插件可以提供控制台工作区和设置页，包装内置页面、布局和操作，注册后端接口、服务、工具及后台任务。已有模型请求插件和旧 Hook 插件继续使用原接口。

## 安装和更新

在“通用 → 客户端 → 插件”下导入 `.py` 或 ZIP。新插件默认停用，保存配置并启用后，各进程和浏览器分别加载；管理页区分配置版本与实际版本。加入共享组的实例会共享 `config/plugins.yaml` 和插件文件，各实例运行状态独立。

ZIP 根目录放 `plugin.json`，示例：

```json
{
  "id": "my-plugin",
  "name": "我的插件",
  "version": "1.0.0",
  "sdk_version": 1,
  "backend": "backend.py",
  "frontend": "frontend.js",
  "styles": ["style.css"],
  "processes": ["supervisor", "engine"],
  "priority": 100,
  "params_schema": []
}
```

`backend` 和 `frontend` 至少提供一个。可以声明 `assets` 供前端读取资源，也可以把含清单的整个目录放进实例 `config/plugins/<id>/`。后端入口支持相对导入包内模块。前端入口是预构建的单文件 ESM，导出 `activate(sdk)`；浏览器不直接编译 TSX，不支持未打包的裸模块导入或相对 JS 导入。

每个安装版本按整个包计算指纹并保留不可变副本。更新必须提交当前版本指纹；冲突会拒绝覆盖。旧请求持有旧版本直到结束，旧资源地址仍可供已加载页面使用。失败的候选版本不会替代正在运行的版本。

## 后端接口

`backend.py` 提供同步 `setup(api)`，可保留同步 `teardown(api)`。异步初始化和释放通过 `api.lifecycle.startup`、`api.lifecycle.shutdown` 登记。

| 接口 | 用途 |
| --- | --- |
| `api.routes.register(router, prefix=None, replace=False)` | 登记 FastAPI router，默认挂在 `/v1/plugins/<id>`；覆盖已有路径需显式声明 |
| `api.middleware.register(factory, global_scope=False)` | 登记 ASGI 中间件，默认只处理本插件路由 |
| `api.services.register(name, factory, replace=False, managed=True)` | 登记可替换服务；也可传 `instance=` |
| `api.host.service(name)` | 取得按当前请求版本解析的服务代理 |
| `api.tools.register(spec, handler, replace=False)` | 注册工具，handler 接收 `(arguments, ToolContext)` |
| `api.jobs.register(name, callback, interval=None)` | 登记后台任务；不传间隔时执行一次，传间隔时按完成后延迟重复执行 |
| `api.events.subscribe(topic, callback)` / `await api.events.emit(topic, payload)` | 订阅和发送宿主内的插件事件 |
| `api.lifecycle.startup(callback)` / `shutdown(callback)` | 启动和关闭回调，可以是异步函数 |

`api.host` 提供当前进程与工作区。Supervisor 宿主包含 `state`、`config_store`、`process_manager`、`eventlog`、`hooks` 及已登记业务服务。服务代理在同一请求中保持版本一致，停用后恢复下一层实现。受管服务的 `stop()`/`close()` 等待旧请求释放后才调用；需要暂停后台工作的服务可提供 `pause()` 和 `resume()`，暂停期间应继续支持已有请求访问。定时工作优先通过 `api.jobs` 登记，宿主会负责停止新任务和取消已有后台任务。

工具继续受调用节点的 `tool_access` 约束，可在 spec 中声明与脚本工具相同的 `guard` 使用现有策略审批适配器。替换已有真实工具必须指定 `replace=True`；`finish`、`reply`、调度等引擎控制协议名称不属于普通工具注册入口。

模型 `before_request`、`after_response`、`on_stream_event`、`on_request_end` 接口仍然有效，详见 [Python SDK](README.md)。节点、渠道等筛选控制这些请求/Hook 的触发范围；应用路由、服务和后台任务属于进程级贡献，不能用模型节点筛选当作它们的访问控制。

## 控制台接口

前端通过 `sdk.React` 使用宿主的 React，不能另带一份 React。注册默认是新增，替换已有 ID 需 `{replace: true}`；包装使用 `wrap(id, next => replacement)`。每项注册归属于当前插件和版本，停用时自动移除。

| 接口 | 用途 |
| --- | --- |
| `sdk.views.register` / `wrap` | 工作区完整视图：侧栏、标题、主区域、输入区、右栏 |
| `sdk.workspaces.register` / `wrap` | 工作区导航项 |
| `sdk.settings.register` / `wrap` | 设置页、导航元数据和右侧面板 |
| `sdk.slots.register` / `wrap` | 包装 `chat.header`、`chat.main`、`chat.composer` 等区域以及应用布局 |
| `sdk.commands.register` / `wrap` / `execute` | 包装或调用聊天发送、取消、会话管理和导航操作 |
| `sdk.state.*.getState/subscribe/use` | 访问并订阅宿主聊天、设置、客户端偏好和导航状态 |
| `sdk.http.request(path, init)` | 携带当前认证和实例前缀，访问 `/v1` 下的接口 |
| `sdk.requests.wrap` | 包装公共 HTTP 请求处理链 |
| `sdk.assets.fetch/url` | 读取清单声明的版本资源 |
| `sdk.styles.add` | 添加可随插件卸载移除的样式 |
| `sdk.events.listen`、`sdk.timers.*`、`sdk.lifecycle.onDispose` | 登记需要随插件释放的监听器、计时器和清理回调 |

JS/CSS 使用认证请求获取并通过 WebCrypto 核对摘要，控制台需要运行在 HTTPS 或 localhost 安全上下文中。内联 CSS 的相对资源地址不会自动指向插件目录，应通过 `sdk.assets.url()` 取得资源 URL。插件启用失败时清理候选注册并保留旧版本，组件渲染错误会回退宿主实现。操作失败不会自动重做原操作，以免重复发送或重复写入。

## 示例与限制

[完整示例](../examples/plugins/system-workspace/README.md) 同时包含前后端：增加工作区、包装聊天标题和客户端设置、包装发送操作，提供计数接口、后台任务和工具。停止插件后，SDK 登记的页面、样式、接口、服务和工具会被撤销。

插件是受信应用代码，权限来自服务进程和当前控制台登录状态。进程筛选、注册归属及版本回退不是 Python/JavaScript 沙箱；直接改写源码、数据库或其他外部系统的副作用不能自动回滚。Supervisor 和 Engine 各有生命周期，不共享 Python 对象；前端与各进程的生效状态分别显示。自带网络访问的独立脚本工具需要主动接入 SDK，不能声称所有出站请求都会自动被模型中间件拦截。
