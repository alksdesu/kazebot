"""dispatch 只能派给 delegate_targets 里的节点。

给模型看的 dispatch 工具由 node.delegate_targets 展开（ai_step 里那段），但执行时
目标是从工具名反查的，反查表 _DISPATCH_TOOL_REVERSE 是模块级全局字典，进程内
任何节点注册过的目标都查得到，查不到还会原样退回 sanitize 后的字符串。

于是白名单只管住了「模型看得见什么」：编一个 dispatch_to_bootstrap_executor 就能
把任务甩给委派清单外的节点，连带用上那个节点的全部工具，而 platform_auth 是原样
透传的，身份不会被降级。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.inference import pseudo_handlers  # noqa: E402


class _Node:
    id = "qq.orchestrator"


class _Call:
    name = "dispatch_to_bootstrap_executor"
    id = "call-1"


class _LS:
    """只带住校验要用的那几个字段。真 _LoopState 的其余部分与这条路径无关。"""

    def __init__(self, allowed: set[str]) -> None:
        self.node = _Node()
        self.allowed_dispatch_targets = allowed
        self.emitted: list[str] = []


@pytest.fixture(autouse=True)
def _capture(monkeypatch: pytest.MonkeyPatch) -> None:
    def _emit(ls, pseudo_call, content, **kwargs):  # noqa: ANN001
        ls.emitted.append(content)

    monkeypatch.setattr(pseudo_handlers, "_emit_pseudo_tool_result", _emit)


@pytest.mark.asyncio
async def test_target_outside_delegate_targets_is_refused() -> None:
    ls = _LS({"draw.image_gen"})

    out = await pseudo_handlers._handle_pseudo_dispatch(
        ls, {"target": "bootstrap.executor", "instruction": "rm -rf"}, _Call(),
    )

    assert out is None
    assert len(ls.emitted) == 1
    said = json.loads(ls.emitted[0])
    assert said["success"] is False
    assert "delegate_targets" in said["error"]


@pytest.mark.asyncio
async def test_refusal_names_the_allowed_targets() -> None:
    # 光说不行没用，模型得知道它能派给谁，否则只会换个名字再试一次。
    ls = _LS({"draw.image_gen", "draw.novelai_planner"})

    await pseudo_handlers._handle_pseudo_dispatch(ls, {"target": "system.dream"}, _Call())

    said = json.loads(ls.emitted[0])
    assert "draw.image_gen" in said["error"]
    assert "draw.novelai_planner" in said["error"]


@pytest.mark.asyncio
async def test_empty_delegate_targets_refuses_everything() -> None:
    ls = _LS(set())

    await pseudo_handlers._handle_pseudo_dispatch(ls, {"target": "anything"}, _Call())

    said = json.loads(ls.emitted[0])
    assert said["success"] is False


@pytest.mark.asyncio
async def test_allowed_target_passes_the_check() -> None:
    """放行的那条路要往下走。这里只验校验不误伤——真正的派发要连 supervisor，不在本测试范围。"""
    ls = _LS({"draw.image_gen"})

    with pytest.raises(AttributeError):
        # 过了校验之后立刻访问 ls.rctx，而这个替身没有它。
        # 换句话说：没有被那条拒绝分支拦下来。
        await pseudo_handlers._handle_pseudo_dispatch(ls, {"target": "draw.image_gen"}, _Call())

    assert ls.emitted == []
