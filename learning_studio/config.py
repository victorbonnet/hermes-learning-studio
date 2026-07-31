"""Behavioural configuration, read from Hermes ``config.yaml``.

Everything here is behaviour, not secrets: retention windows, storage
pragmas, compatibility policy, and managed-image safety limits. Secrets
belong in ``.env`` and this plugin has none.

The whole section is validated and **fails closed**. A malformed value is
raised as :class:`ConfigError` rather than quietly replaced with a default,
because every setting here governs retention, isolation, privacy, or resource
safety — decisions a silent fallback must never make for the operator.
An unknown key is an error too: a typo in ``persist_accessibility_needs``
that degraded to "off" would look identical to the setting working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .components import MINIMUM_REQUEST_BYTES
from .models import MAX_VALUE_CHARS

#: The one ``config.yaml`` section this plugin reads. Nothing else in the
#: profile's configuration is inspected.
CONFIG_SECTION = "learning_studio"


class ConfigError(ValueError):
    """Raised when the ``learning_studio`` config section is malformed."""


def _bounded_int(raw: Any, key: str, low: int, high: int) -> int:
    # bool is an int subclass; `retention_days: true` is a mistake, not a 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{CONFIG_SECTION}.{key} must be an integer, got {type(raw).__name__}")
    if not low <= raw <= high:
        raise ConfigError(f"{CONFIG_SECTION}.{key} must be between {low} and {high}, got {raw}")
    return raw


def _strict_bool(raw: Any, key: str) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError(f"{CONFIG_SECTION}.{key} must be true or false, got {raw!r}")
    return raw


def _choice(raw: Any, key: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(raw, str) or raw.lower() not in allowed:
        raise ConfigError(
            f"{CONFIG_SECTION}.{key} must be one of {', '.join(allowed)}, got {raw!r}"
        )
    return raw.lower()


def _telegram_user_ids(raw: Any, key: str) -> tuple[str, ...]:
    """Validate a list of Telegram user IDs used to *narrow* profile access.

    Strict on purpose. This list can only ever remove people (see
    :mod:`learning_studio.authorization`), but a malformed entry that was
    silently dropped would turn "restrict to these two accounts" into
    "restrict to one", which looks identical to working.
    """
    if not isinstance(raw, list):
        raise ConfigError(f"{CONFIG_SECTION}.{key} must be a list of Telegram user IDs")
    out: list[str] = []
    for item in raw:
        text = str(item).strip() if not isinstance(item, bool) else ""
        if not text.isdigit() or int(text) <= 0:
            raise ConfigError(
                f"{CONFIG_SECTION}.{key} entries must be positive numeric Telegram user IDs, "
                f"got {item!r}. Use the numeric ID, never an @username."
            )
        out.append(str(int(text)))
    return tuple(dict.fromkeys(out))


def _context_mapping(raw: Any, key: str) -> dict[str, Any]:
    """Validate a ``field: value`` mapping of learning-context fields."""
    from .models import validate_context_payload

    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_SECTION}.{key} must be a mapping")
    try:
        return validate_context_payload(raw)
    except ValueError as exc:
        raise ConfigError(f"{CONFIG_SECTION}.{key}: {exc}") from exc


@dataclass(frozen=True)
class LearningStudioConfig:
    """Validated ``learning_studio`` settings with safe defaults."""

    #: How long an unconfirmed temporary context stays readable. Temporary
    #: context is conversational evidence, not a record; it expires.
    temporary_context_ttl_hours: int = 72

    #: Upper bound on confirmed tracks per learner. Prevents an agent loop
    #: from turning a conversation into hundreds of durable records.
    max_tracks_per_learner: int = 20

    #: SQLite lock wait. Bounded so a stuck writer surfaces as an error
    #: rather than an indefinite hang inside a tool call.
    busy_timeout_ms: int = 5000

    #: ``wal`` unless the filesystem cannot support it (some network mounts).
    journal_mode: str = "wal"

    #: How many independent observations before repeated evidence may become
    #: a memory candidate. Below this, evidence stays in temporary context.
    memory_candidate_min_evidence: int = 3

    #: Deprecated compatibility switch. Accessibility needs are always
    #: session-only and never stored. ``False`` additionally refuses the old
    #: model-supplied ``accessibility_consent`` audit payload; ``True`` accepts
    #: and validates that payload for the response only. Neither value grants
    #: storage authority. Kept so existing profile configs continue to load.
    allow_durable_accessibility_needs: bool = True

    #: Longest single context value accepted, in characters. Bounded above by
    #: the tool schema's own ``maxLength``: the schema is a fixed string the
    #: model sees, so configuration may only *tighten* the limit, never raise
    #: it past what the advertised contract allows.
    max_context_value_chars: int = 2000

    #: Managed images are bounded before decode and again by decoded geometry.
    #: These are behavioural safety limits, so they belong in config.yaml.
    max_asset_bytes: int = 10 * 1024 * 1024
    max_asset_width: int = 8192
    max_asset_height: int = 8192
    max_asset_pixels: int = 40_000_000

    # ── Telegram Mini App API ─────────────────────────────────────────────
    #
    # Behaviour, not secrets: no token, no domain, no URL. The bot token stays
    # in ``.env`` where Hermes already keeps it, and is never copied here.

    #: How long a Mini App session stays usable. Short by default — a session
    #: is a bearer credential held by a webview, and an exercise is minutes of
    #: work, not hours.
    mini_app_session_ttl_seconds: int = 1800

    #: How old signed ``initData`` may be when a session is opened. Telegram
    #: signs once at launch, so this is the replay window for a captured
    #: payload and it is deliberately tight.
    mini_app_init_data_max_age_seconds: int = 300

    #: Largest accepted request body. An answer is a short JSON object; this is
    #: the ceiling that keeps a submission endpoint from being a memory
    #: exhaustion endpoint.
    #:
    #: It cannot be configured below
    #: :data:`learning_studio.components.MINIMUM_REQUEST_BYTES`, which is what the
    #: longest *accepted* manifest needs in order to be answerable at all.
    mini_app_max_request_bytes: int = 16 * 1024

    #: Sliding-window rate limit, applied per Telegram user and per session.
    mini_app_rate_limit_requests: int = 60
    mini_app_rate_limit_window_seconds: int = 60

    #: Upper bound on concurrently held sessions, so the in-memory store
    #: cannot grow without limit.
    mini_app_max_sessions: int = 500

    #: Optional *narrowing* of the profile's Telegram allowlist. Empty means
    #: "no additional restriction"; it can never add a user the profile does
    #: not already allow.
    mini_app_allowed_telegram_users: tuple[str, ...] = ()

    #: Context values that apply to the whole profile (``profile_config``
    #: provenance) — e.g. an explanation language the operator has set.
    profile_context: dict[str, Any] = field(default_factory=dict)

    #: Last-resort values (``default`` provenance). Never overwrite anything
    #: stored or explicit. Empty by default: this plugin has no opinion about
    #: what anyone is studying.
    defaults: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, cfg: Any) -> LearningStudioConfig:
        """Build from a full Hermes config mapping.

        Accepts the whole ``config.yaml`` dict and reads only
        :data:`CONFIG_SECTION` from it. A missing or empty section yields
        defaults; a section of the wrong type is an error.
        """
        if not isinstance(cfg, dict):
            return cls()
        section = cfg.get(CONFIG_SECTION)
        if section is None:
            return cls()
        if not isinstance(section, dict):
            raise ConfigError(f"{CONFIG_SECTION} must be a mapping, got {type(section).__name__}")

        unknown = set(section) - _KNOWN_KEYS
        if unknown:
            raise ConfigError(
                f"unknown {CONFIG_SECTION} settings: {', '.join(sorted(unknown))}. "
                "Remove them or correct the spelling — a misspelled privacy, retention, or "
                "resource-safety setting is indistinguishable from one that is switched off."
            )

        values: dict[str, Any] = {}
        for key, raw in section.items():
            values[key] = _PARSERS[key](raw, key)
        return cls(**values)


_PARSERS: dict[str, Any] = {
    "temporary_context_ttl_hours": lambda raw, key: _bounded_int(raw, key, 1, 8760),
    "max_tracks_per_learner": lambda raw, key: _bounded_int(raw, key, 1, 200),
    "busy_timeout_ms": lambda raw, key: _bounded_int(raw, key, 100, 60_000),
    "journal_mode": lambda raw, key: _choice(raw, key, ("wal", "delete", "truncate")),
    "memory_candidate_min_evidence": lambda raw, key: _bounded_int(raw, key, 2, 50),
    "allow_durable_accessibility_needs": _strict_bool,
    # Upper bound is the schema ceiling: config may tighten, never exceed.
    "max_context_value_chars": lambda raw, key: _bounded_int(raw, key, 80, MAX_VALUE_CHARS),
    "max_asset_bytes": lambda raw, key: _bounded_int(raw, key, 1024, 100 * 1024 * 1024),
    "max_asset_width": lambda raw, key: _bounded_int(raw, key, 1, 32_768),
    "max_asset_height": lambda raw, key: _bounded_int(raw, key, 1, 32_768),
    "max_asset_pixels": lambda raw, key: _bounded_int(raw, key, 1, 200_000_000),
    "mini_app_session_ttl_seconds": lambda raw, key: _bounded_int(raw, key, 60, 86_400),
    "mini_app_init_data_max_age_seconds": lambda raw, key: _bounded_int(raw, key, 30, 3_600),
    # The floor is derived from the component contract rather than chosen: below
    # it, a manifest the registry accepts has no response any client could send.
    # See `components.MINIMUM_REQUEST_BYTES`.
    "mini_app_max_request_bytes": lambda raw, key: _bounded_int(
        raw, key, MINIMUM_REQUEST_BYTES, 1_048_576
    ),
    "mini_app_rate_limit_requests": lambda raw, key: _bounded_int(raw, key, 1, 10_000),
    "mini_app_rate_limit_window_seconds": lambda raw, key: _bounded_int(raw, key, 1, 3_600),
    "mini_app_max_sessions": lambda raw, key: _bounded_int(raw, key, 1, 100_000),
    "mini_app_allowed_telegram_users": _telegram_user_ids,
    "profile_context": _context_mapping,
    "defaults": _context_mapping,
}

_KNOWN_KEYS = frozenset(_PARSERS)


def load_raw_config() -> dict[str, Any]:
    """Return the whole profile configuration mapping, or ``{}`` with no host.

    Used for exactly one thing: reading the profile's *own* Telegram
    allowlist (``platforms.telegram.extra``) so Mini App access can be
    intersected with it. This plugin never writes host configuration and reads
    nothing else out of it.

    Failure semantics match :func:`load_config` — an absent host means "no
    operator configuration exists", while a host whose configuration cannot be
    read is an error, because a silently empty allowlist would look exactly
    like a correctly locked-down one right up to the moment it isn't.
    """
    try:
        from hermes_cli.config import load_config as _load
    except ImportError:
        return {}

    try:
        raw = _load()
    except Exception as exc:
        raise ConfigError(
            "The Hermes configuration could not be read, so the Learning Studio cannot "
            "confirm this profile's Telegram allowlist and has authorised nobody. "
            f"Underlying cause: {type(exc).__name__}."
        ) from exc

    return raw if isinstance(raw, dict) else {}


def load_config() -> LearningStudioConfig:
    """Load and validate the plugin's settings from the active profile.

    Uses the host's ``load_config()`` so profile switching, caching, and the
    config schema stay the host's business.

    The two failure modes are deliberately not the same:

    - **The host is absent** — no Hermes process, so there is no operator
      configuration to honour and the standalone defaults are correct. This
      is the test, build, and bare-import path.
    - **The host is present but the read failed** — a permission error, an
      unreadable file, a YAML syntax error. Defaults here would silently
      convert a strict retention or validation setting into its default at
      exactly the moment nobody is watching. Raise instead.
    """
    try:
        from hermes_cli.config import load_config as _load
    except ImportError:
        return LearningStudioConfig()

    try:
        raw = _load()
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            "The Hermes configuration could not be read, so the Learning Studio cannot "
            "confirm this profile's retention and consent settings and has done nothing. "
            f"Underlying cause: {type(exc).__name__}."
        ) from exc

    return LearningStudioConfig.from_mapping(raw)
