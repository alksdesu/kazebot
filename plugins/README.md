# 插件目录

`plugins/` 保存随项目发布的插件，默认启用，源码在控制台只读。自定义插件放在 `config/plugins/<id>.py`，上传后默认停用；同名自定义文件会覆盖项目插件。控制台的「插件」设置页负责上传、配置、启停、编辑源码和查看各进程的生效状态。

启用状态、参数和作用域保存在 `config/plugins.yaml`，共享配置组会共同使用它。实例运行状态单独写入 `data/plugin_runtime/`。保存后各进程自动应用；已发出的请求保留原插件版本直到结束。

新插件使用 `setup(api)` 注册请求、响应、SSE 事件和结束回调，可提供 `teardown(api)` 释放资源。旧版 `register(registry)` 与 `PLUGIN_META.handler_class/hook_points` 插件仍可加载。插件顶层代码只在启用且当前进程符合 `PLUGIN_META.processes` 时执行；加载失败会回滚该插件的注册，并在控制台显示错误。

接口、生命周期、作用域和可运行的参数示例见 [Python plugin SDK](../plugin_sdk/README.md)。
