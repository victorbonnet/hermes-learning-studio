"""Where a launch message may go, decided by the host and never by the model.

A tool payload cannot name a chat. There is no argument for one, so there is
nothing to override — the same rule that governs learner identity in
:mod:`learning_studio.identity`, applied to the one place this plugin sends
something outward.

The destination is derived from Hermes' own session context: the platform the
current message arrived on, the chat it arrived in, and the account that sent
it. Those values originate in the Telegram adapter, from Telegram's own
payload, and are bound before the agent runs.

Five conditions, all required
-----------------------------

**Telegram.** A Web App button is a Telegram construct. Any other platform is
refused rather than approximated.

**A private chat.** ``chat_type`` must name a one-to-one conversation. Groups,
forums, supergroups and channels are refused: an exercise is one person's, and
a button in a room is an invitation to whoever is in the room.

The accepted vocabulary is worth writing down, because getting it wrong is not
a theoretical risk — it is a bug this code shipped with. **Hermes normalises
Telegram's ``private`` to its own canonical ``dm``** before the session
variables are ever bound (``plugins/platforms/telegram/adapter.py``: ``if
chat_type == "private": chat_type = "dm"``), and it normalises ``supergroup``
to ``group`` or ``forum``. A plugin that accepted only ``private`` therefore
refused *every real Telegram direct message* as though it were a group — the
one surface the whole feature exists for. ``dm`` is the value that actually
arrives; ``private`` and ``sender`` are kept because they are unambiguously
one-to-one in Telegram's own vocabulary and cost nothing to accept.

Everything else is refused, including values this plugin has never seen. The
list is an allowlist, not a denylist, so a chat type introduced by a future
Hermes cannot quietly become a place to send somebody's exercise.

**The chat is the person.** In a Telegram private chat the chat id *is* the
user id. Requiring them to be equal is a second, independent check on the same
fact — one that does not depend on ``chat_type`` being present, which some
paths do not set. A mismatch means the message would arrive somewhere other
than that person's own conversation, and it is refused without trying to work
out where.

**The account is allowed here.** The same intersection the Mini App API
enforces — Hermes' own Telegram gates, optionally narrowed by this plugin's
configuration. A learner Hermes would not talk to does not get a button from
this plugin either.

**Nothing is missing.** An absent chat id, an absent user id, or a session that
does not say what platform it is are all refusals. Guessing a destination is
how a private exercise ends up in the wrong conversation.

The refusals are deliberately readable by the agent, because the agent's
correct next move differs: a group launch means "carry on in chat here", an
unauthorised account means "an operator has to add them".
"""

from __future__ import annotations

from dataclasses import dataclass

from .authorization import effective_allowed_users, is_authorized
from .config import LearningStudioConfig, load_config, load_raw_config
from .identity import Principal, session_value
from .runtime.errors import LaunchRefused

#: Session variables this module reads. Two, and both are bound by the
#: platform adapter from Telegram's own payload.
CHAT_ID = "HERMES_SESSION_CHAT_ID"
CHAT_TYPE = "HERMES_SESSION_CHAT_TYPE"

#: Every value that means "a one-to-one conversation", and no others.
#:
#: - ``dm`` is Hermes' canonical name and the one that actually arrives.
#: - ``private`` is Telegram's raw value, which Hermes normalises away. Kept for
#:   a host that has not, and because it is unambiguous.
#: - ``sender`` is what a Telegram Mini App reports for an inline launch in a
#:   private conversation. Also unambiguous.
#:
#: Deliberately absent: ``group``, ``forum``, ``channel``, ``supergroup``,
#: ``thread``, and anything else. See the module docstring.
PRIVATE_CHAT_TYPES = frozenset({"dm", "private", "sender"})

NOT_TELEGRAM = (
    "Opening an exercise needs a Telegram conversation, and this session is not one. "
    "Nothing was started. Run the exercise here in conversation instead."
)

NOT_PRIVATE = (
    "This is a group conversation, and a Learning Studio exercise is one person's. Nothing "
    "was started and no message was sent. Offer the exercise in a direct message, or run it "
    "here as text without anyone's personal record attached."
)

NO_DESTINATION = (
    "This session does not identify a private conversation to send the exercise to, so "
    "nothing was started. Continue in conversation."
)

NOT_AUTHORISED = (
    "This Telegram account is not on the profile's allowlist, so the Learning Studio will "
    "not open an exercise for it. Nothing was started. An operator has to add the account "
    "before this can work."
)


@dataclass(frozen=True)
class TelegramDestination:
    """One private chat, proved to be the sender's own.

    Both fields are the same number, and both are kept: the chat id is what a
    message is addressed to, the user id is what a grant is bound to, and
    writing the equality down once here is better than assuming it twice later.
    """

    chat_id: str
    telegram_user_id: str

    def describe(self) -> dict[str, str]:
        """Non-identifying summary. Neither identifier is echoed anywhere."""
        return {"platform": "telegram", "surface": "private_chat"}


def resolve_destination(
    *,
    principal: Principal,
    config: LearningStudioConfig | None = None,
) -> TelegramDestination:
    """Derive the one place this launch may send a button, or refuse."""
    settings = config or load_config()

    if principal.platform != "telegram" or not principal.user_id:
        raise LaunchRefused(NOT_TELEGRAM, reason="destination_not_telegram")

    chat_type = session_value(CHAT_TYPE)
    if chat_type and chat_type.lower() not in PRIVATE_CHAT_TYPES:
        raise LaunchRefused(NOT_PRIVATE, reason="destination_group_chat")

    chat_id = session_value(CHAT_ID)
    if not chat_id:
        raise LaunchRefused(NO_DESTINATION, reason="destination_absent")
    if chat_id != principal.user_id:
        # In a Telegram private chat these are the same number. When they are
        # not, the surface is a group, a channel, a forwarded context, or
        # something this plugin has not seen — and none of those is a place to
        # send one person's exercise.
        raise LaunchRefused(NOT_PRIVATE, reason="destination_not_the_sender")

    allowed = effective_allowed_users(
        plugin_restriction=settings.mini_app_allowed_telegram_users,
        host_config=load_raw_config(),
    )
    if not is_authorized(principal.user_id, allowed):
        raise LaunchRefused(NOT_AUTHORISED, reason="destination_not_allowed")

    return TelegramDestination(chat_id=chat_id, telegram_user_id=principal.user_id)
