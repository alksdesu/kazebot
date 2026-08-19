"""用户那句话不得在 prompt 里出现两次。

节点提示词里嵌 {{instruction}}，而 message_assembly 又把同一段文本作为末尾 user
消息追加，去重只看 history[-1] 所以拦不到。QQ 群链路的 instruction 本身还含 20 行
群聊记录，重复一次就是每请求数千 token。

顺带钉住缓存前缀：{{now}} 这类每轮变化的变量必须落在 # %%DYNAMIC%% 之后，
否则系统提示词（含约 16k 字符的工具定义）每次请求前缀都不同。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.inference.message_assembly import assemble_messages_with_injections  # noqa: E402
from engine.node import Node  # noqa: E402
from engine.prompt import assemble_prompt  # noqa: E402

MARKER = "# %%DYNAMIC%%"
_NODE_DIRS = ("config/nodes", "engine/system_nodes")


def _node_files() -> list[Path]:
    files: list[Path] = []
    for rel in _NODE_DIRS:
        files.extend(sorted((_ROOT / rel).glob("*.yaml")))
    return files


def _prompt_of(path: Path) -> str:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    prompt = data.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def _rel(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


class TestNodePromptsDoNotEchoTheInstruction:
    def test_no_node_prompt_embeds_the_instruction_variable(self) -> None:
        hits = [_rel(p) for p in _node_files() if "{{instruction}}" in _prompt_of(p)]

        assert hits == [], (
            "instruction 已经作为末尾 user 消息追加，提示词里再嵌一次就是整段重复: "
            f"{hits}"
        )

    def test_turn_varying_variables_stay_behind_the_marker(self) -> None:
        hits: list[str] = []
        for path in _node_files():
            prompt = _prompt_of(path)
            if "{{now}}" not in prompt:
                continue
            marker_at = prompt.find(MARKER)
            if marker_at < 0 or marker_at > prompt.find("{{now}}"):
                hits.append(_rel(path))

        assert hits == [], f"{{{{now}}}} 落在静态前缀里会让工具定义每轮都缓存 miss: {hits}"

    def test_every_node_prompt_still_parses_and_is_non_empty(self) -> None:
        empty = [_rel(p) for p in _node_files() if not _prompt_of(p).strip()]

        assert empty == [], f"删引导句时把整个 prompt 清空了: {empty}"

    @pytest.mark.parametrize("rel", [
        "config/nodes/qq.orchestrator.yaml",
        "config/nodes/qq.vision.yaml",
        "config/nodes/qq.web_search.yaml",
    ])
    def test_qq_prompts_carry_no_turn_varying_variable_at_all(self, rel: str) -> None:
        """QQ 三条 inbound 路径都自带「当前时间: ... CST」，所以这些节点连 now 都不需要。

        提示词里没有任何每轮变化的变量 → 整条系统消息字节稳定 → 工具定义进缓存前缀。
        """
        prompt = _prompt_of(_ROOT / rel)

        assert "{{now}}" not in prompt
        assert "{{instruction}}" not in prompt


class TestAssembledPromptContainsInstructionOnce:
    def _assemble(self, tmp_path: Path, prompt: str, instruction: str, history=None):
        node = Node(id="qq.orchestrator", type="ai", prompt=prompt)
        system_prompt = assemble_prompt(
            tmp_path, node, variables={"node_id": node.id, "node_name": node.name, "instruction": instruction},
        )
        messages, _is_block = assemble_messages_with_injections(
            workspace_root=tmp_path,
            system_prompt=system_prompt,
            history=list(history or []),
            instruction=instruction,
        )
        return messages

    def _text(self, messages) -> str:
        parts = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)

    def test_an_echoing_prompt_would_duplicate_it(self, tmp_path: Path) -> None:
        # 先证明这个断言有鉴别力：模板里嵌 instruction 就会出现两次。
        messages = self._assemble(tmp_path, "RULES\n\n{{instruction}}", "查一下天气")

        assert self._text(messages).count("查一下天气") == 2

    def test_the_current_qq_prompt_yields_exactly_one_copy(self, tmp_path: Path) -> None:
        prompt = _prompt_of(_ROOT / "config/nodes/qq.orchestrator.yaml")

        messages = self._assemble(tmp_path, prompt, "查一下天气")

        assert self._text(messages).count("查一下天气") == 1

    def test_the_copy_that_survives_is_the_trailing_user_message(self, tmp_path: Path) -> None:
        prompt = _prompt_of(_ROOT / "config/nodes/qq.orchestrator.yaml")

        messages = self._assemble(tmp_path, prompt, "查一下天气")

        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "查一下天气"

    def test_history_tail_dedupe_still_prevents_a_third_copy(self, tmp_path: Path) -> None:
        # resume 场景：instruction 已经在 JSONL 历史末尾。
        prompt = _prompt_of(_ROOT / "config/nodes/qq.orchestrator.yaml")
        history = [{"role": "user", "content": "查一下天气"}]

        messages = self._assemble(tmp_path, prompt, "查一下天气", history=history)

        assert self._text(messages).count("查一下天气") == 1

    def test_system_prefix_is_byte_stable_across_turns(self, tmp_path: Path) -> None:
        """两轮不同用户输入必须产出完全相同的系统消息，否则前缀缓存无从命中。"""
        prompt = _prompt_of(_ROOT / "config/nodes/qq.orchestrator.yaml")

        first = self._assemble(tmp_path, prompt, "第一轮问题")[0]
        second = self._assemble(tmp_path, prompt, "第二轮完全不同的问题")[0]

        assert first["role"] == "system"
        assert first["content"] == second["content"]
