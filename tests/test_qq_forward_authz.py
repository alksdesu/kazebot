"""qq_forward 投递鉴权回归测试。

曾经全链路无鉴权：任意白名单群成员都能让 AI 用 op=file 把 data/config.yaml、
data/.admin_token、data/onebot_anon_map.json 私发出去，或借 Bot 身份向任意群发消息。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# NoneBot 不是核心测试环境的依赖，因此单文件加载纯逻辑模块而不 import 整个插件。
_MODULE_PATH = _ROOT / "adapters" / "onebot" / "forward_authz.py"
_SPEC = importlib.util.spec_from_file_location("_onebot_forward_authz", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

from _onebot_forward_authz import (  # type: ignore[import-not-found]  # noqa: E402
    CROSS_SESSION_DENIED,
    FILE_PATHS_DENIED,
    delivery_deny_reason,
    file_deny_reason,
    file_paths_deny_reason,
    has_explicit_file_paths,
    target_is_origin,
)

_ORIGIN_USER = 10001
_ORIGIN_GROUP = 20002
_OTHER_USER = 30003
_OTHER_GROUP = 40004


# --- 密钥外传黑名单：对管理员同样生效 ---


@pytest.mark.parametrize(
    "rel_path",
    [
        "data/config.yaml",
        "data/.admin_token",
        "data/policy.yaml",
        # 去匿名化那一类：别名双向表、conv 哈希↔真实群号的路由状态、真实 sender_id 的
        # 附件索引、热载配置里的真实群号与管理员号、以及它的生效快照。
        "data/onebot_anon_map.json",
        "data/onebot_plugin_state.json",
        "data/onebot_outbound_idempotency.sqlite3",
        "data/cache/onebot_reply_attachments.json",
        "config/qq.yaml",
        "data/qq_live_state.json",
        ".env",
        ".env.production",
        "adapters/onebot/.env",
        "deploy/secrets/server.key",
        "certs/fullchain.pem",
        "keystore/bundle.p12",
        "keystore/bundle.pfx",
        "id_rsa",
        "home/deploy/.ssh/id_rsa",
        # 配置端点覆盖写入前留的备份。副本的敏感度等于原文件，而按原文件名匹配的
        # 规则一条都盖不住多出来的后缀。
        "data/config.yaml.bak",
        "config/runtime.yaml.bak",
        "data/policy.yaml.bak",
        "config/nodes/qq.orchestrator.yaml.bak",
        "config/qq.yaml.bak",
    ],
)
def test_secret_paths_are_denied(rel_path: str) -> None:
    assert file_deny_reason(rel_path)


@pytest.mark.parametrize(
    "rel_path",
    [
        "README.md",
        "data/attachments/report.pdf",
        "data/attachments/naidiff_1.png",
        "config/runtime.yaml",
        "tools/qq_forward.py",
        "data/schedules.yaml",
    ],
)
def test_ordinary_paths_are_allowed(rel_path: str) -> None:
    assert file_deny_reason(rel_path) == ""


@pytest.mark.parametrize(
    "written",
    [
        "./data/config.yaml",
        ".\\data\\config.yaml",
        "data\\config.yaml",
        "  data/config.yaml  ",
        "././data/config.yaml",
    ],
)
def test_deny_survives_path_spelling_variants(written: str) -> None:
    """模型会写 ./前缀、反斜杠或带空白，不能因写法不同就绕过。"""
    assert file_deny_reason(written)


def test_empty_path_is_not_denied() -> None:
    assert file_deny_reason("") == ""
    assert file_deny_reason(None) == ""  # type: ignore[arg-type]


# --- 跨会话投递：只有管理员可用 ---


def test_non_admin_can_send_to_own_private() -> None:
    assert delivery_deny_reason(
        is_admin=False, target_type="private", target_id=_ORIGIN_USER,
        origin_user_id=_ORIGIN_USER, origin_group_id=_ORIGIN_GROUP,
    ) == ""


def test_non_admin_can_send_to_current_group() -> None:
    assert delivery_deny_reason(
        is_admin=False, target_type="group", target_id=_ORIGIN_GROUP,
        origin_user_id=_ORIGIN_USER, origin_group_id=_ORIGIN_GROUP,
    ) == ""


def test_non_admin_cannot_send_to_other_user() -> None:
    assert delivery_deny_reason(
        is_admin=False, target_type="private", target_id=_OTHER_USER,
        origin_user_id=_ORIGIN_USER, origin_group_id=_ORIGIN_GROUP,
    ) == CROSS_SESSION_DENIED


def test_non_admin_cannot_send_to_other_group() -> None:
    assert delivery_deny_reason(
        is_admin=False, target_type="group", target_id=_OTHER_GROUP,
        origin_user_id=_ORIGIN_USER, origin_group_id=_ORIGIN_GROUP,
    ) == CROSS_SESSION_DENIED


def test_admin_can_send_anywhere() -> None:
    assert delivery_deny_reason(
        is_admin=True, target_type="group", target_id=_OTHER_GROUP,
        origin_user_id=_ORIGIN_USER, origin_group_id=None,
    ) == ""


def test_unknown_origin_is_treated_as_non_admin() -> None:
    """旧持久化目标缺 user_id 时反查不到发起人，必须 fail-closed。"""
    assert delivery_deny_reason(
        is_admin=False, target_type="private", target_id=_ORIGIN_USER,
        origin_user_id=None, origin_group_id=None,
    ) == CROSS_SESSION_DENIED


def test_private_session_has_no_group_origin() -> None:
    """私聊会话里 origin_group_id 为 None，不能因此把任意群当成本群。"""
    assert delivery_deny_reason(
        is_admin=False, target_type="group", target_id=_OTHER_GROUP,
        origin_user_id=_ORIGIN_USER, origin_group_id=None,
    ) == CROSS_SESSION_DENIED


def test_explicit_group_ref_matching_current_group_is_allowed() -> None:
    """用户写“发到群 20002”而那正是本群，不该被误拒。"""
    assert target_is_origin(
        target_type="group", target_id=str(_ORIGIN_GROUP),
        origin_user_id=_ORIGIN_USER, origin_group_id=_ORIGIN_GROUP,
    )


def test_string_ids_compare_numerically() -> None:
    assert target_is_origin(
        target_type="private", target_id=str(_ORIGIN_USER),
        origin_user_id=str(_ORIGIN_USER), origin_group_id=None,
    )


def test_unparsable_target_id_is_denied() -> None:
    assert not target_is_origin(
        target_type="private", target_id="not-a-number",
        origin_user_id=_ORIGIN_USER, origin_group_id=None,
    )


def test_unknown_target_type_is_denied() -> None:
    assert not target_is_origin(
        target_type="channel", target_id=_ORIGIN_USER,
        origin_user_id=_ORIGIN_USER, origin_group_id=_ORIGIN_GROUP,
    )


# --- op=file 显式路径：只有管理员可用；use_recent 不受限 ---


def test_non_admin_cannot_use_explicit_file_paths() -> None:
    assert file_paths_deny_reason(
        is_admin=False, raw_file_paths=["README.md"],
    ) == FILE_PATHS_DENIED


def test_admin_can_use_explicit_file_paths() -> None:
    assert file_paths_deny_reason(is_admin=True, raw_file_paths=["README.md"]) == ""


@pytest.mark.parametrize("raw", [None, [], "", "   ", ["", "  "]])
def test_non_admin_without_explicit_paths_is_allowed(raw: object) -> None:
    """只用 use_recent 发 Bot 刚生成的图不算读取工作区，不需要管理员。"""
    assert file_paths_deny_reason(is_admin=False, raw_file_paths=raw) == ""


def test_single_string_path_counts_as_explicit() -> None:
    assert has_explicit_file_paths("data/report.md")
    assert file_paths_deny_reason(
        is_admin=False, raw_file_paths="data/report.md",
    ) == FILE_PATHS_DENIED
