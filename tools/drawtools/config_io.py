from __future__ import annotations

"""Helpers for user-editable drawtools config files.

Only *.example.* files ship with the repository. Runtime files (settings.yaml,
character_tags.yaml, prompts/*.md) belong to the instance, live in its
config/drawtools/, and are created from the examples when missing.
"""

# The supervisor and bot import this as tools.drawtools.config_io; scripts load it from its own directory.
if __package__:
    from .common import WORKSPACE_ROOT, config_layout
else:
    from common import WORKSPACE_ROOT, config_layout


def ensure_user_configs() -> list[str]:
    return config_layout.ensure_drawtools_configs(WORKSPACE_ROOT)
