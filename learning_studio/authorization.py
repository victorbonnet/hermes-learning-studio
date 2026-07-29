"""Who may open the Mini App.

A verified ``initData`` payload proves *which Telegram account* is calling. It
proves nothing about whether that account is allowed anywhere near this
profile's learning data — Telegram will happily sign a payload for any person
who finds the bot.

Authorisation is therefore an **intersection**, computed here:

    effective = profile Telegram allowlist  ∩  plugin restriction

The profile allowlist is the operator's existing answer to "who may talk to
this Hermes at all", expressed the way Hermes already expresses it:
``TELEGRAM_ALLOWED_USERS`` in ``.env`` and ``platforms.telegram.extra`` in
``config.yaml``. This plugin reads it and may only *narrow* it. There is no
setting here that adds a user, because a plugin that could widen the host's
allowlist would be a privilege-escalation feature with a configuration file
for an interface.

Three consequences follow, and all three are enforced rather than documented:

- **Group authorisations grant nothing.** ``TELEGRAM_GROUP_ALLOWED_USERS`` and
  ``group_allowed_chats`` authorise participation in a room, not access to one
  person's learning record. A Mini App session is inherently personal, so only
  the direct-message allowlists count.
- **Empty means denied.** An unset allowlist is an unconfigured deployment,
  not an open one.
- **Unreadable means denied.** If the host configuration cannot be read, the
  answer is "no", not "probably fine".
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

#: Direct-message user allowlists. Hermes accepts both the environment form and
#: the ``config.yaml`` form, and honours the union of them, so this plugin
#: reads the same union rather than inventing a stricter reading of the
#: operator's intent.
_ENV_DM_ALLOWLISTS = ("TELEGRAM_ALLOWED_USERS",)
_CONFIG_DM_ALLOWLISTS = ("allow_from", "allow_admin_from")

#: Read by Hermes but deliberately *not* by this plugin. Listed so the omission
#: is visible and so a test can assert it stays an omission.
GROUP_ONLY_SOURCES = (
    "TELEGRAM_GROUP_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_CHATS",
    "group_allow_from",
    "group_allowed_chats",
)


def _identifiers(raw: Any) -> set[str]:
    """Normalise one allowlist value into a set of Telegram user IDs.

    Accepts the two shapes Hermes accepts: a comma-separated string (the
    environment form) and a list (the YAML form). Anything that is not a
    plain positive integer is dropped — an allowlist entry nobody can match is
    safer than a wildcard nobody intended.
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


def profile_allowed_users(
    *,
    env: Mapping[str, str] | None = None,
    host_config: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """The profile's direct-message Telegram allowlist.

    This is the host's decision, read and not reinterpreted. Group-only
    allowlists are excluded; see :data:`GROUP_ONLY_SOURCES`.
    """
    environment = os.environ if env is None else env
    allowed: set[str] = set()
    for name in _ENV_DM_ALLOWLISTS:
        allowed |= _identifiers(environment.get(name))

    extra = _telegram_extra(host_config)
    for key in _CONFIG_DM_ALLOWLISTS:
        allowed |= _identifiers(extra.get(key))
    return frozenset(allowed)


def _telegram_extra(host_config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(host_config, Mapping):
        return {}
    platforms = host_config.get("platforms")
    if not isinstance(platforms, Mapping):
        return {}
    telegram = platforms.get("telegram")
    if not isinstance(telegram, Mapping):
        return {}
    extra = telegram.get("extra")
    return extra if isinstance(extra, Mapping) else {}


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
