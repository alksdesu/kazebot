"""图片处理提示的注入与附件路径回填：群聊和私聊共用同一份实现。

这段提示是防幻觉的安全提示：拿不到可确认的图片时禁止模型从历史里猜。它一旦分成两份，
改一处漏一处就等于单边失去防护，而失效在日志里看不出来 —— 模型只是开始编图片内容。
所以除了行为断言，还有一条 ast 静态断言钉住"整个模块里只有一份"。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime  # noqa: E402

_ADAPTER_SOURCE = _ROOT / "adapters" / "onebot" / "__init__.py"
_HINT_MARK = "不得从聊天历史猜测图片内容"


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


def _attachment(path: str, **extra: Any) -> dict[str, Any]:
    return {"type": "image", "path": path, "name": Path(path).name, "mime": "image/jpeg", **extra}


class TestTheNoGuessingHint:
    def test_a_bare_image_question_gets_the_no_guessing_hint(self, runtime) -> None:
        result = runtime._apply_attachment_hints("【当前用户指令】这张图里是啥", "这张图里是啥", [], [])

        assert _HINT_MARK in result

    def test_an_image_question_with_an_attachment_gets_no_hint(self, runtime) -> None:
        result = runtime._apply_attachment_hints(
            f"【当前用户指令】这张图里是啥{runtime.IMAGE_PLACEHOLDER}",
            "这张图里是啥",
            [_attachment("out/a.jpg")],
            [],
        )

        assert "【图片处理提示】" not in result

    def test_a_plain_question_gets_no_hint(self, runtime) -> None:
        result = runtime._apply_attachment_hints("【当前用户指令】今天天气如何", "今天天气如何", [], [])

        assert result == "【当前用户指令】今天天气如何"

    def test_errors_are_deduplicated(self, runtime) -> None:
        result = runtime._apply_attachment_hints("正文", "你好", [], ["下载失败", "下载失败"])

        assert result.count("下载失败") == 1

    def test_a_download_error_still_reaches_the_model_without_an_image_question(self, runtime) -> None:
        result = runtime._apply_attachment_hints("正文", "你好", [], ["下载失败"])

        assert "【图片处理提示】" in result
        assert _HINT_MARK not in result


class TestPlaceholderBackfill:
    def test_each_attachment_consumes_one_placeholder(self, runtime) -> None:
        placeholder = runtime.IMAGE_PLACEHOLDER
        result = runtime._apply_attachment_hints(
            f"看这两张{placeholder}和{placeholder}",
            "看这两张图片",
            [_attachment("out/a.jpg"), _attachment("out/b.png")],
            [],
        )

        assert result == "看这两张[图片: out/a.jpg]和[图片: out/b.png]"
        assert placeholder not in result.replace("[图片: ", "")

    def test_a_spare_attachment_leaves_the_text_alone(self, runtime) -> None:
        result = runtime._apply_attachment_hints(
            f"只有一个{runtime.IMAGE_PLACEHOLDER}",
            "看图",
            [_attachment("out/a.jpg"), _attachment("out/b.png")],
            [],
        )

        assert result == "只有一个[图片: out/a.jpg]"


class TestOnlyOneImplementation:
    """ast 静态断言：提示文案和回填只能有一份实现，群聊/私聊各调一次。"""

    def _module(self) -> ast.Module:
        return ast.parse(_ADAPTER_SOURCE.read_text(encoding="utf-8"))

    def test_the_hint_text_appears_exactly_once_in_the_module(self) -> None:
        literals = [
            node for node in ast.walk(self._module())
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _HINT_MARK in node.value
        ]

        assert len(literals) == 1

    def test_group_and_private_share_one_implementation(self) -> None:
        module = self._module()
        definitions = [
            node for node in ast.walk(module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_apply_attachment_hints"
        ]
        calls = [
            node for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_apply_attachment_hints"
        ]

        assert len(definitions) == 1
        assert len(calls) == 2

    def test_the_backfill_label_is_never_hardcoded(self) -> None:
        # 写死「[图片: 」的地方就是把表情包当照片报给模型的地方。
        hardcoded = [
            node for node in ast.walk(self._module())
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("[图片: ")
        ]

        assert hardcoded == []

    def test_every_backfill_goes_through_the_label_helper(self) -> None:
        owners: dict[str, int] = {}
        for top_level in self._module().body:
            if not isinstance(top_level, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            count = sum(
                1 for node in ast.walk(top_level)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_attachment_label"
            )
            if count:
                owners[top_level.name] = count

        # 引用块用引用消息自己的附件回填，和 inbound 层不是同一件事，所以它那两处不并进 helper。
        assert owners == {"_apply_attachment_hints": 1, "_build_reply_context": 2}
