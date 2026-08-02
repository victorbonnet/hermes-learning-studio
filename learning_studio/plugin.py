"""Plugin registration.

The plugin's contract with Hermes: one bundled read-only skill and eight typed
tools — four for learning context, managed assets, and prepared exercises, and
four that open, inspect, report on, and close an exercise on the learner's
screen.

The Mini App itself is still not reachable from here. It lives behind the
optional ``web`` extra and is served by a *separate process* that
``learning_studio_launch`` starts on demand; registration imports no FastAPI,
opens no socket, and starts nothing. Scoring, durable attempts, and a scheduler
are not here either, and the runtime tools say so in their own descriptions
rather than leaving it to be inferred.

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

#: The tools that manage a real process, and therefore the only ones gated by
#: a ``check_fn``.
#:
#: The gate is the *platform* and nothing else. Without POSIX process groups
#: this plugin cannot prove which process it owns and will not signal one it
#: cannot prove, so those four tools genuinely cannot work — and no
#: conversation can change that.
#:
#: Everything else that can go wrong is deliberately *not* gated here. A
#: runtime environment that has not been prepared, or a missing ``cloudflared``,
#: is reported by the handler with something an operator can act on. Gating on
#: those would make the tools vanish from the model's list, and an agent cannot
#: explain the absence of a tool it cannot see — the learner would get silence
#: where they should get "somebody needs to install one thing".
RUNTIME_TOOLS = frozenset(
    {
        "learning_studio_launch",
        "learning_studio_status",
        "learning_studio_results",
        "learning_studio_stop",
    }
)

_SKILL_DESCRIPTION = (
    "Structure a study session: plan what to cover, run recall practice, "
    "and review what needs another pass."
)


def skill_path(name: str = SKILL_NAME) -> Path:
    """Return the on-disk path to a bundled skill's ``SKILL.md``."""
    return SKILLS_DIR / name / "SKILL.md"


#: Fired by the gateway with the real incoming ``MessageEvent``, before the
#: agent runs. It is how launching learns what the learner actually said.
CONSENT_EVIDENCE_HOOK = "pre_gateway_dispatch"


def _register_consent_evidence(ctx: Any) -> None:
    """Ask the host to show us each incoming message before the model sees it.

    The hook only *records*: it never skips a message, never rewrites one, and
    returns ``None`` so dispatch continues exactly as it would have.

    Wrapped in a guard because registration must not raise. A Hermes without
    this hook — or with a different registration surface — should cost the
    profile its ability to *launch*, which then refuses with an explanation,
    not its ability to use the plugin at all.
    """
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):  # pragma: no cover - every current host has it
        logger.warning(
            "this Hermes exposes no %s hook, so the Learning Studio cannot confirm what a "
            "learner asked for and will refuse to open exercises",
            CONSENT_EVIDENCE_HOOK,
        )
        return
    try:
        from .evidence import capture_message_evidence

        register_hook(CONSENT_EVIDENCE_HOOK, capture_message_evidence)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "the Learning Studio could not register %s (%s); launching will refuse",
            CONSENT_EVIDENCE_HOOK,
            type(exc).__name__,
        )


def register(ctx: Any) -> None:
    """Register this plugin's surface with Hermes.

    Called once at startup with a ``PluginContext``. Registration imports
    nothing optional, touches no network, and opens no database, so enabling
    the plugin cannot fail a session.
    """
    path = skill_path()
    ctx.register_skill(SKILL_NAME, path, _SKILL_DESCRIPTION)
    logger.debug("Registered skill %s:%s from %s", PLUGIN_NAME, SKILL_NAME, path)

    _register_consent_evidence(ctx)

    # Imported here rather than at module scope so that a syntax or import
    # error in the tool layer cannot stop the skill from registering.
    from .runtime.availability import runtime_tools_supported
    from .schemas import TOOL_SCHEMAS
    from .tools import HANDLERS

    for name, schema in TOOL_SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET_NAME,
            schema=schema,
            handler=HANDLERS[name],
            description=schema["description"],
            check_fn=runtime_tools_supported if name in RUNTIME_TOOLS else None,
        )
    logger.debug("Registered %d tools in toolset %s", len(TOOL_SCHEMAS), TOOLSET_NAME)
