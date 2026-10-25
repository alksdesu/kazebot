from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, get_args

from tools._wire import normalize_base_url


def image_part_url(part: Any) -> str:
    """取出一个 image_url 内容块的地址。

    内部表示里 image_url 一定是 {"url": ...}，但手写消息和外部导入的历史会直接写成字符串。
    在这里吃掉这个差异，三家的转换才不用各写一次 isinstance。
    """
    if not isinstance(part, dict):
        return ""
    value = part.get("image_url")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("url") or "").strip()
    return ""


def split_data_url(url: str) -> tuple[str, str] | None:
    """data:<mime>;base64,<data> → (mime, data)。不是这个形状返回 None。"""
    text = url or ""
    if not text.startswith("data:"):
        return None
    header, sep, payload = text.partition(",")
    if not sep or not payload:
        return None
    mime = header[len("data:"):].split(";", 1)[0].strip()
    return (mime or "application/octet-stream", payload)


def content_parts(content: Any) -> list[dict[str, Any]]:
    """消息 content 统一成块列表。

    手写消息和外部导入的历史里会出现裸字符串块，甚至整条 content 就是一个块；
    在这里一并折成 {"type": "text"} 块，各家的转换只需要认块。
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, dict):
        return [content]
    if not isinstance(content, (list, tuple)):
        return [{"type": "text", "text": str(content)}]
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, dict):
            parts.append(part)
        elif isinstance(part, str):
            if part:
                parts.append({"type": "text", "text": part})
        elif part is not None:
            parts.append({"type": "text", "text": str(part)})
    return parts


def content_text(content: Any) -> str:
    """content 里全部文本块按顺序拼起来的正文。"""
    return "".join(
        str(part.get("text") or "")
        for part in content_parts(content)
        if part.get("type") == "text"
    )


def provider_metadata(message: Any, provider: str) -> dict[str, Any]:
    """engine 存在消息 _meta.metadata.<provider> 下、要原样回传给这一家的数据。取不到返回空 dict。"""
    meta = message.get("_meta") if isinstance(message, dict) else None
    metadata = meta.get("metadata") if isinstance(meta, dict) else None
    value = metadata.get(provider) if isinstance(metadata, dict) else None
    return value if isinstance(value, dict) else {}


def tool_call_fields(call: Any) -> tuple[str, str, Any] | None:
    """历史里一次工具调用的 (id, 名字, 原样参数)。

    chat/completions 的 {"id", "function": {"name", "arguments": "<json>"}} 和
    本项目存储用的 {"id", "name", "arguments": {...}} 两种形状都认；没有名字的返回 None。
    """
    if not isinstance(call, dict):
        return None
    function = call.get("function")
    if isinstance(function, dict):
        name, arguments = function.get("name"), function.get("arguments")
    else:
        name, arguments = call.get("name"), call.get("arguments")
    name = str(name or "").strip()
    if not name:
        return None
    return str(call.get("id") or ""), name, arguments


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "refusal", "incomplete"]
ErrorKind = Literal[
    "rate_limit", "overloaded", "timeout", "network", "server", "auth", "bad_request",
    "context_length", "content_filter", "invalid_response", "incomplete", "unknown",
]
FINISH_REASONS: frozenset[str] = frozenset(get_args(FinishReason))
ERROR_KINDS: frozenset[str] = frozenset(get_args(ErrorKind))


@dataclass
class ProviderResponse:
    """A rust-like provider result.

    - ok=True  -> text/tool_calls are valid
    - ok=False -> error contains a human-readable error message, error_kind says what failed

    Worker loops should NOT rely on exceptions for normal provider failures
    (timeout, 4xx/5xx, invalid payload...).
    """

    ok: bool
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str | None = None
    error: str | None = None
    # 收到了上游答复时是它的 HTTP 状态码；连接失败、流半路断掉这类没有完整答复的情况为 None。
    status_code: int | None = None
    raw: dict[str, Any] | None = None
    usage: dict[str, int] | None = None  # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    inline_data: list[dict] = field(default_factory=list)
    # provider 私有元数据，engine 只搬运不解读。
    provider_meta: dict[str, Any] = field(default_factory=dict)
    # 取值见 FinishReason。ok=True 且为 "length" 表示内容被输出上限截断，是否采用由 engine 决定。
    finish_reason: str | None = None
    # ok=False 时必填，取值见 ErrorKind；engine 据此决定重试、换渠道还是压缩上下文。
    error_kind: str | None = None


class BaseProvider(ABC):
    """所有 provider 的公共约定。

    子类的 chat / chat_stream 在定义时会被自动包一层兜底：除了取消和插件异常，任何异常都变成
    ok=False 的响应，所以新写的 provider 不必、也无法绕开「失败不抛异常」这条约定。
    """

    # 自动发现按它注册，渠道配置里的 provider 字段写的就是它；实例名也默认取它，下游按 provider.name 判断各家特性。
    provider_name: str = ""

    # 请求格式族。同族之间只换地址不会挂 —— DeepSeek 继承 OpenAIProvider 就是这个意思。
    # 留空回落 provider_name。子类不声明就继承父类的，这里正需要这个行为。
    wire_format: str = ""

    # 这家的官方域名，控制台照它提示「地址和渠道对不上」。
    # 只认自己声明的那份、不继承：DeepSeek 继承到 api.openai.com 会指鹿为马。
    official_hosts: tuple[str, ...] = ()

    # 这家的模型默认收不收图片。渠道块里显式写了 supports_vision 就以那个为准。
    # 带图消息按它决定走主模型还是绕去视觉节点，猜错只是绕一趟，不至于出错。
    default_supports_vision: bool = True

    # 这家默认的接口地址。渠道块留空时用它，列模型时也用它。
    default_base_url: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        from .failures import never_raises

        for name in ("chat", "chat_stream"):
            method = cls.__dict__.get(name)
            if callable(method):
                setattr(cls, name, never_raises(method))

    @classmethod
    def resolve_base_url(cls, base_url: str | None) -> str:
        """渠道地址归一成这家请求格式的根。工具子进程用的是同一个函数，两边才说得到一处去。"""
        wire = (cls.wire_format or cls.provider_name or "").strip().lower()
        return normalize_base_url(wire, (base_url or "").strip() or cls.default_base_url)

    @classmethod
    def catalog_request(
        cls, *, base_url: str, api_key: str,
    ) -> tuple[str, dict[str, str]] | None:
        """列模型请求的 (url, headers)。None 表示这家没有模型列表接口。

        是类方法而不是实例方法：控制台在渠道还没保存时就要用页面上刚填的
        地址和密钥去问，那时候没有实例。
        """
        return None

    @staticmethod
    def parse_catalog(payload: Any) -> list[str]:
        """把列模型响应解析成模型名。形状不认识就返回空。"""
        return []

    def __init__(self, *, model: str, name: str = ""):
        self.model = model
        self.name = name or self.provider_name

    @abstractmethod
    async def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ProviderResponse:
        raise NotImplementedError

    @abstractmethod
    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        """流式请求。on_tool_delta 收工具调用参数的增量，前端靠它实时预览正在写的调用。"""
        raise NotImplementedError
