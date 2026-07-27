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
#:
#: Each rule is deliberately lexical. Nothing here fetches, opens, resolves,
#: decodes, or normalises a match into an executable form — a validator that
#: had to *run* something to decide whether it was safe would be the exact
#: hazard it exists to prevent.
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
    # Schemes with no authority component, which the ``://`` rule above cannot
    # see. Each requires a non-space immediately after the colon so ordinary
    # prose — "the file: notes.md", "Note: see below" — is not caught.
    (
        re.compile(
            r"\b(?:mailto|ftp|ftps|file|tel|sms|ws|wss|ssh|sftp|git|smb|nfs|ldap|irc|"
            r"magnet|gopher|blob|about|chrome|view-source):(?=\S)",
            re.IGNORECASE,
        ),
        "must not contain a URI; describe the source instead of linking to it",
    ),
    # A bare web locator is still a locator. Restricted to a leading ``www.``
    # or a small set of well-known suffixes so that "Node.js" and "e.g." are
    # not mistaken for hosts.
    (
        re.compile(
            r"\bwww\.[a-z0-9-]+\.[a-z]{2,}|"
            r"\b[a-z0-9][a-z0-9-]{1,}\.(?:com|net|org|io|dev|app|edu|gov|mil|info|biz|xyz|"
            r"me|co|ai|sh)\b(?:/\S*)?",
            re.IGNORECASE,
        ),
        "must not contain a web address",
    ),
    (
        re.compile(r"&(?:#x?[0-9a-f]+|lt|gt|quot|apos|amp|nbsp);", re.IGNORECASE),
        "must not contain HTML character references",
    ),
    (
        re.compile(r"@import\b|\bexpression\s*\(|\burl\s*\(", re.IGNORECASE),
        "must not contain stylesheet syntax",
    ),
    # A declaration block, with or without the trailing semicolon. The
    # property name must be at least two characters so that set-builder
    # notation such as ``{x : x > 0}`` is left alone.
    (
        re.compile(r"\{[^{}]*[a-z-]{2,}\s*:\s*[^{};]+;?\s*\}", re.IGNORECASE),
        "must not contain a stylesheet rule",
    ),
    (
        re.compile(r"(?:^|[\s\"'(\[/\\])\.{1,2}[/\\]|%2e%2e", re.IGNORECASE),
        "must not contain a relative path",
    ),
    # Three shapes of absolute path: a multi-segment POSIX path, a
    # single-segment one carrying a file extension, and the Windows and UNC
    # forms. The extension requirement is what lets "/help" and "3 / 4"
    # through while still refusing "/secret.txt".
    (
        re.compile(
            r"(?:^|\s)~?/[A-Za-z0-9._-]+/|"
            r"(?:^|\s)~?/[A-Za-z0-9_-]+\.[A-Za-z0-9]{1,8}\b|"
            r"(?:^|\s)[A-Za-z]:[\\/]|\\\\[A-Za-z0-9]"
        ),
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
        re.compile(
            r"\b(?:proxy-)?authorization\s*:\s*(?:basic|bearer|digest|negotiate)\b|"
            r"\b(?:bearer|basic)\s+[A-Za-z0-9+/._~-]{12,}={0,2}",
            re.IGNORECASE,
        ),
        "must not contain authorization material",
    ),
    # Prefixed token shapes in current circulation. Deliberately a list of
    # *shapes*: no real credential appears here, and none is needed to match
    # one.
    (
        re.compile(
            r"\b(?:sk|pk|rk)[-_](?:[A-Za-z0-9]{1,10}[-_])?[A-Za-z0-9]{16,}|"
            r"\bAKIA[0-9A-Z]{16}\b|"
            r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|"
            r"\bglpat-[A-Za-z0-9_-]{16,}|\bnpm_[A-Za-z0-9]{20,}|"
            r"\bhf_[A-Za-z0-9]{20,}|\bdop_v1_[a-f0-9]{32,}|\bshp(?:at|ss)_[a-f0-9]{20,}|"
            r"\bxox[baprs]-[A-Za-z0-9-]{10,}|\bxapp-[0-9]-[A-Za-z0-9-]{10,}|"
            r"\bAIza[0-9A-Za-z_-]{30,}|\bya29\.[0-9A-Za-z_-]{20,}|"
            r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|"
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
        ),
        "must not contain a secret-looking value",
    ),
)

#: Vocabulary that describes a *person* rather than a component. Applied only
#: to accessibility text, where a diagnosis, a disability label, or a sentence
#: about the learner has no legitimate place — an exercise records what it
#: needs in order to be usable, never a fact about who is using it.
#:
#: Not applied to prompts or content, where these words are ordinary subject
#: matter: a biology item may well ask about glaucoma.
_SENSITIVE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:adhd|add|asd|autis(?:m|tic)|aspergers?|dyslexi[ac]|dyspraxi[ac]|"
            r"dyscalculi[ac]|dysgraphi[ac]|blindness|deafness|glaucoma|cataracts?|"
            r"epilep(?:sy|tic)|tourettes?|cerebral palsy|down syndrome|ptsd|"
            r"anxiety disorder|bipolar|schizophreni[ac]|dementia|alzheimers?)\b",
            re.IGNORECASE,
        ),
        "must not name a diagnosis or condition",
    ),
    (
        re.compile(
            r"\b(?:diagnos(?:is|ed|es|tic)|disabilit(?:y|ies)|disabled|impairment|"
            r"impaired|neurodiver(?:gent|se)|special needs|learning difficult(?:y|ies))\b",
            re.IGNORECASE,
        ),
        "must not describe a disability",
    ),
    (
        re.compile(
            r"\b(?:the\s+)?(?:learner|student|pupil|user|they|he|she)\s+"
            r"(?:has|have|is|are|was|were|cannot|can't|suffers?|struggles?|needs?|"
            r"requires?|finds?)\b",
            re.IGNORECASE,
        ),
        "must describe the component, not the learner",
    ),
)

# The three lexical patterns below are published as strings and compiled from
# those same strings. The advertised JSON Schema emits the string; the runtime
# uses the compiled form. That is what makes "the schema and the validator
# agree" a fact about the code rather than a claim in a comment.

#: Opaque identifiers the author supplies (component ids, option ids, asset
#: references). Lowercase, bounded, and shaped so that nothing path-like,
#: scheme-like, or traversal-like can be spelled with one.
IDENTIFIER_PATTERN = r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$"

#: A conservative BCP-47 subset: language, optional script, optional region.
LOCALE_PATTERN = r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?$"

#: ``2026``, ``2026-07``, or ``2026-07-27``. Enough for provenance, and not a
#: free-text field pretending to be a date.
DATE_PATTERN = r"^[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?$"

_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)
_LOCALE_RE = re.compile(LOCALE_PATTERN)
_DATE_RE = re.compile(DATE_PATTERN)


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


def reject_learner_description(raw: str, label: str) -> str:
    """Refuse accessibility text that describes a person rather than a component.

    Applied on top of :func:`safe_text`, and only to accessibility fields.
    "Alt text: a cross-section of the heart" is what belongs there; "the
    learner has ADHD" is a health record, and an exercise is not the place to
    keep one — nor is a diagnosis something an agent may infer and write down.
    """
    for pattern, explanation in _SENSITIVE_RULES:
        if pattern.search(raw):
            raise UnsafeContent(f"{label} {explanation}")
    return raw


def tokens(text: str) -> list[str]:
    """Word tokens of *text*, casefolded, for answer-leak comparison.

    Punctuation and case are discarded so that "acetyl-CoA" and "Acetyl CoA"
    are the same sequence, and so that a full stop cannot hide a leak.
    """
    return re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold(), flags=re.UNICODE)


def contains_token_sequence(haystack: list[str], needle: list[str]) -> bool:
    """True when *needle* appears in *haystack* as a contiguous token run.

    Token-boundary rather than substring, in both directions: a two-character
    answer such as ``Na`` must not match inside "national", and a multi-word
    answer must appear in order and intact rather than as scattered words.
    """
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    span = len(needle)
    return any(
        haystack[index : index + span] == needle
        for index, token in enumerate(haystack)
        if token == first
    )


def normalised_for_comparison(text: str) -> str:
    """Casefolded, whitespace-collapsed form, for exact-equality comparisons."""
    return " ".join(text.split()).casefold()
