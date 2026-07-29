"""Telegram Mini App ``initData`` verification.

This module is the Mini App's entire authentication boundary. Everything
downstream — sessions, experiences, managed image bytes — trusts exactly one
value that comes out of here: the Telegram user ID. So the verification is the
documented algorithm, in full, with nothing softened:

1. **Canonical data-check string.** Every received field except ``hash``,
   sorted by key, rendered ``key=value``, joined with ``\\n``.

   ``signature`` **is included**, and that is the whole subtlety. Telegram
   documents two validation algorithms and they exclude different fields:

   - the *third-party* Ed25519 algorithm, for verifiers that do not hold the
     bot token, excludes ``hash`` **and** ``signature`` and prepends
     ``<bot_id>:WebAppData``;
   - the *bot-token* HMAC algorithm used here excludes only ``hash``.

   Excluding ``signature`` here — applying the Ed25519 rule to the HMAC path —
   rejects every genuine launch from a Telegram client that sends the field,
   which current clients do. Both reference implementations confirm the split:
   ``@telegram-apps/init-data-node`` skips ``signature`` in ``validate3rd`` but
   pushes it into the signed pairs in ``validate``, and aiogram's
   ``check_webapp_signature`` pops only ``hash``.
2. **Secret key derivation.** ``HMAC-SHA256(key="WebAppData", data=bot_token)``.
   The key and the message are the other way round from the usual reading of
   the docs' notation, and getting them backwards produces a verifier that
   rejects every genuine payload — hence the fixtures in ``tests/init_data.py``
   are built from the specification independently of this code.
3. **Constant-time comparison.** :func:`hmac.compare_digest`, never ``==``: a
   short-circuiting comparison against an attacker-supplied hash leaks the
   expected digest one byte at a time.
4. **Freshness.** ``auth_date`` must be recent, which is what stops a captured
   payload from being replayed forever, and must not be in the future by more
   than :data:`FUTURE_SKEW_SECONDS`. The tolerance is deliberate and stated
   rather than implied: a client whose clock runs a few seconds fast is
   ordinary, and refusing it would deny a legitimate learner for a reason
   nobody could diagnose. Sixty seconds does not meaningfully widen the replay
   window, which the max-age bound governs.
5. **A usable user.** A validated numeric ID, not a bot, and *only* the ID is
   kept — the display name, username, language and photo Telegram also sends
   are personal data with no role here.

The whole module is standard library. It is imported by the FastAPI layer, but
does not depend on it, so the verification can be tested — and reasoned about —
without a web server anywhere near it.

Failures raise :class:`InitDataError`, whose message is fixed and generic. The
specific reason travels in :attr:`InitDataError.reason` for structured logs and
never reaches the client: telling a caller *why* their forgery failed is free
oracle access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib.parse import parse_qsl

#: Bound applied before parsing. A genuine payload is a few hundred characters;
#: this leaves generous room for ``start_param`` while refusing to spend CPU
#: percent-decoding a megabyte someone posted at the endpoint.
MAX_INIT_DATA_CHARS = 4096

#: Excluded from the data-check string for the bot-token HMAC algorithm: the
#: hash cannot sign itself. ``signature`` is deliberately *not* here — it is
#: excluded only by the third-party Ed25519 algorithm, which this module does
#: not implement. See the module docstring.
_UNSIGNED_FIELDS = frozenset({"hash"})

#: ``chat_type`` values that mean "a private conversation with the bot". A
#: Mini App launched anywhere else is a group surface, and this plugin's data
#: is personal, so those are refused rather than shared with a room.
_DIRECT_CHAT_TYPES = frozenset({"sender", "private"})

#: Clocks disagree. A minute of tolerance accepts an honestly-fast client
#: without widening the replay window in any meaningful way.
FUTURE_SKEW_SECONDS = 60

#: What every failure says, whatever went wrong.
AUTH_FAILED_MESSAGE = "Telegram authentication failed."


class InitDataError(Exception):
    """Verification failed. ``reason`` is for logs; the message is for clients."""

    def __init__(self, reason: str) -> None:
        super().__init__(AUTH_FAILED_MESSAGE)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedUser:
    """The only thing a verified payload yields: who, and when they launched.

    Note what is absent. Telegram's ``user`` object also carries a first name,
    last name, username, language code, and photo URL; none is retained, so
    none can end up in a session, a log line, or a response.
    """

    #: Telegram's numeric user ID, as a string — the same form
    #: ``HERMES_SESSION_USER_ID`` carries, so a Mini App session resolves to
    #: the same learner as that person's chat session.
    user_id: str
    #: Unix timestamp Telegram signed the payload at.
    auth_date: int

    def describe(self) -> dict[str, object]:
        """Non-identifying summary, safe for a response body."""
        return {"platform": "telegram", "authenticated": True}


def verify_init_data(
    raw: object,
    *,
    bot_token: str,
    now: int,
    max_age_seconds: int,
) -> VerifiedUser:
    """Verify one ``initData`` payload, or raise :class:`InitDataError`.

    ``now`` and ``max_age_seconds`` are parameters rather than ambient state so
    that freshness is testable and so the caller can apply a tighter window at
    session bootstrap than on subsequent calls within that session's life.
    """
    fields = _parsed(raw)
    _reject_group_launch(fields)
    _check_hash(fields, bot_token)
    auth_date = _fresh_auth_date(fields, now=now, max_age_seconds=max_age_seconds)
    return VerifiedUser(user_id=_user_id(fields), auth_date=auth_date)


def _parsed(raw: object) -> dict[str, str]:
    """Percent-decode into exactly one value per field, or refuse."""
    if not isinstance(raw, str):
        raise InitDataError("init_data_not_a_string")
    if not raw.strip():
        raise InitDataError("init_data_empty")
    if len(raw) > MAX_INIT_DATA_CHARS:
        raise InitDataError("init_data_too_large")

    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("init_data_unparseable") from exc

    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            # Two values for one field are two claims. Picking either would let
            # a caller sign one and have the server read the other.
            raise InitDataError("init_data_duplicate_field")
        fields[key] = value

    for required in ("hash", "auth_date", "user"):
        if not fields.get(required):
            raise InitDataError(f"init_data_missing_{required}")
    return fields


def _reject_group_launch(fields: dict[str, str]) -> None:
    """Refuse anything launched from a group, supergroup, or channel.

    Two independent signals, because a payload may carry either: ``chat_type``
    names the surface, and ``chat`` is only present for a group or channel
    launch. Absence of both is a direct launch from the bot's own chat, which
    is the supported case.
    """
    chat_type = fields.get("chat_type")
    if chat_type is not None and chat_type not in _DIRECT_CHAT_TYPES:
        raise InitDataError("init_data_group_chat_type")
    if fields.get("chat"):
        raise InitDataError("init_data_group_chat_object")


def _check_hash(fields: dict[str, str], bot_token: str) -> None:
    """The signature check itself: derive, recompute, compare in constant time."""
    if not isinstance(bot_token, str) or not bot_token.strip():
        # No token means no way to verify anything. Refusing is the only safe
        # answer; "no token configured" must never mean "accept everything".
        raise InitDataError("bot_token_unavailable")

    received = fields["hash"]
    if len(received) != 64 or any(c not in "0123456789abcdefABCDEF" for c in received):
        raise InitDataError("init_data_hash_malformed")

    check_string = "\n".join(
        f"{key}={fields[key]}" for key in sorted(fields) if key not in _UNSIGNED_FIELDS
    )
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received.lower()):
        raise InitDataError("init_data_hash_mismatch")


def _fresh_auth_date(fields: dict[str, str], *, now: int, max_age_seconds: int) -> int:
    raw = fields["auth_date"]
    if not raw.isdigit():
        raise InitDataError("auth_date_not_numeric")
    auth_date = int(raw)

    if auth_date > now + FUTURE_SKEW_SECONDS:
        raise InitDataError("auth_date_in_future")
    if now - auth_date > max_age_seconds:
        raise InitDataError("auth_date_expired")
    return auth_date


def _user_id(fields: dict[str, str]) -> str:
    """Extract the numeric ID, and nothing else, from the ``user`` object."""
    try:
        user = json.loads(fields["user"])
    except (ValueError, TypeError) as exc:
        raise InitDataError("user_unparseable") from exc

    if not isinstance(user, dict):
        raise InitDataError("user_not_an_object")
    if user.get("is_bot") is True:
        # A bot is not a learner, and its "identity" is not a person's consent.
        raise InitDataError("user_is_bot")

    user_id = user.get("id")
    # bool is an int subclass; `"id": true` is nonsense, not user 1.
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise InitDataError("user_id_invalid")
    return str(user_id)
