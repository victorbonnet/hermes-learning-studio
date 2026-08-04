"""The runtime, as the tool layer is allowed to see it.

Everything above this line is a tool handler; everything below is a process.
This module is where the two meet, and it exists so that exactly one place
decides three things:

**When the profile lock is held.** Every transition — start, reuse, stop —
happens inside it, so "is there a runtime?" and "make there be one" are a
single step. Read-only status does not take it: a status call that blocked
behind a start would make the agent's only way of *finding out* what is
happening the thing that waits for it.

**What an agent is shown.** Lifecycle state and generic, actionable reasons.
No address, no port, no process id, no interpreter path, no tunnel URL, no
secret, and no message built from another program's output. The set of fields
is written out below rather than assembled from whatever the runtime happened
to report, so a field added to the control reply cannot reach a tool result by
accident.

**What "prepared" means.** A launch that finds no runtime environment, or no
``cloudflared``, is a refusal with something an operator can act on — not an
attempt to fix either. Neither is a thing a plugin should do to somebody's
machine in the middle of a study session.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from .. import storage
from ..config import LearningStudioConfig, load_config
from . import bootstrap, ownership, supervisor
from .errors import (
    NO_TUNNEL_BINARY,
    NOT_BOOTSTRAPPED,
    UNSUPPORTED_PLATFORM,
    RuntimeUnavailable,
)
from .state import ProfileLock, read_record

logger = logging.getLogger(__name__)


def prerequisites(config: LearningStudioConfig) -> dict[str, bool]:
    """What this machine can and cannot do, as three plain facts.

    Reported by ``learning_studio_status`` so an agent can tell a learner *why*
    an exercise cannot be opened, in terms an operator can act on, without
    naming a path or a platform detail.
    """
    return {
        "platform_supported": ownership.platform_supported(),
        "runtime_prepared": bootstrap.is_bootstrapped(),
        "tunnel_available": bool(supervisor.resolve_cloudflared(config)),
    }


def require_prerequisites(config: LearningStudioConfig) -> None:
    """Refuse, with the actionable reason, before anything is started."""
    ready = prerequisites(config)
    if not ready["platform_supported"]:
        raise RuntimeUnavailable(UNSUPPORTED_PLATFORM, reason="platform_unsupported")
    if not ready["runtime_prepared"]:
        raise RuntimeUnavailable(NOT_BOOTSTRAPPED, reason="runtime_not_bootstrapped")
    if not ready["tunnel_available"]:
        raise RuntimeUnavailable(NO_TUNNEL_BINARY, reason="tunnel_binary_absent")


def status(config: LearningStudioConfig | None = None) -> dict[str, Any]:
    """Lifecycle state for this profile. Reads only, takes no lock.

    A record whose process cannot be proved ours is reported as *not running*
    and left alone. That is the honest answer — from outside, a runtime that
    died and one that is wedged are the same observation — and it keeps a
    status call from having an opinion about somebody else's process.
    """
    settings = config or load_config()
    ready = prerequisites(settings)

    handle = supervisor.current(settings)
    if handle is None:
        return {
            "ok": True,
            "running": False,
            "prerequisites": ready,
            "stale_record": read_record() is not None,
            **_never_scored(settings),
        }

    reply = handle.reply
    return {
        "ok": True,
        "running": True,
        "prerequisites": ready,
        "generation": handle.record.generation,
        "server_state": reply.server_state,
        "tunnel_state": reply.tunnel_state,
        "tunnel_ready": reply.tunnel_ready,
        "idle_seconds": reply.idle_seconds,
        "idle_timeout_seconds": handle.record.idle_timeout_seconds,
        "max_lifetime_seconds": handle.record.max_lifetime_seconds,
        "expires_in_seconds": reply.payload.get("expires_in_seconds"),
        "open_sessions": reply.payload.get("sessions", 0),
        **_never_scored(settings),
    }


def stop(config: LearningStudioConfig | None = None) -> dict[str, Any]:
    """Stop this profile's runtime. Idempotent, and never touches another's."""
    settings = config or load_config()
    with ProfileLock():
        outcome = supervisor.stop(settings)

    return {
        "ok": True,
        "stopped": bool(outcome["stopped"]),
        "state": outcome["state"],
        "message": _STOP_MESSAGES[str(outcome["state"])],
    }


_STOP_MESSAGES = {
    "not_running": "There was no Learning Studio runtime to stop.",
    "stopped": (
        "The Learning Studio runtime has stopped and its temporary public address is closed."
    ),
    "unprovable": (
        "The Learning Studio could not confirm the recorded runtime was its own, so it left "
        "that process alone. It stops itself when its own time limit expires."
    ),
}


def _never_scored(config: LearningStudioConfig) -> dict[str, Any]:
    """Said on every runtime status response.

    This call itself never scores anything — ``scored`` is always ``False``
    here, whatever a learner has done in a Mini App session. ``attempts_stored``
    is the one fact this notice needs to get right: whether *this profile* has
    ever completed a scored session, checked as a bare existence query with no
    learner-scoped detail, never which learner or what they answered.
    """
    return {
        "scored": False,
        "attempts_stored": _profile_has_attempts(config),
        "notice": (
            "Merely opening an exercise records nothing durable. A finished session is "
            "scored and stored once its completion screen is shown."
        ),
    }


def _profile_has_attempts(config: LearningStudioConfig) -> bool:
    """Whether this profile has ever completed a scored session.

    A bare existence check, scoped to the profile column already used
    everywhere else — never which learner, never what they answered. Reads
    only: a missing database (nothing has ever been stored) is answered
    without creating one.
    """
    if not storage.database_path().exists():
        return False
    try:
        from ..paths import profile_id

        with storage.connect(config) as conn:
            row = conn.execute(
                "SELECT 1 FROM attempts WHERE profile_id = ? LIMIT 1", (profile_id(),)
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False
