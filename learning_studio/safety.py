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

#: The two rules a JSON Schema ``pattern`` can express exactly. Declared here
#: as strings, compiled into the rule table below, *and* emitted into the
#: advertised schema by :func:`text_pattern` — one declaration, three uses, so
#: the schema and the validator cannot say different things about them.
MARKUP_PATTERN = r"<[a-zA-Z/!?]"
SCHEME_URL_PATTERN = r"[a-zA-Z][a-zA-Z0-9+.-]*:\s*//"

#: Every rule, as ``(regex, explanation)``. Ordered so the most specific and
#: most actionable message wins for a string that trips several.
#:
#: Each rule is deliberately lexical. Nothing here fetches, opens, resolves,
#: decodes, or normalises a match into an executable form — a validator that
#: had to *run* something to decide whether it was safe would be the exact
#: hazard it exists to prevent.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(MARKUP_PATTERN),
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
        re.compile(SCHEME_URL_PATTERN, re.IGNORECASE),
        "must not contain a URL; describe the source instead of linking to it",
    ),
    # URI syntax, generically. A finite list of schemes is a list somebody
    # eventually gets past — ``intent:`` did. The shape is what is refused:
    # a scheme name, a colon, and a non-space immediately after it. Ordinary
    # prose puts a space after its colons ("Note: see below", "the file:
    # notes.md"), and a leading digit excludes times and ratios.
    (
        # RFC 3986 scheme grammar: one letter, then any of letter/digit/+/-/.
        # — so a *one-letter* scheme such as ``x:`` is a scheme too. The
        # payload must start with something that is neither a space nor a
        # digit, which is what keeps "12:30", "3:4" and "H:1" out of it.
        re.compile(r"\b[a-z][a-z0-9+.-]{0,31}:(?=[^\s:\d])", re.IGNORECASE),
        "must not contain a URI; describe the source instead of linking to it",
    ),
    # A bare web locator is still a locator. Three shapes, chosen so that
    # ``Node.js`` and ``3.14`` are not mistaken for hosts: anything under
    # ``www.``, a domain carrying a path, and a three-or-more-label name.
    (
        # Four shapes, and no list of top-level domains anywhere: a list has
        # to be maintained, and ``.museum`` is exactly what falls off the end
        # of one. What is matched is the *shape* of a hostname.
        #
        # A two-label name is the hard case, because ``Node.js`` and
        # ``example.fr`` are the same shape. The discriminator is the length
        # of the last label: three or more letters is a domain
        # (``example.museum``, ``malware.tech``), two is left alone unless it
        # carries a path or a ``www.``. That keeps file-extension prose —
        # ``Node.js``, ``index.md`` — readable while refusing the locators.
        re.compile(
            r"\bwww\.[a-z0-9-]+\.[a-z]{2,}|"
            r"\b[a-z0-9][a-z0-9-]{0,62}\.[a-z]{2,24}/\S|"
            r"\b[a-z0-9][a-z0-9-]{1,62}\.[a-z]{3,24}\b|"
            r"\b[a-z0-9][a-z0-9-]{1,62}(?:\.[a-z0-9][a-z0-9-]{1,62}){2,}\b",
            re.IGNORECASE,
        ),
        "must not contain a web address",
    ),
    # An address with a path or a port is a locator whether or not it has a
    # name. A bare dotted-quad on its own is left alone: it is indistinguish-
    # able from an ordinary sequence of numbers.
    (
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?/|\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}"),
        "must not contain a network address",
    ),
    (
        # A bracketed IPv6 literal, with or without a port or a path. At least
        # three colon-separated groups, so an interval such as ``[1:2]`` is
        # not mistaken for one.
        re.compile(r"\[[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,}\](?::\d{1,5})?", re.IGNORECASE),
        "must not contain a network address",
    ),
    (
        re.compile(r"\b[a-z][a-z0-9.-]{1,62}:\d{2,5}(?:/|\b)", re.IGNORECASE),
        "must not contain a host and port",
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
        re.compile(r"(?:^|[\s\"'(\[{«‹“”‘’/\\])\.{1,2}[/\\]|%2e%2e", re.IGNORECASE),
        "must not contain a relative path",
    ),
    # Every shape of path: POSIX absolute (one segment or many, extension or
    # not), home-relative, Windows, and UNC.
    #
    # ``/secret`` and ``/help`` are the same string, so a rule that refuses
    # one refuses both. The contract says manifest strings contain no paths,
    # and a leading slash is what a path looks like — so a slash-prefixed
    # command has to be written without the slash ("the help command"). A
    # slash with a space after it is division and is untouched.
    (
        # The leading context is any opening punctuation, not only whitespace:
        # ``(/etc/passwd)`` and ``"/etc/hosts"`` are paths however they are
        # wrapped, and the quotes may be the typographic ones a word processor
        # produces.
        re.compile(
            r"(?:^|[\s\"'(\[{«‹“”‘’])~?/[A-Za-z0-9._-]+|"
            r"(?:^|[\s\"'(\[{«‹“”‘’])[A-Za-z]:[\\/]|\\\\[A-Za-z0-9]"
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


#: The subset of the rules above that a JSON Schema ``pattern`` can express
#: exactly, as a negative lookahead. Kept here, beside the rules themselves,
#: so the advertised constraint and the enforced one are one declaration:
#: markup and scheme-qualified URLs are refused on both sides.
#:
#: Only these two. The remaining rules span newlines, need alternation the
#: two regex dialects disagree about, or would turn into a pattern no
#: provider could be relied on to compile — and a schema constraint that is
#: *nearly* right is worse than one that is honestly absent, because it
#: invites a reader to stop checking.
_EXPRESSIBLE_RULES: tuple[str, ...] = (MARKUP_PATTERN, SCHEME_URL_PATTERN)


def text_pattern(*, multiline: bool = False) -> str:
    r"""The ``pattern`` a bounded text field advertises.

    Requires a non-whitespace character, because the runtime trims before it
    measures and ``"   "`` is an empty string to it, and refuses the two
    safety rules a pattern can state exactly.

    Written with ``[\s\S]`` rather than ``.`` so the lookaheads cross
    newlines. ``.`` stops at one in both regex dialects, which would leave a
    multiline passage able to carry markup on its second line — advertised as
    valid and then refused, which is the disagreement this exists to close.
    The *multiline* argument is kept for callers that describe their field,
    and deliberately changes nothing: the rules are the same either way.
    """
    del multiline
    refusals = "".join(f"(?![\\s\\S]*{rule})" for rule in _EXPRESSIBLE_RULES)
    return rf"^(?=[\s\S]*\S){refusals}"


def expressible_rule_summary() -> str:
    """Prose naming what the schema pattern enforces, for a field description."""
    return (
        "Plain text: no markup and no URLs. Filesystem paths, other URI schemes, "
        "stylesheet syntax and credential-shaped values are refused too, by the "
        "same validator, though a JSON Schema pattern cannot express them."
    )


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

    # Measured before trimming, because the advertised ``maxLength`` applies
    # to the string as sent. Trimming first would let a title 200 characters
    # long plus four spaces be refused by the schema and accepted by the
    # builder — the same value, two answers, depending which door it came
    # through.
    if len(raw) > max_chars:
        raise UnsafeContent(f"{label} must be at most {max_chars} characters")

    text = unicodedata.normalize("NFC", raw).strip()

    if _CONTROL_RE.search(text):
        raise UnsafeContent(f"{label} must not contain control characters")
    if _INVISIBLE_RE.search(text):
        raise UnsafeContent(f"{label} must not contain invisible or bidirectional characters")
    if not multiline and ("\n" in text or "\r" in text):
        raise UnsafeContent(f"{label} must be a single line")
    if len(text) < min_chars:
        raise UnsafeContent(f"{label} must be at least {min_chars} character(s)")
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
    # Not trimmed. The advertised pattern anchors at both ends, so `" item "`
    # is invalid there; silently accepting it here would mean the schema and
    # the builder disagreeing about the same string.
    if not _IDENTIFIER_RE.match(raw):
        raise UnsafeContent(
            f"{label} must be 1-64 characters of lowercase letters, digits, '-' or '_', "
            "with no surrounding spaces"
        )
    return raw


def safe_locale(raw: Any, label: str) -> str:
    """Validate a language tag: ``en``, ``pt-BR``, ``zh-Hant-TW``."""
    if not isinstance(raw, str):
        raise UnsafeContent(f"{label} must be a string")
    if not _LOCALE_RE.match(raw):
        raise UnsafeContent(
            f"{label} must be a language tag such as 'en', 'pt-BR', or 'zh-Hant-TW', "
            "with no surrounding spaces"
        )
    return raw


def safe_date(raw: Any, label: str) -> str:
    """Validate a publication date as ``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD``."""
    if not isinstance(raw, str):
        raise UnsafeContent(f"{label} must be a string")
    if not _DATE_RE.match(raw):
        raise UnsafeContent(
            f"{label} must be a date as YYYY, YYYY-MM, or YYYY-MM-DD, with no surrounding spaces"
        )
    return raw


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


def spelled_out_pattern(answer: str) -> re.Pattern[str] | None:
    r"""A regex matching *answer* written with separators between its characters.

    ``Paris`` becomes ``\bP[\W_]*a[\W_]*r[\W_]*i[\W_]*s\b``, which catches
    ``P.a.r.i.s``, ``P-----a-----r-----i-----s``, ``P a r i s`` and
    ``P·a·r·i·s`` alike. The run between characters is **unbounded on
    purpose**: any fixed limit is a number an author can simply exceed, and
    the previous two-separator cap was defeated by typing three dots.

    Unbounded is still safe, because the run may contain only non-word
    characters. It cannot cross a letter or a digit, so ``Na`` does not match
    inside ``national`` and two unrelated words cannot be read as one
    obfuscated one. Word boundaries are anchored at each end whenever the
    answer starts or ends with a word character.

    Returns ``None`` where the rule would say nothing useful: fewer than two
    significant characters, or an answer that is only digits — ``4.2`` is a
    different number from ``42``, not a disguise for it.
    """
    characters = [
        character
        for character in unicodedata.normalize("NFKC", answer).casefold()
        if not character.isspace()
    ]
    if len(characters) < 2:
        return None
    if all(character.isdigit() for character in characters):
        return None
    if any(character.isalnum() for character in characters) and len(characters) < 3:
        return None

    body = r"[\W_]*".join(re.escape(character) for character in characters)
    prefix = r"\b" if characters[0].isalnum() else ""
    suffix = r"\b" if characters[-1].isalnum() else ""
    return re.compile(rf"{prefix}{body}{suffix}", re.IGNORECASE)


def symbol_form(text: str) -> str:
    """The comparable form of a string that contains no word characters.

    Tokenising ``+`` or ``===`` yields nothing, so a token comparison cannot
    see them at all — which is how a symbol-only answer used to be printable
    in its own prompt. Compared as a normalised, whitespace-free string
    instead; a symbol is not a word, so there are no boundaries to respect.
    """
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


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
