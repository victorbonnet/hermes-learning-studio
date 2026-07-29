"""Who may open the Mini App.

A verified ``initData`` payload proves *which Telegram account* is calling. It
proves nothing about whether that account is allowed anywhere near this
profile's learning data — Telegram will happily sign a payload for any person
who finds the bot.

Authorisation is therefore an **intersection**, computed here:

    effective = profile Telegram DM allowlist  ∩  plugin restriction

The plugin side may only *narrow*. There is no setting that adds a user,
because a plugin able to widen the host's allowlist would be a
privilege-escalation feature with a configuration file for an interface.

Reading the profile side correctly
----------------------------------

The profile side is Hermes' decision, and this module reproduces the shape of
``gateway/authz_mixin.py::_is_user_authorized`` for a **direct message**
rather than inventing a reading of the raw fields:

1. If *any* Telegram or gateway environment allowlist is configured
   (``TELEGRAM_ALLOWED_USERS``, ``TELEGRAM_GROUP_ALLOWED_USERS``,
   ``TELEGRAM_GROUP_ALLOWED_CHATS``, ``GATEWAY_ALLOWED_USERS``), Hermes decides
   a DM from the *environment* allowlists alone and never consults the
   adapter's ``allow_from``. The DM allowlist is then
   ``TELEGRAM_ALLOWED_USERS ∪ GATEWAY_ALLOWED_USERS``.
2. Only when **no** environment allowlist is configured at all does Hermes fall
   back to the adapter's configured ``allow_from``.

Unioning the two unconditionally — which this module did first — can authorise
somebody the host denies: an operator who sets ``TELEGRAM_ALLOWED_USERS`` and
leaves a stale ``allow_from`` in configuration has, in Hermes' view, one
allowlist and this plugin would have honoured two.

``allow_admin_from`` is **not** an authorisation source. Hermes reads it only
in ``gateway/slash_access.py``, to decide which *already authorised* users may
run privileged slash commands. Treating it as an access grant let a user
excluded from Telegram entirely reach the Mini App.

Platform configuration is read from every shape Hermes accepts: top-level
``platforms.telegram`` and ``gateway.platforms.telegram``, each with
``allow_from`` written directly or inside ``extra`` (``gateway/config.py``
bridges the former into the latter).

What this module deliberately does not honour
---------------------------------------------

Each of these can only ever *deny* somebody Hermes would allow, never admit
somebody Hermes would deny, which is the safe direction for a plugin that has
promised never to broaden access:

- **Wildcards.** ``allow_from: ["*"]`` opens a chat bot to everyone. It does
  not open one person's learning record to everyone, so a wildcard authorises
  nobody here and the operator must name IDs.
- **Allow-all flags.** ``GATEWAY_ALLOW_ALL_USERS`` and
  ``TELEGRAM_ALLOW_ALL_USERS`` are ignored for the same reason.
- **Group-only grants.** ``TELEGRAM_GROUP_ALLOWED_USERS``,
  ``TELEGRAM_GROUP_ALLOWED_CHATS``, ``group_allow_from``, and
  ``group_allowed_chats`` authorise participation in a room, not access to a
  personal record. They are counted only when deciding whether *any*
  environment allowlist exists — never as members.
- **DM pairing grants.** A pairing approval is a first-class grant in Hermes,
  stored outside configuration. This module cannot read it without reaching
  into host internals, so a paired-but-unlisted user is denied. Hermes writes
  approvals into the allowlist whenever one is configured, so the gap is
  narrow; the remedy is naming the user in ``TELEGRAM_ALLOWED_USERS``.

An empty allowlist authorises nobody, and a host configuration that cannot be
read raises rather than resolving to "empty" (see
:func:`learning_studio.config.load_raw_config`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

#: Environment allowlists whose mere presence makes Hermes decide from the
#: environment and stop consulting adapter configuration.
ENV_ALLOWLISTS = (
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_CHATS",
    "GATEWAY_ALLOWED_USERS",
)

#: Of those, the ones that authorise a *direct message* sender.
_ENV_DM_ALLOWLISTS = ("TELEGRAM_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS")

#: The adapter-configured DM allowlist, consulted only when no environment
#: allowlist exists. ``allow_admin_from`` is absent on purpose — see the module
#: docstring.
_CONFIG_DM_ALLOWLIST = "allow_from"

#: Every place Hermes accepts a Telegram platform block.
_PLATFORM_PATHS = (
    ("platforms", "telegram"),
    ("gateway", "platforms", "telegram"),
)

#: Read by Hermes but deliberately never counted as members here. Listed so the
#: omission is visible and so a test can assert it stays an omission.
GROUP_ONLY_SOURCES = (
    "TELEGRAM_GROUP_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_CHATS",
    "group_allow_from",
    "group_allowed_chats",
)

#: Never an authorisation source: slash-command privilege only.
NON_AUTHORISING_CONFIG_KEYS = ("allow_admin_from", "group_allow_admin_from")


def _identifiers(raw: Any) -> set[str]:
    """Normalise one allowlist value into a set of Telegram user IDs.

    Accepts the two shapes Hermes accepts: a comma-separated string (the
    environment form) and a list (the YAML form). Anything that is not a plain
    positive integer — including the ``*`` wildcard — is dropped, so an
    allowlist this module cannot read exactly authorises nobody rather than
    approximately somebody.
    """
    if raw is None:
        return set()
    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, (list, tuple, set, frozenset)):
        return set()

    out: set[str] = set()
    for item in items:
        if isinstance(item, bool):
            continue
        text = str(item).strip()
        if text.isdigit() and int(text) > 0:
            out.add(str(int(text)))
    return out


def _env_value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def any_env_allowlist_configured(env: Mapping[str, str] | None = None) -> bool:
    """True when Hermes would decide this DM from the environment alone."""
    environment = os.environ if env is None else env
    return any(_env_value(environment, name) for name in ENV_ALLOWLISTS)


def _telegram_platform_config(host_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge every shape a Telegram platform block is written in.

    ``extra`` is folded in beneath the top-level keys because
    ``gateway/config.py`` bridges top-level ``allow_from`` *into* ``extra`` —
    they are the same setting arriving by two routes, so reading only one of
    them misses half the operators.
    """
    merged: dict[str, Any] = {}
    if not isinstance(host_config, Mapping):
        return merged

    for path in _PLATFORM_PATHS:
        node: Any = host_config
        for segment in path:
            node = node.get(segment) if isinstance(node, Mapping) else None
        if not isinstance(node, Mapping):
            continue
        extra = node.get("extra")
        if isinstance(extra, Mapping):
            merged.update(extra)
        merged.update({k: v for k, v in node.items() if k != "extra"})
    return merged


def profile_allowed_users(
    *,
    env: Mapping[str, str] | None = None,
    host_config: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """The profile's effective direct-message Telegram allowlist.

    Mirrors Hermes' own precedence: environment allowlists win outright, and
    the adapter's ``allow_from`` applies only when no environment allowlist is
    configured.
    """
    environment = os.environ if env is None else env

    if any_env_allowlist_configured(environment):
        allowed: set[str] = set()
        for name in _ENV_DM_ALLOWLISTS:
            allowed |= _identifiers(_env_value(environment, name))
        return frozenset(allowed)

    platform = _telegram_platform_config(host_config)
    return frozenset(_identifiers(platform.get(_CONFIG_DM_ALLOWLIST)))


def effective_allowed_users(
    *,
    plugin_restriction: tuple[str, ...] | frozenset[str] | None,
    env: Mapping[str, str] | None = None,
    host_config: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """Intersect the profile allowlist with this plugin's optional restriction.

    An empty or absent ``plugin_restriction`` means "no additional
    restriction" — *not* "allow everyone", which is why it is intersected only
    when it names somebody.
    """
    profile = profile_allowed_users(env=env, host_config=host_config)
    restriction = _identifiers(list(plugin_restriction or ()))
    if not restriction:
        return profile
    return frozenset(profile & restriction)


def is_authorized(user_id: str, allowed: frozenset[str]) -> bool:
    """Membership, fail-closed. An empty allowlist authorises nobody."""
    if not allowed or not user_id:
        return False
    return str(user_id) in allowed
