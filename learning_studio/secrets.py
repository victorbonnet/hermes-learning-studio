"""Credentials, resolved for the profile that is actually being served.

``os.environ`` is a process-global. Hermes can multiplex several profiles
through one process, and when it does, the Telegram token in the environment
may belong to a *different* profile than the turn currently running. Reading it
directly is therefore not a shortcut — it is a cross-profile credential leak
with a plausible-looking implementation.

Hermes solves this with ``agent.secret_scope.get_secret``, and this module is a
thin wrapper over it. The wrapper exists for three reasons and does nothing
else:

**The host is not always there.** This plugin is importable and testable
without Hermes — that is how the suite and the build run. When the host is
absent there is no multiplexing and no other profile to leak from, so the
environment is the right answer and is exactly what the host would do too.

**A missing scope must refuse, not crash.** When multiplexing is on and no
scope is installed, Hermes raises ``UnscopedSecretError`` on purpose, so the
mistake is loud. A learning plugin should not take a session down for it: this
turns it into "no credential", which every caller already handles by refusing
and saying so.

**The precedence is the host's, not ours.** ``get_secret`` already knows which
names are genuinely process-global (``HERMES_HOME``, ``HOME``, ``PATH``) and
which are profile secrets, and it already defines the single-profile fallback
that keeps credentials injected by systemd or a secret-manager wrapper working.
Reimplementing any of that here would be a second, divergent opinion about
whose credential this is.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Where Hermes keeps the Telegram bot token, in a profile's ``.env``.
BOT_TOKEN = "TELEGRAM_BOT_TOKEN"


def get_secret(name: str, default: str = "") -> str:
    """Resolve one credential for the profile currently being served.

    Returns ``""`` rather than raising when the value cannot be resolved
    safely. Every caller in this plugin treats an empty credential as a
    refusal, so the fail-closed path and the absent path are the same path —
    which is what stops a future caller from accidentally handling one and not
    the other.
    """
    try:
        from agent.secret_scope import get_secret as _host_secret
    except ImportError:
        # No Hermes in this process: no multiplexing, no other profile, and the
        # environment is the only source there has ever been.
        return str(os.environ.get(name, default) or "").strip()

    try:
        value = _host_secret(name, default)
    except Exception as exc:
        # `UnscopedSecretError` under multiplexing, or anything else the host
        # raises. The *name* is safe to log; the value never is.
        logger.warning(
            "the Learning Studio could not resolve %s for the active profile (%s); "
            "treating it as absent",
            name,
            type(exc).__name__,
        )
        return ""

    return str(value or "").strip()


def telegram_bot_token() -> str:
    """The active profile's bot token, or ``""``.

    Read on each use rather than cached. A cache would be a copy of a
    credential with a lifetime nobody chose, and under multiplexing it would be
    a copy of *one* profile's credential answering for every profile.
    """
    return get_secret(BOT_TOKEN)
