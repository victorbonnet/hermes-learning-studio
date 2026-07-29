"""What the API depends on, gathered in one injectable object.

Every piece of ambient state the request path needs — the clock, the bot
token, the effective allowlist, the profile name, the session store, and the
two service reads — arrives through :class:`Dependencies`. Nothing in
``app.py`` reads ``os.environ`` or calls ``time.time()`` directly.

That is a security decision, not a style one. Tests need to control identity,
time, and authorisation; the usual way to give them that is a
``DEV_MODE``/``SKIP_AUTH`` switch, and such a switch is one mistyped
environment variable away from an unauthenticated production API. There is no
such flag here and no code path that skips verification. A test substitutes a
*fake bot token* and then signs real payloads with it, so the same HMAC check
runs in tests as in production — the thing under test is never bypassed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..authorization import effective_allowed_users
from ..config import LearningStudioConfig, load_config, load_raw_config
from ..identity import Principal
from ..paths import profile_id
from ..sessions import SessionStore

#: Where Hermes already keeps the bot token. Read on demand, never copied into
#: configuration, a database row, a response, a log line, or a process
#: argument; the only thing it is ever used for is deriving the HMAC key.
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

#: Per-process key for pseudonymising user IDs in logs. Random per start, held
#: only in memory: log lines correlate within one run of the server and are
#: not linkable to a Telegram account by anyone reading them afterwards.
_LOG_PEPPER = secrets.token_bytes(32)


def bot_token_from_env(env: Mapping[str, str] | None = None) -> str:
    """Return the configured bot token, or ``""`` if there is none.

    An empty result is a refusal downstream, never a bypass:
    :func:`learning_studio.telegram_auth.verify_init_data` treats an absent
    token as a verification failure.
    """
    environment = os.environ if env is None else env
    return str(environment.get(BOT_TOKEN_ENV, "") or "").strip()


def user_log_reference(user_id: str) -> str:
    """A short, per-process pseudonym for a Telegram user ID.

    Telegram IDs are small integers, so a plain hash is brute-forceable; the
    per-process random pepper is what makes this reference useless to a reader
    of the logs while still letting one run's lines be correlated.
    """
    return hmac.new(_LOG_PEPPER, str(user_id).encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def _unwired(name: str):
    raise RuntimeError(f"The Mini App API was created without a {name} dependency.")


@dataclass(frozen=True)
class Dependencies:
    """The API's entire connection to the outside world."""

    config: LearningStudioConfig
    sessions: SessionStore
    #: Called per request, so rotating the token in ``.env`` takes effect
    #: without holding a copy anywhere.
    bot_token: Callable[[], str]
    #: The already-intersected effective allowlist. Recomputed per request so
    #: revoking access does not need a restart.
    allowed_users: Callable[[], frozenset[str]]
    profile: Callable[[], str]
    clock: Callable[[], float] = time.time
    #: The two ownership-checked service reads. They default to a refusal
    #: rather than to ``None`` so that a partially wired application fails with
    #: a stated reason instead of an ``AttributeError`` three frames deeper.
    load_experience: Callable[..., Any] = field(default=lambda *_: _unwired("load_experience"))
    load_asset: Callable[..., Any] = field(default=lambda *_: _unwired("load_asset"))

    def now(self) -> float:
        return float(self.clock())

    def principal(self, telegram_user_id: str) -> Principal:
        """The learner this Telegram account maps to.

        ``platform`` and ``user_id`` are exactly what the Telegram gateway
        binds into Hermes' session context, so a Mini App session and a chat
        session resolve to the *same* stored learner rather than to two
        records that happen to belong to one person.
        """
        return Principal(
            profile=self.profile(),
            platform="telegram",
            user_id=str(telegram_user_id),
            source="telegram_mini_app",
        )


def build_dependencies(
    *,
    config: LearningStudioConfig | None = None,
    clock: Callable[[], float] = time.time,
) -> Dependencies:
    """Wire the real implementations. Used by anything that actually serves."""
    from .. import service

    resolved = config or load_config()

    def allowed_users() -> frozenset[str]:
        return effective_allowed_users(
            plugin_restriction=resolved.mini_app_allowed_telegram_users,
            host_config=load_raw_config(),
        )

    return Dependencies(
        config=resolved,
        sessions=SessionStore(
            ttl_seconds=resolved.mini_app_session_ttl_seconds,
            max_sessions=resolved.mini_app_max_sessions,
            clock=clock,
        ),
        bot_token=bot_token_from_env,
        allowed_users=allowed_users,
        profile=profile_id,
        clock=clock,
        load_experience=lambda principal, experience_id: service.delivery_bundle(
            principal=principal, experience_id=experience_id, config=resolved
        ),
        load_asset=lambda principal, asset_id: service.read_managed_asset(
            principal=principal, asset_id=asset_id, config=resolved
        ),
    )
