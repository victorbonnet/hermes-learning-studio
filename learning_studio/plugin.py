"""Plugin registration.

The plugin's contract with Hermes: one bundled read-only skill and two typed
tools for learning context. The dashboard, exercise runtime, and Mini App
land in later PRs.

``register(ctx)`` runs at every Hermes startup for every enabled plugin, so
it must not raise. It deliberately does **not** open the database: a
corrupt or newer-versioned store would then take the whole plugin down at
startup rather than failing one tool call with an explanation. Storage is
initialised lazily, inside the handlers.
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

#: Toolset the Learning Studio's tools are grouped under.
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

    Called once at startup with a ``PluginContext``. Registration imports
    nothing optional, touches no network, and opens no database, so enabling
    the plugin cannot fail a session.
    """
    path = skill_path()
    ctx.register_skill(SKILL_NAME, path, _SKILL_DESCRIPTION)
    logger.debug("Registered skill %s:%s from %s", PLUGIN_NAME, SKILL_NAME, path)

    # Imported here rather than at module scope so that a syntax or import
    # error in the tool layer cannot stop the skill from registering.
    from .schemas import TOOL_SCHEMAS
    from .tools import HANDLERS

    for name, schema in TOOL_SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET_NAME,
            schema=schema,
            handler=HANDLERS[name],
            description=schema["description"],
        )
    logger.debug("Registered %d tools in toolset %s", len(TOOL_SCHEMAS), TOOLSET_NAME)
