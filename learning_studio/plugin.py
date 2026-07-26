"""Plugin registration.

This is the whole of the plugin's contract with Hermes for now: one bundled,
read-only skill. Tools, persistence, and the dashboard land in later PRs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Must match ``name:`` in plugin.yaml — Hermes derives the skill namespace
#: from the manifest, so a mismatch silently changes the agent-facing name.
PLUGIN_NAME = "learning-studio"

#: The bundled skill, addressed by the agent as
#: ``skill_view("learning-studio:adaptive-learning")``.
SKILL_NAME = "adaptive-learning"

#: Reserved toolset name for the tools later PRs will register. Declared here
#: so the identity is fixed even though nothing uses it yet.
TOOLSET_NAME = "plugin_learning_studio"

SKILLS_DIR = Path(__file__).parent / "skills"

_SKILL_DESCRIPTION = (
    "Structure a study session: plan what to cover, run recall practice, "
    "and review what needs another pass."
)


def skill_path(name: str = SKILL_NAME) -> Path:
    """Return the on-disk path to a bundled skill's ``SKILL.md``."""
    return SKILLS_DIR / name / "SKILL.md"


def register(ctx: Any) -> None:
    """Register this plugin's surface with Hermes.

    Called once at startup with a ``PluginContext``. Registration is
    deliberately minimal — a single bundled skill — so that enabling the
    plugin cannot fail on missing optional dependencies or credentials.
    """
    path = skill_path()
    ctx.register_skill(SKILL_NAME, path, _SKILL_DESCRIPTION)
    logger.debug("Registered skill %s:%s from %s", PLUGIN_NAME, SKILL_NAME, path)
