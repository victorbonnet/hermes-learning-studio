"""Who is asking — resolved from the host, never from the model.

The previous design took a ``learner_key`` tool argument and treated it as
identity. That is not identity, it is a *claim*: the model writes tool
arguments, and anything the model writes is reachable by anyone who can
influence the conversation. Supplying a guessed or overheard platform user ID
was enough to read or modify that person's learning record.

Identity therefore comes from Hermes' own session context, which the gateway
binds from the real platform payload before the agent runs:

    gateway.session_context.get_session_env("HERMES_SESSION_USER_ID")

That value originates in the platform adapter (for Telegram, ``message.from.id``),
lives in a ``contextvars.ContextVar``, and is never derived from model output.
Hermes core trusts the same mechanism for approval decisions, which is the
strongest available signal that it is the supported way to ask who is on the
other end.

Two rules follow, and both are enforced below rather than documented:

- **No tool argument may name a learner.** There is no argument to override,
  so there is nothing to impersonate with.
- **Ambiguous identity is refused, not guessed.** If a gateway session is
  active but supplies no user ID, this plugin cannot tell two people apart,
  so it stores nothing rather than merging their records.
"""

from __future__ import annotations

from dataclasses import dataclass

from .paths import profile_id

#: Platform recorded when Hermes is driven locally (CLI, cron, one-shot) with
#: no gateway session bound. The profile is the principal there.
LOCAL_PLATFORM = "local"


class IdentityError(Exception):
    """The caller could not be identified safely. The message is agent-safe."""


#: Returned when a gateway session is active but anonymous. Deliberately
#: explains the refusal, because the agent's correct next move is to continue
#: in conversation rather than retry.
AMBIGUOUS_IDENTITY_MESSAGE = (
    "This session does not identify who is speaking, so the Learning Studio cannot tell "
    "one learner from another and has stored nothing. Continue the session in "
    "conversation; nothing will be remembered between sessions."
)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, as the host reports them."""

    #: Hermes profile — the outermost storage boundary.
    profile: str
    #: Platform the message arrived on, or :data:`LOCAL_PLATFORM`.
    platform: str
    #: Stable platform user ID of the message sender. Empty only for
    #: :data:`LOCAL_PLATFORM`, where the profile is the identity.
    user_id: str
    #: Conversation the message arrived in. Recorded for provenance; it is
    #: deliberately *not* part of the learner scope, so the same person keeps
    #: one learning record across their DM and a group chat.
    chat_id: str = ""
    chat_type: str = ""
    #: ``gateway_session`` | ``local_profile`` — which resolution path applied.
    source: str = "local_profile"

    @property
    def is_local(self) -> bool:
        return self.platform == LOCAL_PLATFORM

    @property
    def scope(self) -> str:
        """The stable string that identifies this learner for storage.

        Platform is included so that the same numeric ID on two platforms is
        two people, which it is.
        """
        return f"{self.platform}\x00{self.user_id}"

    def describe(self) -> dict[str, str]:
        """Non-sensitive summary for tool responses.

        The raw user ID is never echoed: a tool result is model-visible and
        becomes conversation history, so repeating a platform identifier there
        would leak it into transcripts, logs, and any later summary.
        """
        return {
            "platform": self.platform,
            "profile_scope": self.profile,
            "identity_source": self.source,
        }


def _session_value(name: str) -> str:
    """Read one Hermes session variable.

    ``gateway.session_context`` is documented as the public replacement for
    the old ``os.getenv("HERMES_SESSION_*")`` calls. When it is importable it
    is authoritative — it distinguishes "never bound in this task" from
    "explicitly bound to empty", which the environment cannot, and that
    distinction is what keeps concurrent gateway sessions from reading each
    other's identity.

    Only when the host is absent entirely does this fall back to the
    environment, which is exactly what the host does in that case too.
    """
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        import os

        return str(os.environ.get(name, "") or "").strip()

    try:
        return str(get_session_env(name, "") or "").strip()
    except Exception:  # pragma: no cover - defensive; host-side failure
        return ""


def _gateway_session_active() -> bool:
    """True when some host in this process binds sessions via the gateway.

    This is what distinguishes "a local CLI where the profile is the user"
    from "a multi-user host that simply did not tell us who is speaking". The
    two need opposite defaults: the first is safe, the second is not.
    """
    try:
        from gateway.session_context import session_context_engaged
    except ImportError:
        return False
    try:
        return bool(session_context_engaged())
    except Exception:  # pragma: no cover - defensive
        return True  # fail closed: assume multi-user


def resolve_principal() -> Principal:
    """Resolve the calling learner from host-supplied session state.

    Raises :class:`IdentityError` when a multi-user host is active but has
    not said who is speaking. Refusing is the only safe answer: the
    alternative is writing one person's goals into a record another person
    can read.
    """
    profile = profile_id()
    platform = _session_value("HERMES_SESSION_PLATFORM")
    user_id = _session_value("HERMES_SESSION_USER_ID")

    if platform and user_id:
        return Principal(
            profile=profile,
            platform=platform,
            user_id=user_id,
            chat_id=_session_value("HERMES_SESSION_CHAT_ID"),
            chat_type=_session_value("HERMES_SESSION_CHAT_TYPE"),
            source="gateway_session",
        )

    if _gateway_session_active() or platform:
        # A gateway host is running but this turn carries no sender identity —
        # an anonymous webhook, a channel post, a platform that omits the
        # author. Storing anything here would pool strangers into one record.
        raise IdentityError(AMBIGUOUS_IDENTITY_MESSAGE)

    return Principal(
        profile=profile,
        platform=LOCAL_PLATFORM,
        user_id="",
        source="local_profile",
    )
