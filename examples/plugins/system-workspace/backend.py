from fastapi import APIRouter


class Counter:
    def __init__(self):
        self.ticks = 0
        self.last_event = "started"

    def snapshot(self):
        return {"ticks": self.ticks, "last_event": self.last_event, "process": "supervisor"}


def setup(api):
    if api.host.process == "engine":
        async def echo(args, ctx):
            return {"ok": True, "data": {"result": str(args.get("text", ""))}}

        api.tools.register({
            "name": "system_plugin_echo",
            "description": "返回输入文字，用于验证插件工具调用。",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        }, echo)
        return

    counter = Counter()
    api.services.register("system-workspace.counter", instance=counter, managed=False)
    router = APIRouter()

    @router.get("/status")
    async def status():
        return api.host.service("system-workspace.counter").snapshot()

    @router.post("/reset")
    async def reset():
        service = api.host.service("system-workspace.counter")
        service.ticks = 0
        service.last_event = "reset"
        return service.snapshot()

    async def tick(_api):
        service = _api.host.service("system-workspace.counter")
        service.ticks += 1
        service.last_event = "tick"

    api.routes.register(router)
    api.jobs.register("counter", tick, interval=float(api.config.get("interval", 5)))
