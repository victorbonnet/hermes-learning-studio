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

A Telegram DM passes **two** gates in Hermes, in order, and a sender must clear
both. This module bounds Mini App access by both, so it cannot exceed either.

**Gate one — adapter intake.**
``plugins/platforms/telegram/adapter.py::_is_user_authorized_from_message``
runs before batching, event construction, and the runner. Its comment is
explicit — *"Adapter-level allow_from / group_allow_from: when set, they are
the sole authority"* — and the test is ``if adapter_allow_from is not None``,
so a **present but empty** ``allow_from`` authorises nobody and a message from
anyone outside it never reaches the rest of Hermes at all.

**Gate two — runner authorisation.**
``gateway/authz_mixin.py::_is_user_authorized`` then decides from the
environment allowlists (``TELEGRAM_ALLOWED_USERS ∪ GATEWAY_ALLOWED_USERS``)
when any environment allowlist is configured, falling back to the adapter's
``allow_from`` when none is.

So the effective host policy is the *intersection* of the two, and that is what
this module computes:

===========================  ==========================  =========================
``allow_from``               environment allowlist        Mini App upper bound
===========================  ==========================  =========================
present (ids)                configured                  ids ∩ environment
present (ids)                absent                      ids
present but empty            anything                    nobody
absent                       configured                  environment
absent                       absent                      nobody
===========================  ==========================  =========================

Two earlier versions of this got it wrong in opposite directions, and both
broadened host access:

- unioning environment and ``allow_from`` unconditionally authorised anyone
  named in either;
- letting the environment *win* over a present ``allow_from`` authorised a user
  the adapter drops at intake. With ``allow_from: ["1001"]`` and
  ``TELEGRAM_ALLOWED_USERS=2002``, Hermes never delivers a message from 2002,
  yet the Mini App would have let 2002 in.

Intersecting is at least as strict as either gate taken alone, which is the
only property that makes "may narrow, never broaden" true rather than intended.

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
  not open one person's learning record to everyone, so a wildcard grants
  nothing here and the operator must name IDs. It does *remove* the intake
  gate's bound, exactly as it does in Hermes — so a wildcard beside an
  environment allowlist still authorises the users that allowlist names, rather
  than denying them for the operator's choice of shorthand. A wildcard as the
  only allowlist authorises nobody.
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
from dataclasses import dataclass
from typing import Any

#: Environment allowlists whose mere presence makes the runner gate decide a DM
#: from the environment rather than from adapter configuration.
ENV_ALLOWLISTS = (
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_CHATS",
    "GATEWAY_ALLOWED_USERS",
)

#: Of those, the ones that authorise a *direct message* sender.
_ENV_DM_ALLOWLISTS = ("TELEGRAM_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS")

#: The adapter-configured DM allowlist. When present it is the intake gate's
#: sole authority, so it is an upper bound on Mini App access.
#: ``allow_admin_from`` is absent on purpose — see the module docstring.
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
    """True when the runner gate bounds a DM by the environment allowlists."""
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


@dataclass(frozen=True)
class AllowFrom:
    """The adapter's ``allow_from`` as the intake gate reads it.

    ``present`` mirrors Hermes' ``is not None`` test, which is why it is a
    separate field from the set: ``allow_from: []`` is *present* and authorises
    nobody, while an absent key imposes no bound at all. Collapsing the two
    into "empty set" would turn a deliberate lockout into a fallback.
    """

    present: bool
    #: True when the setting contains ``*``. The wildcard lets everyone past
    #: intake, so it removes this gate's bound — but it grants nothing on its
    #: own, because a wildcard must never open a personal learning record.
    wildcard: bool
    ids: frozenset[str]

    @property
    def bound(self) -> frozenset[str] | None:
        """The users this gate permits, or ``None`` for "no bound"."""
        if not self.present or self.wildcard:
            return None
        return self.ids


def configured_allow_from(host_config: Mapping[str, Any] | None) -> AllowFrom:
    """Read ``allow_from`` from every shape Hermes accepts."""
    platform = _telegram_platform_config(host_config)
    if _CONFIG_DM_ALLOWLIST not in platform:
        return AllowFrom(present=False, wildcard=False, ids=frozenset())

    raw = platform[_CONFIG_DM_ALLOWLIST]
    items = raw.split(",") if isinstance(raw, str) else raw
    wildcard = isinstance(items, (list, tuple, set, frozenset)) and any(
        str(item).strip() == "*" for item in items
    )
    return AllowFrom(present=True, wildcard=wildcard, ids=frozenset(_identifiers(raw)))


def env_allowed_users(env: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """The runner gate's DM allowlist, or ``None`` when it imposes no bound."""
    environment = os.environ if env is None else env
    if not any_env_allowlist_configured(environment):
        return None
    allowed: set[str] = set()
    for name in _ENV_DM_ALLOWLISTS:
        allowed |= _identifiers(_env_value(environment, name))
    return frozenset(allowed)


def profile_allowed_users(
    *,
    env: Mapping[str, str] | None = None,
    host_config: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """The profile's effective direct-message Telegram allowlist.

    The intersection of Hermes' two gates — adapter intake and runner
    authorisation — so the result can never exceed either. A gate that imposes
    no bound contributes nothing; when *neither* bounds anything, the answer is
    nobody, because an unconfigured deployment is closed.
    """
    bounds = [
        bound
        for bound in (configured_allow_from(host_config).bound, env_allowed_users(env))
        if bound is not None
    ]
    if not bounds:
        return frozenset()

    allowed = bounds[0]
    for bound in bounds[1:]:
        allowed &= bound
    return frozenset(allowed)


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
