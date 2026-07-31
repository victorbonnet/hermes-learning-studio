"""Transport-level defences: headers, body limits, rate limits, safe logs.

Kept apart from the routes so each rule is one testable function rather than a
line of middleware nobody reads. None of it depends on FastAPI.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("learning_studio.web")

#: Sent on every response, success or failure.
#:
#: ``default-src 'none'`` with per-directive opt-ins is the shape to keep. This
#: is the policy for the *API*: it serves JSON and images and nothing else, so
#: scripts and styles are refused outright.
#:
#: The frontend needs more than that, and it does not get it here. The document
#: carries its own, wider policy —
#: :data:`learning_studio.web.static_files.DOCUMENT_CONTENT_SECURITY_POLICY` —
#: which is the only response that may execute anything. Keeping the two apart
#: means the API's policy never had to be widened for the renderer at all.
#:
#: ``frame-ancestors`` is what lets Telegram — and only Telegram — embed the
#: Mini App. ``X-Frame-Options`` is deliberately absent: it cannot express
#: "this one origin", so setting it would either break the Mini App or lie.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors https://web.telegram.org https://telegram.org; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "script-src 'none'; "
    "style-src 'none'; "
    "object-src 'none'"
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Learning content and progress are personal and none of it is cacheable
    # by anything between the tunnel and the webview.
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
    # Meaningful once the tunnel terminates TLS, harmless before then.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class RequestTooLarge(Exception):
    """The request body exceeded the configured ceiling."""


class RateLimited(Exception):
    """The caller has used their allowance for the current window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many requests.")
        self.retry_after = max(1, int(retry_after))


def enforce_declared_length(declared_length: str | None, limit: int) -> None:
    """Refuse an oversized body *before* a single byte is read.

    This is the half that matters for resource safety. Reading the body and
    then measuring it — which this module did first — means the ceiling is
    enforced only after the whole request is already in memory, so a caller who
    declares a gigabyte gets a gigabyte buffered and *then* a 413. Checking the
    declaration first turns that into a refusal that costs nothing.

    A malformed declaration is refused rather than ignored: a
    ``Content-Length`` that is not a number is not a request this API needs to
    understand, and treating it as "absent" would hand the caller the
    unmeasured path for free.
    """
    if not declared_length:
        return
    try:
        declared = int(declared_length)
    except ValueError as exc:
        raise RequestTooLarge("malformed content-length") from exc
    if declared < 0:
        raise RequestTooLarge("negative content-length")
    if declared > limit:
        raise RequestTooLarge("declared body too large")


def enforce_measured_length(size: int, limit: int) -> None:
    """Refuse a body that turned out larger than it declared, or declared nothing.

    Kept as a separate check because a chunked request carries no
    ``Content-Length`` at all, and a dishonest one carries the wrong value. The
    caller applies this *as it reads*, so the cumulative size can never exceed
    the limit by more than one chunk.
    """
    if size > limit:
        raise RequestTooLarge("body too large")


class RateLimiter:
    """Sliding-window rate limiting, keyed by caller.

    A dict of timestamp deques: small, exact, and adequate for a single-process
    API whose whole population is an allowlist of a few people. It bounds
    memory by discarding a key's window once it empties.
    """

    def __init__(self, *, limit: int, window_seconds: int, clock) -> None:
        self._limit = int(limit)
        self._window = float(window_seconds)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> None:
        """Record one request against ``key``, or raise :class:`RateLimited`."""
        now = float(self._clock())
        window = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= self._limit:
            raise RateLimited(retry_after=int(window[0] + self._window - now) + 1)
        window.append(now)
        self._prune(cutoff)

    def _prune(self, cutoff: float) -> None:
        empty = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in empty:
            del self._hits[key]


#: The only fields a log line may carry. Anything else is dropped rather than
#: rendered: the fastest way to leak an ``initData`` payload or a session token
#: is a well-meaning ``extra=locals()``.
LOGGABLE_FIELDS = frozenset(
    {
        "event",
        "route",
        "method",
        "status",
        "reason",
        "session_ref",
        "user_ref",
        "experience_ref",
        "asset_ref",
        "authorized",
    }
)

#: Substrings that must never appear in a log line, whatever the field name.
_FORBIDDEN_LOG_SUBSTRINGS = ("init_data=", "hash=", "TELEGRAM_BOT_TOKEN", "Bearer ")


@dataclass(frozen=True)
class LogEvent:
    """One structured, redacted line."""

    fields: dict[str, Any]

    def render(self) -> str:
        return json.dumps(self.fields, sort_keys=True, ensure_ascii=False)


def redacted(**fields: Any) -> LogEvent:
    """Keep the allowlisted fields, stringify them, and drop the rest."""
    kept: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in LOGGABLE_FIELDS or value is None:
            continue
        text = value if isinstance(value, (int, bool)) else str(value)
        if isinstance(text, str) and any(bad in text for bad in _FORBIDDEN_LOG_SUBSTRINGS):
            continue
        kept[key] = text
    return LogEvent(kept)


def log_request(level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, "%s", redacted(**fields).render())


def log_unhandled(exc: BaseException, *, route: str, method: str) -> None:
    """Record an unexpected failure without quoting it.

    An unexpected failure in this API can be holding anything the failing code
    was holding: a filesystem path, a SQL fragment, a bot token read from the
    environment, a component's answer key. ``logger.exception`` — which this
    module used first — writes the message and the traceback into the ordinary
    log, defeating the redaction every other line goes through.

    So no exception text and no traceback are logged. What is recorded is the
    route, the method, the status, and the exception's *class name* — a name a
    programmer chose, not a string built from runtime values.

    **This is a deliberate trade, and it costs something.** Diagnosing a 500
    here means reproducing it, because the traceback is not written anywhere.
    An intermediate design — a second, silenced logger that an operator could
    opt into — was tried and removed: a ``LogRecord`` carrying ``exc_info`` is
    a live object holding the sensitive values, and any component that captures
    records broadly renders it regardless of how that logger is configured.
    pytest's own log capture does exactly that, reaching records through
    ``propagate = False`` and attaching handlers to loggers it did not create.
    A record that is never constructed is the only one that cannot be captured,
    and "the traceback is off unless somebody flips this switch" is a switch
    that gets flipped by accident.
    """
    logger.error(
        "%s",
        redacted(
            event="unhandled_error",
            route=route,
            method=method,
            status=500,
            reason=type(exc).__name__,
        ).render(),
    )


#: Bounds on a submitted answer, applied after the body-size limit. A response
#: is a short value or a small list of them; anything deeper is either a bug or
#: an attempt to make the JSON encoder do the work.
MAX_RESPONSE_CHARS = 4000
MAX_RESPONSE_ITEMS = 100
MAX_RESPONSE_DEPTH = 4


class InvalidResponseValue(Exception):
    """A submitted answer was not a shape this API accepts."""


def validate_response_value(value: Any, *, depth: int = 0) -> Any:
    """Accept a JSON value bounded in size, breadth, and depth."""
    if depth > MAX_RESPONSE_DEPTH:
        raise InvalidResponseValue("response is nested too deeply")

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_RESPONSE_CHARS:
            raise InvalidResponseValue("response is too long")
        return value
    if isinstance(value, list):
        if len(value) > MAX_RESPONSE_ITEMS:
            raise InvalidResponseValue("response has too many items")
        return [validate_response_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_RESPONSE_ITEMS:
            raise InvalidResponseValue("response has too many fields")
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 200:
                raise InvalidResponseValue("response field names must be short strings")
            out[key] = validate_response_value(item, depth=depth + 1)
        return out
    raise InvalidResponseValue("response must be JSON data")
