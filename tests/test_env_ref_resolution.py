"""四条 env 展开路径的一致性：节点 yaml、config.yaml provider、fallback 块、生图门控。

仓库自己的节点文件在用 $ENV{NEW|OLD} 回退写法，另外三条路径必须认同一套语法。
"""
from __future__ import annotations

import pytest

from clonoth_runtime import resolve_env_ref
from engine.builtin.fallback_provider import _resolve_env_value as fallback_resolve
from engine.builtin.image_gen_gating import _resolve_ref as gating_resolve
from supervisor.config_store import _resolve_env_value as config_resolve

# 四条路径的入口签名不同，但语义必须一致。gating 多一个 .env 兜底参数。
RESOLVERS = [
    pytest.param(resolve_env_ref, id="node_yaml"),
    pytest.param(config_resolve, id="config_provider"),
    pytest.param(fallback_resolve, id="fallback_block"),
    pytest.param(lambda v: gating_resolve(v, {}), id="image_gating"),
]


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_fallback_list_prefers_first_set_name(resolve, monkeypatch):
    monkeypatch.delenv("NEW_MODEL", raising=False)
    monkeypatch.setenv("OLD_MODEL", "legacy-model")

    assert resolve("$ENV{NEW_MODEL|OLD_MODEL}") == "legacy-model"

    monkeypatch.setenv("NEW_MODEL", "current-model")
    assert resolve("$ENV{NEW_MODEL|OLD_MODEL}") == "current-model"


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_empty_env_var_falls_through_to_next_name(resolve, monkeypatch):
    # 设了但是空串，等同于没设 —— 否则空值会挡住后面那个真正配了的名字。
    monkeypatch.setenv("NEW_MODEL", "   ")
    monkeypatch.setenv("OLD_MODEL", "legacy-model")

    assert resolve("$ENV{NEW_MODEL|OLD_MODEL}") == "legacy-model"


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_both_syntaxes_and_literal_passthrough(resolve, monkeypatch):
    monkeypatch.setenv("SOME_MODEL", "gpt-4o-mini")

    assert resolve("$ENV{SOME_MODEL}") == "gpt-4o-mini"
    assert resolve("${SOME_MODEL}") == "gpt-4o-mini"
    assert resolve("gpt-4o-mini") == "gpt-4o-mini"
    assert resolve("") == ""


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_unset_name_resolves_to_empty_not_the_template(resolve, monkeypatch):
    # 展开不出来就给空串：把 "${MISSING}" 原样送上游只会换来一个 400。
    monkeypatch.delenv("MISSING_MODEL", raising=False)

    assert resolve("$ENV{MISSING_MODEL}") == ""
    assert resolve("${MISSING_MODEL}") == ""


@pytest.mark.parametrize("resolve", RESOLVERS)
def test_crlf_residue_is_stripped(resolve, monkeypatch):
    # CRLF 仓库里 .env 解析出来的值常带尾随 \r，原样拼进 URL 或模型名就是一个坏请求。
    monkeypatch.setenv("DIRTY_MODEL", " gpt-4o-mini\r")

    assert resolve("$ENV{DIRTY_MODEL}") == "gpt-4o-mini"


def test_gating_falls_back_to_dotenv_when_process_env_missing(monkeypatch):
    # 生图门控在引擎加载 .env 之前就要判断"配没配"，所以它多一层文件兜底。
    monkeypatch.delenv("DRAW_MODEL", raising=False)

    assert gating_resolve("$ENV{DRAW_MODEL}", {"DRAW_MODEL": "sd-xl"}) == "sd-xl"
    assert gating_resolve("$ENV{ABSENT|DRAW_MODEL}", {"DRAW_MODEL": "sd-xl"}) == "sd-xl"


def test_gating_prefers_process_env_over_dotenv(monkeypatch):
    monkeypatch.setenv("DRAW_MODEL", "from-process")

    assert gating_resolve("${DRAW_MODEL}", {"DRAW_MODEL": "from-file"}) == "from-process"


def test_repo_node_syntax_resolves_through_config_store(monkeypatch):
    # config/nodes/draw.image_gen.yaml 里就是这个写法，照抄进 config.yaml 不能变成空。
    monkeypatch.delenv("DRAW_TAG_MODEL", raising=False)
    monkeypatch.setenv("DRAW_PLANNER_MODEL", "planner-model")

    assert config_resolve("$ENV{DRAW_TAG_MODEL|DRAW_PLANNER_MODEL}") == "planner-model"
