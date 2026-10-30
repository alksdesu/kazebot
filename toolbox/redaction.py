"""认密钥、打码：日志、诊断导出、插件诊断、审批预览和调试工具的输出共用这一套。

只认得出带前缀的密钥、挂在敏感键名下的值和地址里的账号密码，认不出的照原样，是兜底而不是放心外传原文的理由。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from enum import IntEnum
from typing import Any

_KEEP_TAIL = 4
# 短于这个长度的整段打码：留下的尾巴就是值的一大截。
_TAIL_MIN_CHARS = 16
_STARS = "******"

# 各家密钥的前缀。命中就整段打码。
_TOKEN_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"\b(AIza[A-Za-z0-9_\-]{10,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{10,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"),
)

# 底下的数字也当密钥藏的键名：口令常是纯数字，YAML 里读出来就是 int。
_NUMERIC_SECRET_KEY_NAMES = r"api.?key|secret|password|passwd|authorization|bearer|cookies?|credentials?"
# 认得出是密钥的键名，各处经 hide_level 判的都是这同一张表。token 不进上一档：它底下的数字多是 max_tokens 这类上限。
SENSITIVE_KEY_NAMES = rf"{_NUMERIC_SECRET_KEY_NAMES}|token"
SENSITIVE_KEY = re.compile(SENSITIVE_KEY_NAMES, re.IGNORECASE)
NUMERIC_SECRET_KEY = re.compile(_NUMERIC_SECRET_KEY_NAMES, re.IGNORECASE)

# 诊断导出按变量名收进程环境和共享密钥文件里的值时只看结尾：名字中间带 TOKEN 的多是上限和开关。
SECRET_ENV_NAME = re.compile(r"(?:TOKEN|SECRET|PASSWORD|API_?KEY)$", re.IGNORECASE)

# 讲密钥规则的键：末词是长度、次数、上下限、时限、策略，或者首词是 min、max。只看这两处，password_history 底下放的还是口令。
_RULE_LAST_WORDS = frozenset({
    "length", "len", "minlength", "maxlength", "chars", "characters", "digits", "size", "bits",
    "count", "limit", "limits", "min", "max", "minimum", "maximum",
    "attempts", "retries", "tries", "lockout",
    "ttl", "timeout", "expiry", "expiration", "expire", "expires", "lifetime", "age", "interval", "period",
    "days", "hours", "minutes", "seconds", "secs", "sec", "ms",
    "rotation", "rotate", "policy", "policies", "rule", "rules", "strength", "complexity", "required", "threshold",
})
_RULE_FIRST_WORDS = frozenset({"min", "max", "minimum", "maximum"})
_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

# 按值替换时，短于这个长度的只换独立成词的，token 档下这么短的数字也不当密钥：按子串换会把不相干的字抹掉。
MIN_SECRET_CHARS = 6

# 整个值藏起来时换上的字样。
HIDDEN = "[已隐藏]"

# .env 的值全是字符串，qq.yaml 也把 on/off/yes/no 留成文字：挂在密钥键下的开关当密钥收，按值替换会抹掉每个独立的 true、on。
_SWITCH_WORDS = frozenset({"true", "false", "yes", "no", "on", "off"})

# 打过码的样子，再过一遍时原样留着：不然留下的尾巴会被当成值再打一次。
_MASKED = re.compile(rf"\*+|\*{{6}}[^\s*]{{1,{_KEEP_TAIL}}}|{re.escape(HIDDEN)}")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
# 值以方案词打头时，后面的令牌由 _AUTH_SCHEME_PATTERN 打码，方案词本身照留。
_SCHEME_WORD = re.compile(r"(?:bearer|basic)\b", re.IGNORECASE)
_SCHEME_PREFIX = re.compile(r"(?:bearer|basic)\s+", re.IGNORECASE)

# 键值对写法。键名认得出就不管值长什么样 —— 中转站的 key 可以是任意串。
# 键须以敏感词结尾（prompt_tokens= 这类用量不算）；键和值可以带引号或转义的引号，引号里的值连空格一起算。
_ASSIGN_PATTERN = re.compile(
    rf"(?P<word>{SENSITIVE_KEY_NAMES})(?:\\?['\"])?[ \t]*[=:][ \t]*"
    r"(?:\\?(?P<quote>['\"])(?P<quoted>(?:(?!\\?(?P=quote))[^\r\n])+)\\?(?P=quote)"
    r"|(?:\\?['\"])?(?P<bare>[^\s'\",;)]+))",
    re.IGNORECASE,
)

# URL 里带的密钥，Gemini 那套 ?key= 就是这么传的。
_QUERY_PATTERN = re.compile(
    rf"([?&](?:key|api[_-]?key|access[_-]?token)=)([^\s&'\"]{{{MIN_SECRET_CHARS},}})",
    re.IGNORECASE,
)

_AUTH_SCHEME_PATTERN = re.compile(r"\b((?:Bearer|Basic)\s+)([^\s'\",;]+)", re.IGNORECASE)

# 地址里的 user:pass@ 整段藏掉，不留尾巴：留下的就是密码的最后几位。
_USERINFO_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@")


class Hide(IntEnum):
    """藏到哪一档。往下走时经过敏感键只升不降。"""

    NOTHING = 0
    TEXT = 1
    TEXT_AND_NUMBERS = 2


def _names_a_rule(name: str) -> bool:
    words = [word.lower() for word in _KEY_WORD.findall(name)]
    return bool(words) and (words[-1] in _RULE_LAST_WORDS or words[0] in _RULE_FIRST_WORDS)


def hide_level(key: Any, *, also: re.Pattern[str] | None = None) -> Hide:
    """键名本身要求藏到哪一档。讲规则的键（password_min_length、max_tokens）不算密钥；also 是调用方额外认的键名，按 token 那一档算。"""
    name = str(key)
    if NUMERIC_SECRET_KEY.search(name):
        level = Hide.TEXT_AND_NUMBERS
    elif SENSITIVE_KEY.search(name) or (also is not None and also.search(name)):
        level = Hide.TEXT
    else:
        return Hide.NOTHING
    return Hide.NOTHING if _names_a_rule(name) else level


def mask(value: str) -> str:
    """打码后的样子：16 个字符及以上留尾 4 位供人对号，更短的整段换成星号，不露尾巴也不露长度。"""
    if len(value) < _TAIL_MIN_CHARS:
        return _STARS
    return _STARS + value[-_KEEP_TAIL:]


def _masked_once(value: str) -> str:
    return value if _MASKED.fullmatch(value) else mask(value)


def is_switch_text(value: str) -> bool:
    """整个值（去掉首尾空白，不分大小写）是 true、false、yes、no、on、off：文字写的开关，不是密钥。"""
    return value.strip().lower() in _SWITCH_WORDS


def counts_token_usage(key: Any) -> bool:
    """键名的词里有复数 tokens（prompt_tokens、budget_tokens、maxOutputTokens）：底下的数字是 token 用量，不是密钥。"""
    return any(word.lower() == "tokens" for word in _KEY_WORD.findall(str(key)))


def counts_as_secret(value: Any, level: Hide, *, usage: bool = False) -> bool:
    """挂在 level 这一档的键下，value 算不算密钥。打码和按值收密钥都按它判，字符串 "12" 和数字 12 结论一样。

    空值、布尔和文字开关不算；token 档短于 MIN_SECRET_CHARS 位的纯数字不算，多是上限和编号；口令档不设最短长度。
    usage 为真表示值挂在数 token 的键下（见 counts_token_usage）：token 档的数字多长都算用量，口令档照藏，字符串照旧。
    """
    if level < Hide.TEXT or value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        return False
    if not text or is_switch_text(text):
        return False
    if level == Hide.TEXT and _NUMBER.fullmatch(text) is not None:
        return not usage and len(text) >= MIN_SECRET_CHARS
    return True


def _usage_below(key: Any, own: Hide, inherited: bool) -> bool:
    """key 底下的数字算不算 token 用量：键名数 token 的算；自己认不出是密钥的键接着上层的算，自己是密钥键的不算。"""
    return counts_token_usage(key) or (inherited and own == Hide.NOTHING)


def hides_entry(key: Any, value: Any) -> bool:
    """按项藏的地方（诊断导出、进度摘要）判 key: value 这一项整个藏不藏，口径和 mask_sensitive 一样。

    键名认不出是密钥的不藏；密钥键下的 dict、list 整项藏；标量按 counts_as_secret 判，空值、布尔、开关、token 档的短数字和用量照给。
    """
    level = hide_level(key)
    if level == Hide.NOTHING:
        return False
    if value is None or isinstance(value, (bool, int, float, str)):
        return counts_as_secret(value, level, usage=counts_token_usage(key))
    return True


def _key_before(text: str, end: int) -> str:
    """紧挨在敏感词前面、同属一个键名的那段。"""
    start = end
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_-."):
        start -= 1
    return text[start:end]


def _assigned_secret(match: re.Match[str]) -> str | None:
    """这一处「键=值」里要打码的值。打过码的、方案词打头的给 None，其余按键名的档位过 counts_as_secret。"""
    value = match.group("quoted") if match.group("quoted") is not None else match.group("bare")
    if _MASKED.fullmatch(value) or _SCHEME_WORD.match(value):
        return None
    key = _key_before(match.string, match.start("word")) + match.group("word")
    return value if counts_as_secret(value, hide_level(key), usage=counts_token_usage(key)) else None


def _mask_assignment(match: re.Match[str]) -> str:
    secret = _assigned_secret(match)
    if secret is None:
        return match.group(0)
    group = "quoted" if match.group("quoted") is not None else "bare"
    start, end = match.start(group) - match.start(), match.end(group) - match.start()
    whole = match.group(0)
    return whole[:start] + mask(secret) + whole[end:]


def redact(text: str) -> str:
    """把看得出是密钥的东西打码。宁可多打，看日志的人不需要那几位。打过码的再过一遍不变。"""
    text = _USERINFO_PATTERN.sub(lambda match: match.group(1) + HIDDEN + "@", text)
    text = _AUTH_SCHEME_PATTERN.sub(lambda match: match.group(1) + _masked_once(match.group(2)), text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda match: mask(match.group(1)), text)
    text = _ASSIGN_PATTERN.sub(_mask_assignment, text)
    text = _QUERY_PATTERN.sub(lambda match: match.group(1) + _masked_once(match.group(2)), text)
    return text


def assigned_values(text: str) -> list[str]:
    """按「敏感键名=值」认出来、redact 会打码的值里够 MIN_SECRET_CHARS 长的，按出现顺序。

    这份拿去按值替换：写坏的文件按行扫出来的短值多半是没写完的记号，不收。
    """
    values: list[str] = []
    for match in _ASSIGN_PATTERN.finditer(text):
        secret = _assigned_secret(match)
        if secret is not None and len(secret) >= MIN_SECRET_CHARS:
            values.append(secret)
    return values


class ValueHider:
    """把一组值换成 HIDDEN。同一组值要换很多段文字时先建好它，正则只编译一次。

    够 MIN_SECRET_CHARS 长的按子串换，更短的只换独立成词的；长的先换，一个值是另一个的一截时不会只换掉半个。
    """

    def __init__(self, values: Iterable[str]) -> None:
        ordered = sorted({str(value) for value in values if value}, key=len, reverse=True)
        parts = [
            re.escape(value) if len(value) >= MIN_SECRET_CHARS else rf"(?<![\w-]){re.escape(value)}(?![\w-])"
            for value in ordered
        ]
        self._pattern = re.compile("|".join(parts)) if parts else None

    def __call__(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(lambda _match: HIDDEN, text)


def hide_values(text: str, values: Iterable[str]) -> str:
    """text 里出现的这些值换成 HIDDEN，规则同 ValueHider。"""
    return ValueHider(values)(text)


def secret_values(
    data: Any,
    *,
    also: re.Pattern[str] | None = None,
    resolve: Callable[[str], str] | None = None,
) -> set[str]:
    """data 里挂在密钥键名下的值，拿去按值打码用。档位和 mask_sensitive 一样往下只升不降。

    字符串和数字一个口径，按 counts_as_secret 判：文字开关不收，token 档短于 MIN_SECRET_CHARS 位的纯数字不收，
    数 token 的键（prompt_tokens 这类）底下 token 档的数字不收，口令档不设最短长度。
    resolve 给了就按展开后的值判、收展开后的值。写成「Bearer xxx」的另收后面那段：日志和报错里常只有令牌本身。
    also 是这一处额外当密钥认的键名。
    """
    found: set[str] = set()
    _collect_secrets(data, Hide.NOTHING, False, found, also, resolve)
    found.discard("")
    return found


def _collect_secrets(
    value: Any,
    hide: Hide,
    usage: bool,
    found: set[str],
    also: re.Pattern[str] | None,
    resolve: Callable[[str], str] | None,
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            own = hide_level(key, also=also)
            _collect_secrets(item, max(hide, own), _usage_below(key, own, usage), found, also, resolve)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_secrets(item, hide, usage, found, also, resolve)
    elif isinstance(value, str):
        if hide < Hide.TEXT:
            return
        text = (resolve(value) if resolve is not None else value).strip()
        if not counts_as_secret(text, hide, usage=usage):
            return
        found.add(text)
        scheme = _SCHEME_PREFIX.match(text)
        if scheme is not None and counts_as_secret(text[scheme.end():], hide, usage=usage):
            found.add(text[scheme.end():])
    elif isinstance(value, (int, float)) and counts_as_secret(value, hide, usage=usage):
        found.add(str(value))


def mask_sensitive(value: Any) -> Any:
    """value 的副本：键名认得出是密钥的那一支里，按 counts_as_secret 算密钥的值换成 HIDDEN，别处的字符串过 redact。

    口令、secret、api key、authorization、bearer、cookie、credential 那一支里的数字也换掉：口令常是纯数字，YAML 读出来是 int。
    键名只认得出 token 的那一支，短于 MIN_SECRET_CHARS 位的数字照给，写成字符串的也一样，多是上限和编号；
    prompt_tokens 这类数 token 的键底下的数字多长都照给，是用量。讲长度、次数、上下限、时限、策略的键自己不算密钥，
    上层已经要藏的照藏。键和层次不变，空值、布尔和 true、on 这类文字开关照原样，看的人分得清配没配、配在哪；
    元组和集合变成列表，原对象不动。
    """
    return _masked(value, Hide.NOTHING, False)


def _masked(value: Any, hide: Hide, usage: bool) -> Any:
    if isinstance(value, Mapping):
        masked: dict[Any, Any] = {}
        for key, item in value.items():
            own = hide_level(key)
            masked[key] = _masked(item, max(hide, own), _usage_below(key, own, usage))
        return masked
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_masked(item, hide, usage) for item in value]
    if isinstance(value, str):
        return HIDDEN if counts_as_secret(value, hide, usage=usage) else redact(value)
    if isinstance(value, (bytes, bytearray)):
        return HIDDEN if hide >= Hide.TEXT and value else value
    if isinstance(value, (int, float)):
        return HIDDEN if counts_as_secret(value, hide, usage=usage) else value
    return value
