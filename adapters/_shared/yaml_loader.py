"""手写 yaml 的解析。

PyYAML 按 YAML 1.1 认布尔，写在关键词表里的 on/off/yes/no 到不了消费方就成了 True/False。
这里只认 YAML 1.2 的 true/false，其余一律按字符串读。
"""
from __future__ import annotations

import re
from typing import Any

import yaml

_BOOL_TAG = "tag:yaml.org,2002:bool"


class _Loader(yaml.SafeLoader):
    # 表必须先于 add_implicit_resolver 建好：它发现子类自己没有这张表，会从父类整份
    # 复制一份带 on/off 的，把这里摘掉的布尔别名又带回来。
    yaml_implicit_resolvers = {
        ch: [(tag, pattern) for tag, pattern in pairs if tag != _BOOL_TAG]
        for ch, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_Loader.add_implicit_resolver(
    _BOOL_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_yaml(text: str) -> Any:
    """解析一份手写 yaml。语法错误照常抛 yaml.YAMLError，由调用方决定怎么兜。"""
    return yaml.load(text, Loader=_Loader)
