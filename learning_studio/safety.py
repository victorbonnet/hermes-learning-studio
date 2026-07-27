"""Content safety for every string that enters a learning experience.

An experience manifest is authored by a language model and stored so that a
later runtime can render it. That makes every string here *untrusted content
destined for a UI*, which is the exact shape of an injection sink. The rules
below are therefore applied to learner-visible and evaluator-only text alike:
hidden fields are shown to nobody today, but they are one feature away from
being rendered in a review screen, and a store that already holds markup is a
store that has to be sanitised on the way out forever.

The design rule is **inert text or nothing**:

- No markup. A ``<`` immediately followed by a letter, ``/``, ``!`` or ``?`` is
  refused. A spaced comparison (``a < b``) is fine, which is what mathematical
  and code prose actually looks like; ``a<b`` has to be written with spaces.
- No schemes, no URLs, no filesystem paths. The manifest references sources by
  bounded descriptive metadata and assets by opaque identifier. There is
  nothing to fetch, so nothing may look fetchable.
- No credential-shaped values. ``password: hunter2`` is refused; "what makes a
  strong password?" is not, because the rule matches an assignment of a value,
  not the vocabulary of the subject.
- No invisible or bidirectional characters. Text that renders differently from
  how it reads is a spoofing tool, not content.

**A known limitation, stated plainly.** These rules make it impossible to ship
HTML, CSS, or JavaScript *as subject matter* through the manifest: a lesson on
the box model cannot put ``<div>`` in a prompt. That is a deliberate trade —
this PR has no renderer, and a store that accepts markup before a renderer
exists is a store that will be read by one that forgets to escape it. Those
subjects are still teachable in conversation, which is where the skill already
delivers exercises. Lifting the restriction needs a reviewed, explicitly
escaped inert-code channel, and that belongs with the renderer that would have
to honour it.

Every check is a fixed rule over the string. Nothing here asks a model whether
something is safe.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any


class UnsafeContent(ValueError):
    """A string failed a content-safety rule. The message is agent-safe."""


#: C0/C1 controls, minus tab/newline/carriage-return which multiline text needs.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: Zero-width joiners, directional overrides, and the byte-order mark. Text
#: that displays differently from how it reads cannot be reviewed. Declared as
#: codepoint ranges rather than literals: spelled out, this character class
#: would be invisible in the source that defines it.
_INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x200B, 0x200F),  # zero-width space through right-to-left mark
    (0x202A, 0x202E),  # embedding and override controls
    (0x2060, 0x2064),  # word joiner and invisible operators
    (0x2066, 0x2069),  # isolate controls
    (0xFEFF, 0xFEFF),  # byte-order mark
)

_INVISIBLE_RE = re.compile(
    "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _INVISIBLE_RANGES) + "]"
)

#: Every rule, as ``(regex, explanation)``. Ordered so the most specific and
#: most actionable message wins for a string that trips several.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"<[a-zA-Z/!?]"),
        "must not contain markup; write the content as plain text "
        "(a comparison needs spaces around '<')",
    ),
    (
        re.compile(r"\bon[a-z]{3,20}\s*=", re.IGNORECASE),
        "must not contain an event-handler attribute",
    ),
    (
        re.compile(r"\b(?:javascript|vbscript)\s*:", re.IGNORECASE),
        "must not contain a script URL",
    ),
    (
        re.compile(
            r"\bdata:\s*(?:[a-z][a-z0-9!#$&^_+.-]*/[a-z0-9!#$&^_+.-]+|;base64|,)",
            re.IGNORECASE,
        ),
        "must not contain a data URL",
    ),
    (
        re.compile(r"\b[a-z][a-z0-9+.-]*:\s*//", re.IGNORECASE),
        "must not contain a URL; describe the source instead of linking to it",
    ),
    (
        re.compile(r"&(?:#x?[0-9a-f]+|lt|gt|quot|apos|amp|nbsp);", re.IGNORECASE),
        "must not contain HTML character references",
    ),
    (
        re.compile(r"@import\b|\bexpression\s*\(|\burl\s*\(", re.IGNORECASE),
        "must not contain stylesheet syntax",
    ),
    (
        re.compile(r"\{[^{}]*[a-z-]+\s*:\s*[^{};]+;[^{}]*\}", re.IGNORECASE),
        "must not contain a stylesheet rule",
    ),
    (
        re.compile(r"(?:^|[\s\"'(\[/\\])\.\.[/\\]|%2e%2e", re.IGNORECASE),
        "must not contain a relative path",
    ),
    (
        re.compile(r"(?:^|\s)~?/[A-Za-z0-9._-]+/|(?:^|\s)[A-Za-z]:[\\/]|\\\\[A-Za-z0-9]"),
        "must not contain a filesystem path",
    ),
    (
        re.compile(r"-----BEGIN[A-Z ]*(?:KEY|CERTIFICATE)"),
        "must not contain key material",
    ),
    (
        re.compile(
            r"\b(?:api[_-]?keys?|secrets?|passwords?|passwd|access[_-]?tokens?|"
            r"auth[_-]?tokens?|client[_-]?secrets?|private[_-]?keys?|credentials?)\b"
            r"\s*[:=]\s*\S",
            re.IGNORECASE,
        ),
        "must not contain a credential",
    ),
    (
        re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE),
        "must not contain an authorization header",
    ),
    (
        re.compile(
            r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}|\bAKIA[0-9A-Z]{16}\b|"
            r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[baprs]-[A-Za-z0-9-]{10,}|"
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
        ),
        "must not contain a secret-looking value",
    ),
)

#: Opaque identifiers the author supplies (component ids, option ids, asset
#: references). Lowercase, bounded, and shaped so that nothing path-like,
#: scheme-like, or traversal-like can be spelled with one.
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$")

#: A conservative BCP-47 subset: language, optional script, optional region.
_LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?$")

#: ``2026``, ``2026-07``, or ``2026-07-27``. Enough for provenance, and not a
#: free-text field pretending to be a date.
_DATE_RE = re.compile(r"^[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?$")


def safe_text(
    raw: Any,
    label: str,
    *,
    max_chars: int,
    min_chars: int = 1,
    multiline: bool = False,
) -> str:
    """Validate one string, returning its normalised form.

    Normalisation is NFC and whitespace trimming only. Nothing is *stripped*
    to make it pass: a string containing markup is refused, never silently
    rewritten, because a caller who is told "saved" about content that was
    quietly altered will ship the altered version to a learner.
    """
    if not isinstance(raw, str):
        raise UnsafeContent(f"{label} must be a string")

    text = unicodedata.normalize("NFC", raw).strip()

    if _CONTROL_RE.search(text):
        raise UnsafeContent(f"{label} must not contain control characters")
    if _INVISIBLE_RE.search(text):
        raise UnsafeContent(f"{label} must not contain invisible or bidirectional characters")
    if not multiline and ("\n" in text or "\r" in text):
        raise UnsafeContent(f"{label} must be a single line")
    if len(text) < min_chars:
        raise UnsafeContent(f"{label} must be at least {min_chars} character(s)")
    if len(text) > max_chars:
        raise UnsafeContent(f"{label} must be at most {max_chars} characters")

    for pattern, explanation in _RULES:
        if pattern.search(text):
            raise UnsafeContent(f"{label} {explanation}")
    return text


def safe_identifier(raw: Any, label: str) -> str:
    """Validate an author-supplied opaque identifier.

    These are *labels within one manifest* — an option id, a marker id. They
    are never a primary key and never an authorisation boundary; storage keys
    on generated ids, so a caller cannot address anything by choosing a clever
    identifier here.
    """
    if not isinstance(raw, str):
        raise UnsafeContent(f"{label} must be a string")
    text = raw.strip()
    if not _IDENTIFIER_RE.match(text):
        raise UnsafeContent(
            f"{label} must be 1-64 characters of lowercase letters, digits, '-' or '_'"
        )
    return text


def safe_locale(raw: Any, label: str) -> str:
    """Validate a language tag: ``en``, ``pt-BR``, ``zh-Hant-TW``."""
    if not isinstance(raw, str):
        raise UnsafeContent(f"{label} must be a string")
    text = raw.strip()
    if not _LOCALE_RE.match(text):
        raise UnsafeContent(
            f"{label} must be a language tag such as 'en', 'pt-BR', or 'zh-Hant-TW'"
        )
    return text


def safe_date(raw: Any, label: str) -> str:
    """Validate a publication date as ``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD``."""
    if not isinstance(raw, str):
        raise UnsafeContent(f"{label} must be a string")
    text = raw.strip()
    if not _DATE_RE.match(text):
        raise UnsafeContent(f"{label} must be a date as YYYY, YYYY-MM, or YYYY-MM-DD")
    return text


def serialized_size(value: Any) -> int:
    """Byte length of *value* as compact UTF-8 JSON.

    Used for the manifest size bound. Measured on the serialised form because
    that is what is actually stored and later shipped to a client, so it is
    the number a limit has to be about.
    """
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def normalised_for_comparison(text: str) -> str:
    """Casefolded, whitespace-collapsed form, for answer-leak comparisons."""
    return " ".join(text.split()).casefold()
