"""Tool naming rules: what an external tool name may look like, and which names the engine keeps for itself."""
from __future__ import annotations

import re

TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# Handled by the engine's pseudo-tool layer and injected next to the registry tools on every step.
PSEUDO_TOOL_NAMES = frozenset({"finish", "ask", "reply", "switch_node", "compact_context", "preempt_task"})

# One dispatch_to_<target> tool per delegate target; the names can't be enumerated, so match the prefix.
DISPATCH_TOOL_PREFIX = "dispatch_to_"


def is_pseudo_tool_name(name: str) -> bool:
    """Whether the pseudo-tool layer will route this call; a dispatch name needs a non-empty target id."""
    tool_name = str(name or "").strip()
    return tool_name in PSEUDO_TOOL_NAMES or (
        tool_name.startswith(DISPATCH_TOOL_PREFIX)
        and bool(tool_name[len(DISPATCH_TOOL_PREFIX):].strip())
    )


def is_engine_owned_name(name: str) -> bool:
    """Whether an external tool must not take this name: the model would see two tools with the same name."""
    tool_name = str(name or "").strip()
    return tool_name in PSEUDO_TOOL_NAMES or tool_name.startswith(DISPATCH_TOOL_PREFIX)
