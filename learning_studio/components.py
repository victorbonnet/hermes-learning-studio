"""The trusted component registry: what an exercise component may contain.

This module is the data contract. An agent designs a learning experience by
describing components in it, and nothing outside this registry can be stored.

Three properties are load-bearing, and each is enforced structurally rather
than by convention:

1. **Discriminated and closed.** A component's ``type`` selects exactly one
   specification. An unknown type is refused, and so is any field that
   specification does not declare — at every level of nesting.
2. **Visible and hidden are different places, not different names.** The
   learner-facing payload is built by *copying an allowlist* out of the
   validated component. Answer keys, rubrics, scoring rules, hints, per-option
   feedback, branching, and evaluator notes live under ``evaluation``, which
   that allowlist does not mention. Nobody has to remember to delete anything,
   because nothing is ever deleted: the safe payload is constructed, not
   filtered.
3. **One source of truth for what a schema can state.** The JSON Schema the
   model sees and the validation the handler runs are generated from the same
   specifications below — the same bounds, enums, patterns and required
   fields. Where JSON Schema cannot express a rule (an answer that must
   reference a declared option; a passage whose gaps must match its blanks)
   the runtime is stricter and the schema says so in a description, rather
   than pretending. ``tests/test_schema_parity.py`` checks both directions.

The families are deliberately subject-neutral. ``fill_blank`` is as useful for
a chemistry equation as for a Spanish conjugation; ``timeline`` orders the
stages of mitosis as readily as the causes of a war. Nothing here privileges
language learning, programming, or any other discipline, and a component that
only made sense for one subject would not belong.

``code_response`` deserves a specific note: it collects code **as text** and
evaluates it as text. This plugin never compiles, imports, or runs a learner's
answer, and there is no code path here that could — see
``tests/test_experience_security.py``.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .safety import (
    DATE_PATTERN,
    IDENTIFIER_PATTERN,
    LOCALE_PATTERN,
    UnsafeContent,
    contains_token_sequence,
    reject_learner_description,
    safe_date,
    safe_identifier,
    safe_locale,
    safe_text,
    spelled_out_pattern,
    symbol_form,
    text_pattern,
    tokens,
)


class ComponentError(ValueError):
    """A component failed validation. The message is safe to show the agent."""


# ── Bounds ────────────────────────────────────────────────────────────────
#
# Every bound is stated once and used by both the schema and the validator.
# They are sized for a study exercise, not for a document: a component that
# needs more than this is two components.

LABEL_MAX = 200
TEXT_MAX = 1000
PROMPT_MAX = 2000
PASSAGE_MAX = 4000

#: The longest a single submitted response string may be, and the largest word
#: count that can therefore actually be written.
#:
#: These two numbers are one decision, not two, and getting that wrong made a
#: whole class of manifest impossible to complete. The API caps a response string
#: at :data:`learning_studio.web.security.MAX_RESPONSE_CHARS` characters. The
#: shortest text containing *n* words is *n* one-character words joined by
#: *n − 1* spaces, so it needs ``2n − 1`` characters — which means a word bound
#: above ``(chars + 1) // 2`` is a requirement no learner and no client could
#: ever satisfy, whatever they typed.
#:
#: The bound used to be 5,000 words against a 4,000-character ceiling: a
#: ``min_words: 5000`` component validated, stored, and rendered, and then
#: refused every possible answer. It is now derived, and
#: ``tests/test_components.py`` fails if the derivation and the API's own limit
#: ever drift apart.
RESPONSE_CHARS_MAX = 4000
MAX_WORDS = (RESPONSE_CHARS_MAX + 1) // 2

MAX_OPTIONS = 12
MAX_ITEMS = 20
MAX_CATEGORIES = 8
MAX_PAIRS = 15
MAX_BLANKS = 10
MAX_MARKERS = 20
MAX_LABEL_BANK = 24
MAX_REGIONS = 8
MAX_REGION_POINTS = 40
MAX_ROWS = 12
MAX_COLUMNS = 8
MAX_CELLS = 60
MAX_STEPS = 8
MAX_ACCEPTED = 20
MAX_HINTS = 5
MAX_CRITERIA = 8
MAX_LEVELS = 6
MAX_BRANCHES = 6
MAX_PROMPT_LIST = 8

#: How a response is judged. Declared, never improvised — the skill's
#: manifest contract says normalisation is stated rather than assumed, and
#: this enum is the code half of that promise.
SCORING_MODES: tuple[str, ...] = (
    "exact",
    "normalised",
    "numeric",
    "set",
    "ordered",
    "rubric",
    "self_check",
)

#: What a branch reacts to. ``always`` is unconditional, which is why it is
#: the only edge that can form an unescapable cycle — see
#: :func:`learning_studio.manifest.validate_branching`.
BRANCH_CONDITIONS: tuple[str, ...] = ("correct", "incorrect", "always")

#: The keys copied into the learner-facing payload. This tuple *is* the
#: security boundary: a field not named here cannot reach a learner, whatever
#: a future caller does.
LEARNER_VISIBLE_KEYS: tuple[str, ...] = ("id", "type", "prompt", "content", "accessibility")

#: The keys holding everything the learner must not see before grading. Two
#: rather than one so that the judging half — rubric, scoring, hints, feedback,
#: branching, notes — is identical for every component type and can be stated
#: once in the schema. The visible allowlist above is what makes them hidden;
#: splitting them changes the schema's size, not the boundary.
HIDDEN_KEYS: tuple[str, ...] = ("answer", "evaluation")


# ── Field vocabulary ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Field:
    """One declared field: how it validates, and how it appears in the schema."""

    name: str
    required: bool = False
    description: str = ""
    #: When set, this field's schema is emitted once under ``$defs`` and
    #: referenced everywhere it is used. Purely a size optimisation for the
    #: advertised schema: the referenced definition *is* this field's schema,
    #: so what the model is shown and what the validator enforces stay one
    #: thing. Without it the union of 31 component types serialises to more
    #: than 140 KB, which every request would then carry.
    ref: str = ""

    def validate(self, value: Any, path: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def emit(self) -> dict[str, Any]:
        """The schema as it appears in a parent: inline, or a reference.

        A reference carries no description of its own. The description belongs
        to the definition, which is written once — repeating it at all 31 use
        sites is most of what made the inline schema unaffordable.
        """
        return {"$ref": f"#/$defs/{self.ref}"} if self.ref else self.schema()

    def _described(self, schema: dict[str, Any]) -> dict[str, Any]:
        if self.description:
            schema["description"] = self.description
        return schema


@dataclass(frozen=True)
class Text(Field):
    max_chars: int = TEXT_MAX
    multiline: bool = False
    #: Refuse vocabulary that describes a person rather than the component.
    #: Set on accessibility fields, where a diagnosis has no place; left off
    #: for prompts and content, where the same words are ordinary subject
    #: matter — a biology item may legitimately ask about glaucoma.
    about_the_component: bool = False

    def validate(self, value: Any, path: str) -> Any:
        text = safe_text(value, path, max_chars=self.max_chars, multiline=self.multiline)
        if self.about_the_component:
            reject_learner_description(text, path)
        return text

    def schema(self) -> dict[str, Any]:
        # ``minLength`` alone would accept "   ", which the runtime trims to
        # nothing and refuses. The pattern closes that, and carries the two
        # safety rules a schema can state exactly.
        return self._described(
            {
                "type": "string",
                "minLength": 1,
                "maxLength": self.max_chars,
                "pattern": text_pattern(multiline=self.multiline),
            }
        )


@dataclass(frozen=True)
class Ident(Field):
    def validate(self, value: Any, path: str) -> Any:
        return safe_identifier(value, path)

    def schema(self) -> dict[str, Any]:
        # The advertised pattern is the same string the validator compiles, so
        # an identifier the schema accepts is one the runtime accepts.
        return self._described(
            {"type": "string", "minLength": 1, "maxLength": 64, "pattern": IDENTIFIER_PATTERN}
        )

    def emit(self) -> dict[str, Any]:
        """Always a reference. Identifiers appear well over a hundred times in
        the union, and the pattern that makes the schema and the runtime agree
        is longer than the reference that stands in for it."""
        return {"$ref": f"#/$defs/{self.ref or IDENTIFIER_DEF}"}


@dataclass(frozen=True)
class Locale(Field):
    def validate(self, value: Any, path: str) -> Any:
        return safe_locale(value, path)

    def schema(self) -> dict[str, Any]:
        return self._described(
            {"type": "string", "minLength": 2, "maxLength": 16, "pattern": LOCALE_PATTERN}
        )


@dataclass(frozen=True)
class Date(Field):
    """``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD`` — never free text."""

    def validate(self, value: Any, path: str) -> Any:
        return safe_date(value, path)

    def schema(self) -> dict[str, Any]:
        return self._described(
            {"type": "string", "minLength": 4, "maxLength": 10, "pattern": DATE_PATTERN}
        )


@dataclass(frozen=True)
class Bool(Field):
    def validate(self, value: Any, path: str) -> Any:
        if not isinstance(value, bool):
            raise ComponentError(f"{path} must be true or false")
        return value

    def schema(self) -> dict[str, Any]:
        return self._described({"type": "boolean"})


@dataclass(frozen=True)
class Int(Field):
    minimum: int = 0
    maximum: int = 1000

    def validate(self, value: Any, path: str) -> Any:
        # bool is an int subclass; ``points: true`` is a mistake, not a 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ComponentError(f"{path} must be an integer")
        if not self.minimum <= value <= self.maximum:
            raise ComponentError(f"{path} must be between {self.minimum} and {self.maximum}")
        return value

    def schema(self) -> dict[str, Any]:
        return self._described(
            {"type": "integer", "minimum": self.minimum, "maximum": self.maximum}
        )


@dataclass(frozen=True)
class Number(Field):
    """A fraction of the image's width or height, for asset geometry."""

    minimum: float = 0.0
    maximum: float = 1.0

    def validate(self, value: Any, path: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ComponentError(f"{path} must be a number")
        if not self.minimum <= float(value) <= self.maximum:
            raise ComponentError(f"{path} must be between {self.minimum} and {self.maximum}")
        return float(value)

    def schema(self) -> dict[str, Any]:
        return self._described({"type": "number", "minimum": self.minimum, "maximum": self.maximum})


@dataclass(frozen=True)
class Enum(Field):
    choices: tuple[str, ...] = ()

    def validate(self, value: Any, path: str) -> Any:
        if not isinstance(value, str) or value not in self.choices:
            raise ComponentError(f"{path} must be one of: {', '.join(self.choices)}")
        return value

    def schema(self) -> dict[str, Any]:
        return self._described({"type": "string", "enum": list(self.choices)})


@dataclass(frozen=True)
class EnumList(Field):
    """A set drawn from a fixed vocabulary — never free text.

    Used where the alternative would be a description of a person. A closed
    vocabulary is what makes "this exercise needs captions" storable and "the
    learner has a hearing impairment" unwritable.
    """

    choices: tuple[str, ...] = ()

    def validate(self, value: Any, path: str) -> Any:
        items = _as_list(value, path, len(self.choices))
        element = Enum("item", choices=self.choices)
        chosen = [element.validate(item, f"{path}[{i}]") for i, item in enumerate(items)]
        duplicates = sorted({item for item in chosen if chosen.count(item) > 1})
        if duplicates:
            raise ComponentError(f"{path} repeats {', '.join(duplicates)}")
        return chosen

    def schema(self) -> dict[str, Any]:
        return self._described(
            {
                "type": "array",
                "minItems": 1,
                "maxItems": len(self.choices),
                "uniqueItems": True,
                "items": {"type": "string", "enum": list(self.choices)},
            }
        )


@dataclass(frozen=True)
class TextList(Field):
    max_items: int = 10
    max_chars: int = TEXT_MAX
    multiline: bool = False

    def validate(self, value: Any, path: str) -> Any:
        items = _as_list(value, path, self.max_items)
        return [
            safe_text(item, f"{path}[{index}]", max_chars=self.max_chars, multiline=self.multiline)
            for index, item in enumerate(items)
        ]

    def schema(self) -> dict[str, Any]:
        return self._described(
            {
                "type": "array",
                "minItems": 1,
                "maxItems": self.max_items,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": self.max_chars,
                    "pattern": text_pattern(multiline=self.multiline),
                },
            }
        )


@dataclass(frozen=True)
class IdentList(Field):
    max_items: int = MAX_ITEMS

    def validate(self, value: Any, path: str) -> Any:
        items = _as_list(value, path, self.max_items)
        return [safe_identifier(item, f"{path}[{index}]") for index, item in enumerate(items)]

    def schema(self) -> dict[str, Any]:
        # The identifier definition, not a second copy of it — and unique,
        # because every list of identifiers in this registry is either a set
        # or a permutation, and the runtime refuses a repeat in both.
        return self._described(
            {
                "type": "array",
                "minItems": 1,
                "maxItems": self.max_items,
                "uniqueItems": True,
                "items": {"$ref": f"#/$defs/{IDENTIFIER_DEF}"},
            }
        )


@dataclass(frozen=True)
class NumberList(Field):
    max_items: int = MAX_REGION_POINTS
    minimum: float = 0.0
    maximum: float = 1.0

    def validate(self, value: Any, path: str) -> Any:
        items = _as_list(value, path, self.max_items)
        element = Number("point", minimum=self.minimum, maximum=self.maximum)
        return [element.validate(item, f"{path}[{index}]") for index, item in enumerate(items)]

    def schema(self) -> dict[str, Any]:
        return self._described(
            {
                "type": "array",
                "minItems": 1,
                "maxItems": self.max_items,
                "items": {"type": "number", "minimum": self.minimum, "maximum": self.maximum},
            }
        )


@dataclass(frozen=True)
class Obj(Field):
    members: tuple[Field, ...] = ()

    def validate(self, value: Any, path: str) -> Any:
        return validate_object(self.members, value, path)

    def schema(self) -> dict[str, Any]:
        return self._described(object_schema(self.members))


@dataclass(frozen=True)
class ObjList(Field):
    members: tuple[Field, ...] = ()
    max_items: int = MAX_ITEMS

    def validate(self, value: Any, path: str) -> Any:
        items = _as_list(value, path, self.max_items)
        validated = [
            validate_object(self.members, item, f"{path}[{index}]")
            for index, item in enumerate(items)
        ]
        # Any list whose elements carry an ``id`` is an id space, and a
        # duplicate in it makes every reference to that id ambiguous — an
        # answer key that could grade two different things.
        if any(member.name == "id" for member in self.members):
            seen: set[str] = set()
            for index, item in enumerate(validated):
                identifier = item.get("id")
                if identifier in seen:
                    raise ComponentError(f"{path}[{index}].id is a duplicate: '{identifier}'")
                seen.add(identifier)
        return validated

    def schema(self) -> dict[str, Any]:
        return self._described(
            {
                "type": "array",
                "minItems": 1,
                "maxItems": self.max_items,
                "items": object_schema(self.members),
            }
        )


def _as_list(value: Any, path: str, max_items: int) -> list[Any]:
    if not isinstance(value, list):
        raise ComponentError(f"{path} must be an array")
    if not value:
        raise ComponentError(f"{path} must not be empty")
    if len(value) > max_items:
        raise ComponentError(f"{path} must have at most {max_items} items, got {len(value)}")
    return value


def validate_object(members: tuple[Field, ...], raw: Any, path: str) -> dict[str, Any]:
    """Validate a closed object against its declared members.

    Unknown fields are an error rather than a silent drop, and so is an
    explicit ``null``: a caller who sends one has a bug, and honouring it as
    "absent" hides the bug in stored data.
    """
    if not isinstance(raw, dict):
        raise ComponentError(f"{path} must be an object")

    declared = {member.name: member for member in members}
    unknown = sorted(set(raw) - set(declared))
    if unknown:
        raise ComponentError(f"{path}: unknown field(s): {', '.join(unknown)}")

    out: dict[str, Any] = {}
    for member in members:
        if member.name not in raw:
            if member.required:
                raise ComponentError(f"{path}: missing required field '{member.name}'")
            continue
        value = raw[member.name]
        if value is None:
            raise ComponentError(f"{path}.{member.name} must not be null; omit it instead")
        try:
            out[member.name] = member.validate(value, f"{path}.{member.name}")
        except UnsafeContent as exc:
            raise ComponentError(str(exc)) from exc
    return out


#: Schemas emitted once under ``$defs`` instead of once per component type.
#: Chosen by hand rather than by deduplicating the finished schema, because a
#: reference the model reads as "the rubric" is worth more than one named
#: after a hash of its contents.
_SHARED: dict[str, Field] = {}


def shared(name: str, field: Field) -> Field:
    """Register *field* as a named definition and return a referencing copy."""
    _SHARED[name] = replace(field, ref="")
    return replace(field, ref=name)


#: The name every :class:`Ident` resolves to.
IDENTIFIER_DEF = "identifier"


def shared_definitions() -> dict[str, Any]:
    """The ``$defs`` block every reference above resolves against."""
    return {name: field.schema() for name, field in sorted(_SHARED.items())}


def object_schema(members: tuple[Field, ...]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {member.name: member.emit() for member in members},
    }
    required = [member.name for member in members if member.required]
    if required:
        schema["required"] = required
    return schema


# ── Reusable shapes ───────────────────────────────────────────────────────


def _labelled(name: str, *, text: str = "text", max_chars: int = TEXT_MAX, **kwargs: Any):
    """A list of ``{id, <text>}`` entries — options, items, steps, events."""
    return ObjList(
        name,
        members=(
            Ident("id", required=True),
            Text(text, required=True, max_chars=max_chars),
        ),
        **kwargs,
    )


shared(
    IDENTIFIER_DEF,
    Ident(
        "identifier",
        description=(
            "An opaque label, unique where it is used: lowercase letters, digits, "
            "'-' and '_', up to 64 characters."
        ),
    ),
)

_ASSET = shared(
    "managed_asset",
    Obj(
        "asset",
        description="A managed asset, by opaque identifier. Never a URL or a path.",
        members=(
            Ident("asset_ref", required=True),
            Text("alt_text", required=True, max_chars=TEXT_MAX, about_the_component=True),
            Text(
                "long_description",
                max_chars=PASSAGE_MAX,
                multiline=True,
                about_the_component=True,
            ),
        ),
    ),
)


def _asset(name: str, *, required: bool = False) -> Field:
    """A reference to a managed asset, under whatever name the type uses.

    Component validation checks only the *shape* of the reference: nothing is
    fetched, imported, or opened here. Managed assets are created by
    ``learning_studio_import_asset``, and ``prepare_experience()`` resolves
    each reference and authorises it against the current profile, learner, and
    exact track scope before an experience is stored. ``alt_text`` is required
    because an image whose alternative text is optional is an image that ships
    without one.
    """
    return replace(_ASSET, name=name, required=required)


_ACCEPTED_MEMBERS: tuple[Field, ...] = (
    TextList(
        "accepted",
        required=True,
        max_items=MAX_ACCEPTED,
        max_chars=TEXT_MAX,
        description="Every answer that counts as correct.",
    ),
    Bool("case_sensitive"),
    Bool("accent_sensitive"),
)

# The same answer object under four different questions. Registering it here
# is what puts ``text_answer`` in ``$defs``; the specs that use it name it
# through ``answer_ref`` while keeping ``_ACCEPTED_MEMBERS`` as what actually
# validates, so the reference and the runtime check are one declaration.
shared("text_answer", Obj("answer", required=True, members=_ACCEPTED_MEMBERS))

#: Accessibility metadata that travels *with* a component and is safe to show.
#: Note what is absent: nothing here describes the learner. It describes what
#: the component needs in order to be usable.
COMPONENT_ACCESSIBILITY: tuple[Field, ...] = (
    Text(
        "alt_text",
        max_chars=TEXT_MAX,
        about_the_component=True,
        description="Text alternative for non-text content.",
    ),
    Text("caption", max_chars=TEXT_MAX, about_the_component=True),
    Text("long_description", max_chars=PASSAGE_MAX, multiline=True, about_the_component=True),
    Text(
        "transcript",
        max_chars=PASSAGE_MAX,
        multiline=True,
        about_the_component=True,
        description="The spoken content of any audio or video, written out.",
    ),
    Text(
        "keyboard_alternative",
        max_chars=TEXT_MAX,
        about_the_component=True,
        description="How to answer without dragging or pointing.",
    ),
    Bool("reduced_motion"),
    Bool("no_time_limit"),
)

# ``transcript_required`` and ``captions_required`` used to sit here as
# booleans. They are gone on purpose: a flag saying a transcript is required
# is not a transcript, and an experience that promised one could be satisfied
# by a component merely asserting that somebody ought to provide it. The
# fields now hold the content itself, so the promise and the thing promised
# are the same object.

_ACCESSIBILITY = shared(
    "component_accessibility",
    Obj("accessibility", members=COMPONENT_ACCESSIBILITY),
)


# ── Answer references ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Ref:
    """An answer field whose values must name ids declared in the content.

    ``mode`` decides how strict the relationship is:

    - ``member`` — every referenced id exists.
    - ``permutation`` — the referenced ids are exactly the content's ids, each
      once. An ordering answer that omits or repeats a step is not a partial
      answer, it is an unusable one.
    """

    answer_path: str
    content_field: str
    mode: str = "member"


def _walk(value: Any, path: str) -> list[Any]:
    """Collect the values at a dotted path with ``[]`` list segments."""
    if not path:
        return [value] if value is not None else []
    head, _, rest = path.partition(".")
    if head.endswith("[]"):
        key = head[:-2]
        container = value.get(key) if isinstance(value, dict) else None
        if not isinstance(container, list):
            return []
        collected: list[Any] = []
        for item in container:
            collected.extend(_walk(item, rest))
        return collected
    found = value.get(head) if isinstance(value, dict) else None
    if found is None:
        return []
    return _walk(found, rest) if rest else ([*found] if isinstance(found, list) else [found])


def _content_ids(content: dict[str, Any], field_name: str) -> list[str]:
    entries = content.get(field_name)
    if not isinstance(entries, list):
        return []
    return [entry["id"] for entry in entries if isinstance(entry, dict) and "id" in entry]


# ── The registry ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComponentSpec:
    """One component type: its visible content, its hidden answer, its rules."""

    type: str
    family: str
    summary: str
    content: tuple[Field, ...] = ()
    #: ``None`` means the type has no answer key — open work judged against a
    #: rubric, or a self-report with no right answer at all.
    answer: tuple[Field, ...] | None = None
    #: True when the type is unmarkable without criteria. Open work with no
    #: rubric is not an exercise, it is a prompt with no way to close the loop.
    requires_rubric: bool = False
    #: True for self-report components, where a rubric would be a category
    #: error: nobody grades how confident someone says they feel, so a rubric
    #: is refused outright rather than merely discouraged.
    self_report: bool = False
    #: The scoring modes that make sense for this type. Anything else is a
    #: category error — an ordered score over a single-answer question grades
    #: nothing — and is refused by the schema and the runtime alike.
    scoring_modes: tuple[str, ...] = ()
    #: True when answering needs pointing, dragging, or placing. Used to check
    #: an experience that claims keyboard-only operation can actually deliver
    #: it.
    pointer_interaction: bool = False
    refs: tuple[Ref, ...] = ()
    #: Name of a shared ``$defs`` entry holding this type's answer object, for
    #: the shapes several types have in common. The members in ``answer`` are
    #: what actually validates; this only decides how the schema spells it.
    answer_ref: str = ""
    #: Answer fields whose text must not already appear in the visible payload.
    #: Only used where showing the answer is *definitionally* a leak.
    leak_paths: tuple[str, ...] = ()


SPECS: tuple[ComponentSpec, ...] = (
    # ── Selection ─────────────────────────────────────────────────────────
    ComponentSpec(
        type="multiple_choice",
        scoring_modes=("exact",),
        family="selection",
        summary="One correct option among several.",
        content=(
            _labelled("options", required=True, max_items=MAX_OPTIONS),
            Bool("shuffle"),
        ),
        answer=(Ident("option_id", required=True),),
        refs=(Ref("option_id", "options"),),
    ),
    ComponentSpec(
        type="multi_select",
        scoring_modes=("set",),
        family="selection",
        summary="Several correct options among many.",
        content=(
            _labelled("options", required=True, max_items=MAX_OPTIONS),
            Bool("shuffle"),
        ),
        answer=(
            IdentList("option_ids", required=True, max_items=MAX_OPTIONS),
            Bool("partial_credit"),
        ),
        refs=(Ref("option_ids[]", "options"),),
    ),
    ComponentSpec(
        type="true_false",
        scoring_modes=("exact",),
        family="selection",
        summary="A single claim to accept or reject.",
        content=(Text("statement", required=True, max_chars=PROMPT_MAX, multiline=True),),
        answer=(Bool("value", required=True),),
    ),
    ComponentSpec(
        type="classification",
        scoring_modes=("set",),
        pointer_interaction=True,
        family="selection",
        summary="Put each item in exactly one category.",
        content=(
            _labelled("items", required=True, max_items=MAX_ITEMS),
            _labelled("categories", text="label", required=True, max_items=MAX_CATEGORIES),
        ),
        answer=(
            ObjList(
                "assignments",
                required=True,
                max_items=MAX_ITEMS,
                members=(
                    Ident("item_id", required=True),
                    Ident("category_id", required=True),
                ),
            ),
            Bool("partial_credit"),
        ),
        refs=(
            Ref("assignments[].item_id", "items", mode="permutation"),
            Ref("assignments[].category_id", "categories"),
        ),
    ),
    # ── Text input ────────────────────────────────────────────────────────
    ComponentSpec(
        type="fill_blank",
        scoring_modes=("exact", "normalised", "numeric"),
        leak_paths=("blanks[].accepted",),
        family="text_input",
        summary="Complete the gaps in a passage.",
        content=(
            Text(
                "text",
                required=True,
                max_chars=PASSAGE_MAX,
                multiline=True,
                description="The passage, with each gap written as {{blank_id}}.",
            ),
            ObjList(
                "blanks",
                required=True,
                max_items=MAX_BLANKS,
                members=(Ident("id", required=True), Text("label", max_chars=LABEL_MAX)),
            ),
        ),
        answer=(
            ObjList(
                "blanks",
                required=True,
                max_items=MAX_BLANKS,
                members=(
                    Ident("blank_id", required=True),
                    TextList("accepted", required=True, max_items=MAX_ACCEPTED),
                ),
            ),
            Bool("case_sensitive"),
            Bool("accent_sensitive"),
        ),
        refs=(Ref("blanks[].blank_id", "blanks", mode="permutation"),),
    ),
    ComponentSpec(
        type="short_answer",
        scoring_modes=("exact", "normalised", "numeric"),
        leak_paths=("accepted",),
        family="text_input",
        summary="A short produced answer, graded against accepted forms.",
        content=(Int("max_words", minimum=1, maximum=200),),
        answer=_ACCEPTED_MEMBERS,
        answer_ref="text_answer",
    ),
    ComponentSpec(
        type="free_response",
        scoring_modes=("rubric",),
        family="text_input",
        summary="Extended writing, judged against a rubric.",
        content=(
            Int("min_words", minimum=1, maximum=MAX_WORDS),
            Int("max_words", minimum=1, maximum=MAX_WORDS),
        ),
        answer=None,
        requires_rubric=True,
    ),
    ComponentSpec(
        type="translation",
        scoring_modes=("exact", "normalised"),
        leak_paths=("accepted",),
        family="text_input",
        summary="Render a passage in another language.",
        content=(
            Text("source_text", required=True, max_chars=PASSAGE_MAX, multiline=True),
            Locale("source_locale", required=True),
            Locale("target_locale", required=True),
        ),
        answer=_ACCEPTED_MEMBERS,
        answer_ref="text_answer",
    ),
    ComponentSpec(
        type="error_correction",
        scoring_modes=("exact", "normalised"),
        leak_paths=("corrections[].correct",),
        family="text_input",
        summary="Find and fix what is wrong in a given passage.",
        content=(
            Text("text", required=True, max_chars=PASSAGE_MAX, multiline=True),
            Int("error_count", minimum=1, maximum=20),
        ),
        answer=(
            ObjList(
                "corrections",
                required=True,
                max_items=MAX_ITEMS,
                members=(
                    Text("incorrect", required=True, max_chars=TEXT_MAX),
                    Text("correct", required=True, max_chars=TEXT_MAX),
                    Text("explanation", max_chars=TEXT_MAX),
                ),
            ),
        ),
    ),
    ComponentSpec(
        type="code_response",
        scoring_modes=("rubric", "exact", "normalised"),
        leak_paths=("reference_solution",),
        family="text_input",
        summary="Write code, collected and compared as text. Never executed.",
        content=(
            Text("language", required=True, max_chars=40),
            Text("starter_code", max_chars=PASSAGE_MAX, multiline=True),
            TextList("requirements", max_items=MAX_PROMPT_LIST),
        ),
        answer=(
            Text("reference_solution", required=True, max_chars=PASSAGE_MAX, multiline=True),
            TextList("must_include", max_items=MAX_ACCEPTED),
            TextList("must_not_include", max_items=MAX_ACCEPTED),
        ),
    ),
    # ── Ordering and matching ─────────────────────────────────────────────
    ComponentSpec(
        type="sentence_order",
        scoring_modes=("ordered",),
        pointer_interaction=True,
        family="ordering",
        summary="Arrange fragments into a well-formed whole.",
        content=(_labelled("tokens", required=True, max_items=MAX_ITEMS, max_chars=LABEL_MAX),),
        answer=(IdentList("order", required=True, max_items=MAX_ITEMS),),
        refs=(Ref("order[]", "tokens", mode="permutation"),),
    ),
    ComponentSpec(
        type="sequence_order",
        scoring_modes=("ordered",),
        pointer_interaction=True,
        family="ordering",
        summary="Put steps into the order they must happen in.",
        content=(_labelled("steps", required=True, max_items=MAX_ITEMS),),
        answer=(IdentList("order", required=True, max_items=MAX_ITEMS),),
        refs=(Ref("order[]", "steps", mode="permutation"),),
    ),
    ComponentSpec(
        type="matching",
        scoring_modes=("set",),
        pointer_interaction=True,
        family="ordering",
        summary="Pair each item on the left with one on the right.",
        content=(
            _labelled("left", required=True, max_items=MAX_PAIRS),
            _labelled("right", required=True, max_items=MAX_PAIRS),
        ),
        answer=(
            ObjList(
                "pairs",
                required=True,
                max_items=MAX_PAIRS,
                members=(Ident("left_id", required=True), Ident("right_id", required=True)),
            ),
            Bool("partial_credit"),
        ),
        refs=(
            Ref("pairs[].left_id", "left", mode="permutation"),
            Ref("pairs[].right_id", "right"),
        ),
    ),
    ComponentSpec(
        type="categorization",
        scoring_modes=("set",),
        pointer_interaction=True,
        family="ordering",
        summary="Group items, where an item may belong to more than one group.",
        content=(
            _labelled("items", required=True, max_items=MAX_ITEMS),
            _labelled("categories", text="label", required=True, max_items=MAX_CATEGORIES),
            Bool("allow_multiple"),
        ),
        answer=(
            ObjList(
                "assignments",
                required=True,
                max_items=MAX_ITEMS,
                members=(
                    Ident("item_id", required=True),
                    IdentList("category_ids", required=True, max_items=MAX_CATEGORIES),
                ),
            ),
            Bool("partial_credit"),
        ),
        # ``category_ids`` is deliberately *not* a generic reference: flattened
        # across every assignment, two items placed in the same category read
        # as a duplicate, which is the commonest correct answer there is. See
        # :func:`_check_categorization`.
        refs=(Ref("assignments[].item_id", "items", mode="permutation"),),
    ),
    # ── Recall ────────────────────────────────────────────────────────────
    ComponentSpec(
        type="flashcard",
        scoring_modes=("self_check",),
        leak_paths=("back",),
        family="recall",
        summary="A prompt and its reverse, self-graded after an attempt.",
        content=(
            Text("front", required=True, max_chars=TEXT_MAX, multiline=True),
            Text("front_note", max_chars=TEXT_MAX),
        ),
        answer=(
            Text("back", required=True, max_chars=TEXT_MAX, multiline=True),
            Text("mnemonic", max_chars=TEXT_MAX),
        ),
    ),
    ComponentSpec(
        type="typed_recall",
        scoring_modes=("exact", "normalised"),
        leak_paths=("accepted",),
        family="recall",
        summary="Retrieve from memory and type it, graded against accepted forms.",
        content=(Text("cue", required=True, max_chars=TEXT_MAX),),
        answer=_ACCEPTED_MEMBERS,
        answer_ref="text_answer",
    ),
    # ── Visual and diagrammatic ───────────────────────────────────────────
    ComponentSpec(
        type="image_observation",
        scoring_modes=("rubric",),
        family="visual",
        summary="Describe what an image shows, judged against a rubric.",
        content=(
            _asset("image", required=True),
            TextList("focus_points", max_items=MAX_PROMPT_LIST),
        ),
        answer=None,
        requires_rubric=True,
    ),
    ComponentSpec(
        type="image_choice",
        scoring_modes=("exact",),
        family="visual",
        summary="Choose the right image among several.",
        content=(
            ObjList(
                "options",
                required=True,
                max_items=MAX_OPTIONS,
                members=(
                    Ident("id", required=True),
                    Text("caption", max_chars=LABEL_MAX),
                    _asset("image", required=True),
                ),
            ),
        ),
        answer=(Ident("option_id", required=True),),
        refs=(Ref("option_id", "options"),),
    ),
    ComponentSpec(
        type="diagram",
        scoring_modes=("exact", "normalised"),
        leak_paths=("accepted",),
        family="visual",
        summary="Read a diagram and state what it shows.",
        content=(
            _asset("image", required=True),
            _labelled("callouts", required=False, max_items=MAX_MARKERS, max_chars=LABEL_MAX),
        ),
        answer=_ACCEPTED_MEMBERS,
        answer_ref="text_answer",
    ),
    ComponentSpec(
        type="hotspot",
        scoring_modes=("exact",),
        pointer_interaction=True,
        family="visual",
        summary="Point at the right place on an image.",
        content=(_asset("image", required=True), Bool("show_grid")),
        answer=(
            ObjList(
                "regions",
                required=True,
                max_items=MAX_REGIONS,
                members=(
                    Ident("id", required=True),
                    Enum("shape", required=True, choices=("rectangle", "circle", "polygon")),
                    NumberList(
                        "points",
                        required=True,
                        max_items=MAX_REGION_POINTS,
                        description="Coordinates as fractions of width and height.",
                    ),
                    Text("label", max_chars=LABEL_MAX),
                ),
            ),
            Number("tolerance", minimum=0.0, maximum=0.5),
        ),
    ),
    ComponentSpec(
        type="labeling",
        scoring_modes=("set",),
        pointer_interaction=True,
        family="visual",
        summary="Put each label on the right marker.",
        content=(
            _asset("image", required=True),
            ObjList(
                "markers",
                required=True,
                max_items=MAX_MARKERS,
                members=(
                    Ident("id", required=True),
                    Number("x", required=True),
                    Number("y", required=True),
                ),
            ),
            _labelled("label_bank", required=True, max_items=MAX_LABEL_BANK, max_chars=LABEL_MAX),
        ),
        answer=(
            ObjList(
                "labels",
                required=True,
                max_items=MAX_MARKERS,
                members=(Ident("marker_id", required=True), Ident("label_id", required=True)),
            ),
        ),
        refs=(
            Ref("labels[].marker_id", "markers", mode="permutation"),
            Ref("labels[].label_id", "label_bank"),
        ),
    ),
    # ── Timeline and process ──────────────────────────────────────────────
    ComponentSpec(
        type="timeline",
        scoring_modes=("ordered",),
        pointer_interaction=True,
        family="timeline",
        summary="Place events in chronological order.",
        content=(
            ObjList(
                "events",
                required=True,
                max_items=MAX_ITEMS,
                members=(
                    Ident("id", required=True),
                    Text("text", required=True, max_chars=TEXT_MAX),
                    Text("date_label", max_chars=LABEL_MAX),
                ),
            ),
            Bool("show_dates"),
        ),
        answer=(IdentList("order", required=True, max_items=MAX_ITEMS),),
        refs=(Ref("order[]", "events", mode="permutation"),),
    ),
    ComponentSpec(
        type="process_flow",
        scoring_modes=("ordered",),
        pointer_interaction=True,
        family="timeline",
        summary="Order the stages of a process and how they connect.",
        content=(
            _labelled("stages", required=True, max_items=MAX_ITEMS),
            Text("start_stage_label", max_chars=LABEL_MAX),
        ),
        answer=(
            IdentList("order", required=True, max_items=MAX_ITEMS),
            ObjList(
                "transitions",
                max_items=MAX_CELLS,
                members=(
                    Ident("from_id", required=True),
                    Ident("to_id", required=True),
                    Text("condition", max_chars=LABEL_MAX),
                ),
            ),
        ),
        refs=(
            Ref("order[]", "stages", mode="permutation"),
            Ref("transitions[].from_id", "stages"),
            Ref("transitions[].to_id", "stages"),
        ),
    ),
    # ── Structured information ────────────────────────────────────────────
    ComponentSpec(
        type="table_grid",
        scoring_modes=("set", "exact", "normalised"),
        pointer_interaction=True,
        leak_paths=("cells[].accepted",),
        family="structured",
        summary="Fill a grid, contrasting cases across two dimensions.",
        content=(
            _labelled(
                "rows", text="header", required=True, max_items=MAX_ROWS, max_chars=LABEL_MAX
            ),
            _labelled(
                "columns", text="header", required=True, max_items=MAX_COLUMNS, max_chars=LABEL_MAX
            ),
            ObjList(
                "prefilled_cells",
                max_items=MAX_CELLS,
                members=(
                    Ident("row_id", required=True),
                    Ident("column_id", required=True),
                    Text("text", required=True, max_chars=LABEL_MAX),
                ),
            ),
        ),
        answer=(
            ObjList(
                "cells",
                required=True,
                max_items=MAX_CELLS,
                members=(
                    Ident("row_id", required=True),
                    Ident("column_id", required=True),
                    TextList("accepted", required=True, max_items=MAX_ACCEPTED),
                ),
            ),
            Bool("case_sensitive"),
            Bool("partial_credit"),
        ),
        refs=(
            Ref("cells[].row_id", "rows"),
            Ref("cells[].column_id", "columns"),
        ),
    ),
    # ── Scenarios and decisions ───────────────────────────────────────────
    ComponentSpec(
        type="scenario_choice",
        scoring_modes=("exact",),
        family="scenario",
        summary="Judge one situation and choose what to do.",
        content=(
            Text("situation", required=True, max_chars=PASSAGE_MAX, multiline=True),
            _labelled("options", required=True, max_items=MAX_OPTIONS),
        ),
        answer=(
            Ident("option_id", required=True),
            ObjList(
                "consequences",
                max_items=MAX_OPTIONS,
                members=(
                    Ident("option_id", required=True),
                    Text("text", required=True, max_chars=TEXT_MAX),
                ),
            ),
        ),
        refs=(
            Ref("option_id", "options"),
            Ref("consequences[].option_id", "options"),
        ),
    ),
    ComponentSpec(
        type="decision_path",
        scoring_modes=("ordered",),
        family="scenario",
        summary="A sequence of decisions where each one follows from the last.",
        content=(
            Text("situation", required=True, max_chars=PASSAGE_MAX, multiline=True),
            ObjList(
                "steps",
                required=True,
                max_items=MAX_STEPS,
                members=(
                    Ident("id", required=True),
                    Text("prompt", required=True, max_chars=PROMPT_MAX, multiline=True),
                    _labelled("options", required=True, max_items=MAX_OPTIONS),
                ),
            ),
        ),
        answer=(
            ObjList(
                "decisions",
                required=True,
                max_items=MAX_STEPS,
                description="The option chosen at each step, in order.",
                members=(Ident("step_id", required=True), Ident("option_id", required=True)),
            ),
        ),
        refs=(Ref("decisions[].step_id", "steps", mode="permutation"),),
    ),
    ComponentSpec(
        type="case_study",
        scoring_modes=("rubric",),
        family="scenario",
        summary="Extended material with questions, judged against a rubric.",
        content=(
            Text("background", required=True, max_chars=PASSAGE_MAX, multiline=True),
            TextList("questions", required=True, max_items=MAX_PROMPT_LIST, max_chars=PROMPT_MAX),
            TextList("materials", max_items=MAX_PROMPT_LIST),
        ),
        answer=None,
        requires_rubric=True,
    ),
    # ── Reflection and assessment ─────────────────────────────────────────
    ComponentSpec(
        type="confidence_rating",
        scoring_modes=("self_check",),
        family="reflection",
        summary="How sure the learner is. A self-report, never marked.",
        content=(
            Int("scale_min", required=True, minimum=0, maximum=10),
            Int("scale_max", required=True, minimum=1, maximum=10),
            TextList("scale_labels", max_items=11, max_chars=LABEL_MAX),
        ),
        answer=None,
        self_report=True,
    ),
    ComponentSpec(
        type="self_explanation",
        scoring_modes=("rubric",),
        family="reflection",
        summary="Explain the reasoning, judged against a rubric.",
        content=(
            # Required, exactly as `reflection` requires it. Optional `prompts`
            # meant a component could validate, store, and then render *no
            # response field at all* -- an exercise card with nothing to answer,
            # which submitted an empty `responses` array and advanced. There is
            # no such thing as a self-explanation with nothing to explain, so the
            # honest place to say so is here rather than in the renderer.
            TextList("prompts", required=True, max_items=MAX_PROMPT_LIST, max_chars=PROMPT_MAX),
            Int("min_words", minimum=1, maximum=MAX_WORDS),
        ),
        answer=None,
        requires_rubric=True,
    ),
    ComponentSpec(
        type="reflection",
        scoring_modes=("self_check",),
        family="reflection",
        summary="Look back on the work. A self-report, never marked.",
        content=(
            TextList("prompts", required=True, max_items=MAX_PROMPT_LIST, max_chars=PROMPT_MAX),
            Int("min_words", minimum=1, maximum=MAX_WORDS),
        ),
        answer=None,
        self_report=True,
    ),
    ComponentSpec(
        type="rubric_response",
        scoring_modes=("rubric",),
        family="reflection",
        summary="Open work produced explicitly against named criteria.",
        content=(
            TextList("requirements", max_items=MAX_PROMPT_LIST),
            Int("min_words", minimum=1, maximum=MAX_WORDS),
            Int("max_words", minimum=1, maximum=MAX_WORDS),
        ),
        answer=None,
        requires_rubric=True,
    ),
)

SPEC_BY_TYPE: dict[str, ComponentSpec] = {spec.type: spec for spec in SPECS}

COMPONENT_TYPES: tuple[str, ...] = tuple(spec.type for spec in SPECS)

FAMILIES: tuple[str, ...] = tuple(dict.fromkeys(spec.family for spec in SPECS))


# ── Evaluation (hidden) ───────────────────────────────────────────────────

_RUBRIC = shared(
    "rubric",
    ObjList(
        "rubric",
        max_items=MAX_CRITERIA,
        description="Named criteria and their levels. Evaluator-only.",
        members=(
            Text("criterion", required=True, max_chars=TEXT_MAX),
            ObjList(
                "levels",
                required=True,
                max_items=MAX_LEVELS,
                members=(
                    Text("label", required=True, max_chars=LABEL_MAX),
                    Text("descriptor", required=True, max_chars=TEXT_MAX),
                    Int("points", required=True, minimum=0, maximum=100),
                ),
            ),
        ),
    ),
)


def _scoring(modes: tuple[str, ...]) -> Field:
    """The scoring block for one set of allowed modes.

    Emitted per mode-set rather than once for everything: the type already
    decides what "scored" can mean, so the schema says so too. There are far
    fewer distinct sets than there are component types, so this stays a
    handful of definitions rather than 31.
    """
    name = "scoring_" + "_".join(modes)
    if name in _SHARED:
        return replace(_SHARED[name], ref=name)
    return shared(
        name,
        Obj(
            "scoring",
            description="How the response is judged. Evaluator-only.",
            members=(
                Enum("mode", required=True, choices=modes),
                Bool("partial_credit"),
                Int("points", minimum=0, maximum=1000),
                Number("tolerance", minimum=0.0, maximum=1000000.0),
                Text("units", max_chars=40),
            ),
        ),
    )


_FEEDBACK = shared(
    "feedback",
    Obj(
        "feedback",
        description="What to say after an attempt. Evaluator-only until then.",
        members=(
            Text("correct", max_chars=TEXT_MAX, multiline=True),
            Text("incorrect", max_chars=TEXT_MAX, multiline=True),
            ObjList(
                "per_option",
                max_items=MAX_OPTIONS,
                members=(
                    Ident("option_id", required=True),
                    Text("text", required=True, max_chars=TEXT_MAX),
                ),
            ),
        ),
    ),
)

_BRANCHING = shared(
    "branching",
    ObjList(
        "branching",
        max_items=MAX_BRANCHES,
        description="Where to go next. Correctness branches are evaluator-only.",
        members=(
            Enum("on", required=True, choices=BRANCH_CONDITIONS),
            Ident("go_to", required=True),
        ),
    ),
)

_HINTS = shared(
    "hints",
    TextList(
        "hints",
        max_items=MAX_HINTS,
        max_chars=TEXT_MAX,
        description="Progressive hints, released one at a time. Never sent up front.",
    ),
)

_NOTES = shared(
    "evaluator_notes",
    Text(
        "notes",
        max_chars=PASSAGE_MAX,
        multiline=True,
        description="Evaluator notes. Never shown to the learner.",
    ),
)


#: The judging half. Everything except ``scoring`` is identical for every
#: component type; ``scoring`` narrows to the modes the type can actually be
#: marked with, and the rubric is required, optional, or refused depending on
#: whether the type is open work, keyed, or a self-report.
_EVALUATION_TAIL: tuple[Field, ...] = (_HINTS, _FEEDBACK, _BRANCHING, _NOTES)


def _evaluation(spec: ComponentSpec) -> Field:
    """The evaluation block for one component type, shared by shape."""
    scoring = _scoring(spec.scoring_modes)
    if spec.self_report:
        # No rubric at all. Grading how confident somebody says they feel is a
        # category error, and a field that exists invites the attempt.
        kind, members, required = "self_report", (scoring, *_EVALUATION_TAIL), False
    elif spec.requires_rubric:
        kind = "open"
        members = (replace(_RUBRIC, required=True), scoring, *_EVALUATION_TAIL)
        required = True
    else:
        kind, members, required = "keyed", (_RUBRIC, scoring, *_EVALUATION_TAIL), False

    name = f"evaluation_{kind}_{'_'.join(spec.scoring_modes)}"
    if name in _SHARED:
        return replace(_SHARED[name], ref=name, required=required)
    return shared(
        name,
        Obj(
            "evaluation",
            required=required,
            description="How the response is judged. Never shown to the learner.",
            members=members,
        ),
    )


#: Registering every type's scoring and evaluation block at import time, so
#: that :func:`shared_definitions` is complete no matter what order a caller
#: asks in. Built lazily, a definition referenced by a component branch could
#: be missing from ``$defs`` simply because nothing had asked for that branch
#: yet — a dangling reference in the advertised schema.
_EAGER_DEFINITIONS: tuple[Field, ...] = tuple(_evaluation(spec) for spec in SPECS)


def component_members(spec: ComponentSpec) -> tuple[Field, ...]:
    """Every field of one component type, visible and hidden together."""
    members: list[Field] = [
        Ident("id", required=True, description="Unique in this experience."),
        Enum("type", required=True, choices=(spec.type,), description=spec.summary),
        Text("prompt", required=True, max_chars=PROMPT_MAX, multiline=True),
    ]
    if spec.content:
        members.append(Obj("content", required=_content_required(spec), members=spec.content))
    members.append(_ACCESSIBILITY)
    if spec.answer is not None:
        members.append(
            Obj(
                "answer",
                required=True,
                description="The answer key. Never leaves the server.",
                members=spec.answer,
                ref=spec.answer_ref,
            )
        )
    members.append(_evaluation(spec))
    return tuple(members)


def _content_required(spec: ComponentSpec) -> bool:
    return any(member.required for member in spec.content)


# ── Validation ────────────────────────────────────────────────────────────


#: Visible lists whose *order* would otherwise disclose the answer, by type.
#:
#: Two different leaks, both mechanical:
#:
#: - the ordering families (``sentence_order``, ``sequence_order``, ``timeline``,
#:   ``process_flow``) are graded on ``answer.order``, and an author naturally
#:   writes the list in the correct order — so the list *is* the key;
#:  - ``matching.right`` and ``labeling.label_bank`` are the option lists behind
#:   each row's ``<select>``, and an author naturally writes them parallel to the
#:   rows they belong to — so "the first option of the first dropdown" is the key.
#:
#: Not listed, deliberately: ``multiple_choice.options`` and friends, where the
#: answer is an id rather than a position, so the order discloses nothing. Those
#: types carry their own ``shuffle`` flag for presentation variety, which is a
#: different concern with a different name.
ANSWER_BEARING_ORDER: dict[str, tuple[str, ...]] = {
    "sentence_order": ("tokens",),
    "sequence_order": ("steps",),
    "timeline": ("events",),
    "process_flow": ("stages",),
    "matching": ("right",),
    "labeling": ("label_bank",),
}

#: Content fields that are only meant to be shown when a sibling flag says so.
#: ``timeline.date_label`` is the case that matters: with ``show_dates`` off the
#: renderer must not show it, and a projection that carried it anyway would be
#: shipping the ordering clue to the client and trusting the client not to look.
GATED_CONTENT: dict[str, tuple[tuple[str, str, str], ...]] = {
    # (list field, entry field to drop, flag that must be true to keep it)
    "timeline": (("events", "date_label", "show_dates"),),
}


def _unpredictable_shuffle(items: list[Any]) -> None:
    """Shuffle in place, using randomness a learner cannot anticipate.

    ``secrets.SystemRandom`` rather than the default ``random`` module: the
    module-level generator is a Mersenne Twister that can be seeded — and *is*
    seeded, reproducibly, by anything that calls ``random.seed()`` anywhere in the
    process — which would make the arrangement of an exercise predictable from
    outside. This is cheap and it removes the question.

    A fixed transformation such as reversing the list would also "not be the
    answer", and would be worse than either: it is a pattern a learner learns
    once and then reads backwards forever.
    """
    _SYSTEM_RANDOM.shuffle(items)


_SYSTEM_RANDOM = secrets.SystemRandom()


def shuffled_content(
    component_type: str,
    content: dict[str, Any],
    *,
    shuffle: Callable[[list[Any]], None] | None = None,
) -> dict[str, Any]:
    """Return ``content`` with any answer-bearing order rearranged.

    Guarantees, each one tested:

    - every entry survives exactly once — this is a permutation, never a filter;
    - the result differs from the input whenever a different arrangement exists,
      so a one-item list is returned as it came and a two-item list is genuinely
      rearranged rather than left alone by an unlucky draw;
    - identity travels with the opaque ``id``, so duplicate visible labels and
      per-entry extras (a ``date_label``, coordinates) stay attached to the entry
      they belong to;
    - the input is not mutated, and neither is the evaluator-only answer.
    """
    fields = ANSWER_BEARING_ORDER.get(component_type, ())
    gates = GATED_CONTENT.get(component_type, ())
    if not fields and not gates:
        return content

    rearrange = shuffle or _unpredictable_shuffle
    projected = dict(content)

    for field_name in fields:
        entries = projected.get(field_name)
        if not isinstance(entries, list) or len(entries) < 2:
            # Nothing to hide: an empty or single-entry list has exactly one
            # arrangement, and returning it unchanged is the whole truth.
            continue
        order = list(entries)
        for _attempt in range(8):
            rearrange(order)
            if order != entries:
                break
        else:
            # Astronomically unlikely, and a silent no-op here would be the one
            # outcome that reveals the answer. Rotating by one is guaranteed to
            # differ for any list of two or more.
            order = order[1:] + order[:1]
        projected[field_name] = order

    for list_field, entry_field, flag in gates:
        if projected.get(flag) is True:
            continue
        entries = projected.get(list_field)
        if isinstance(entries, list):
            projected[list_field] = [
                {key: value for key, value in entry.items() if key != entry_field}
                if isinstance(entry, dict)
                else entry
                for entry in entries
            ]

    return projected


@dataclass(frozen=True)
class Component:
    """One validated component, with its two halves kept apart.

    ``answer`` and ``evaluation`` are not private by naming convention — they
    are simply absent from :meth:`learner_payload`, which is the only path
    anything here takes to a learner.
    """

    id: str
    type: str
    prompt: str
    content: dict[str, Any]
    accessibility: dict[str, Any]
    answer: dict[str, Any]
    evaluation: dict[str, Any]

    def hidden(self) -> dict[str, Any]:
        """Everything the learner must not see, for the evaluator-only store."""
        hidden: dict[str, Any] = {}
        if self.answer:
            hidden["answer"] = self.answer
        if self.evaluation:
            hidden["evaluation"] = self.evaluation
        return hidden

    def learner_payload(
        self, *, shuffle: Callable[[list[Any]], None] | None = None
    ) -> dict[str, Any]:
        """The safe projection, assembled from an allowlist.

        Constructed rather than filtered. A field added to this class later is
        hidden by default and stays hidden until someone deliberately adds it
        to :data:`LEARNER_VISIBLE_KEYS`, which is the failure mode worth
        having: the safe direction is the one you get for free.

        **Omitting the answer is not the same as not showing it.** For the
        ordering families the *order of the visible list is itself the answer*:
        an author writes the steps of a titration in the right order and states
        the same order under ``answer.order``, so a projection that copied the
        list through displayed the correct sequence to a learner who had only to
        press Submit. Every canonical fixture in this repository did exactly
        that. :func:`shuffled_content` is therefore part of the projection, not a
        nicety the frontend could be trusted to add — the client never receives
        an answer-bearing order it would have to repair.

        ``shuffle`` exists so tests can make the permutation deterministic. It
        defaults to an unpredictable one; there is no way to ask for "no shuffle".
        """
        payload: dict[str, Any] = {"id": self.id, "type": self.type, "prompt": self.prompt}
        if self.content:
            payload["content"] = shuffled_content(self.type, self.content, shuffle=shuffle)
        if self.accessibility:
            payload["accessibility"] = self.accessibility
        return payload

    def branch_targets(self) -> tuple[tuple[str, str], ...]:
        """``(condition, target_component_id)`` for every declared branch."""
        branches = self.evaluation.get("branching") or []
        return tuple((branch["on"], branch["go_to"]) for branch in branches)


def build_component(raw: Any, path: str) -> Component:
    """Validate one component and return its two separated halves.

    Every refusal raised from here passes through :func:`_without_hidden_data`
    first. The messages below are already written to name a field rather than
    a value, but "written carefully" is not a property anyone can check later:
    the scrub is, and it is what a canary test asserts.
    """
    if not isinstance(raw, dict):
        raise ComponentError(f"{path} must be an object")

    declared_type = raw.get("type")
    if not isinstance(declared_type, str) or declared_type not in SPEC_BY_TYPE:
        raise ComponentError(
            f"{path}.type must be one of the known component types: {', '.join(COMPONENT_TYPES)}"
        )
    spec = SPEC_BY_TYPE[declared_type]

    try:
        return _build_checked(spec, raw, path)
    except ComponentError as exc:
        raise ComponentError(_without_hidden_data(str(exc), raw, path)) from None


def _build_checked(spec: ComponentSpec, raw: dict[str, Any], path: str) -> Component:
    validated = validate_object(component_members(spec), raw, path)
    content = validated.get("content", {})
    answer = validated.get("answer", {})
    evaluation = validated.get("evaluation", {})

    _check_answer_references(spec, content, answer, path)
    _check_scoring(spec, evaluation, path)
    _check_feedback(spec, content, evaluation, path)
    for check in _CROSS_CHECKS.get(spec.type, ()):
        check(content, answer, path)
    _check_word_bounds(content, path)

    component = Component(
        id=validated["id"],
        type=spec.type,
        prompt=validated["prompt"],
        content=content,
        accessibility=validated.get("accessibility", {}),
        answer=answer,
        evaluation=evaluation,
    )
    _check_answer_leak(spec, component, path)
    return component


#: Values that may appear in an error even though they live in the hidden
#: half: they are closed vocabularies the caller itself chose from, and a
#: message that cannot name the invalid enum it is rejecting is not a message.
#: Nothing here can carry content — every entry is a fixed keyword.
_ECHOABLE = frozenset(
    {*SCORING_MODES, *BRANCH_CONDITIONS, "rectangle", "circle", "polygon", "true", "false"}
)


def without_hidden_data_across(message: str, components: Any, path: str) -> str:
    """Scrub a whole-manifest error against every component at once.

    Visibility is a property of the *manifest*, not of one component: a
    component id is visible wherever it appears, so naming component ``two``
    in an error about component ``one`` discloses nothing. Scrubbing
    component-by-component would treat every other component's id as hidden
    and withhold ordinary structural errors.
    """
    if not isinstance(components, list):
        return message

    visible = {
        value.casefold()
        for component in components
        if isinstance(component, dict)
        for key in LEARNER_VISIBLE_KEYS
        for value in _all_strings(component.get(key))
    }
    message_tokens = tokens(message)
    for component in components:
        for value in _hidden_strings(component):
            if value.casefold() in visible:
                continue
            if _appears_in(value, message, message_tokens):
                return _withheld(path)
    return message


def _without_hidden_data(message: str, raw: Any, path: str) -> str:
    """Replace an error that quotes evaluator-only content with a safe one.

    The tool promises the model that answers, rubrics, scoring rules, hints
    and feedback never come back. An error message is a response like any
    other, so a refusal that says "'Paris' is already readable" hands over
    exactly what the check was protecting. This is the structural guarantee
    behind that promise: if any evaluator-only string is present in the
    message, the message does not go out.
    """
    message_tokens = tokens(message)
    for value in _hidden_strings(raw):
        if _appears_in(value, message, message_tokens):
            return _withheld(path)
    return message


def _withheld(path: str) -> str:
    return (
        f"{path} was refused, and the reason quotes evaluator-only content, so it has been "
        "withheld. Check the component's answer and evaluation against the schema."
    )


#: Below this length a hidden value is indistinguishable from ordinary
#: message vocabulary — "2" is a token of "2 cell(s)", "no" of "no options" —
#: and scrubbing on it would withhold almost every refusal while proving
#: nothing. The primary protection is that no message interpolates a value
#: from the hidden half in the first place; this scrub is the backstop that
#: makes that checkable, not the mechanism itself.
_SCRUB_MIN_CHARS = 3


def _appears_in(value: str, message: str, message_tokens: list[str]) -> bool:
    """True when *value* is readable in *message*, on token boundaries.

    Boundaries rather than substrings, for the same reason the leak check
    uses them: a hidden ``Na`` must not match inside "national".
    """
    if len(value.strip()) < _SCRUB_MIN_CHARS:
        return False
    value_tokens = tokens(value)
    if value_tokens:
        return contains_token_sequence(message_tokens, value_tokens)
    symbols = symbol_form(value)
    return len(symbols) >= _SCRUB_MIN_CHARS and symbols in symbol_form(message)


def _hidden_strings(raw: Any) -> list[str]:
    """Strings that are *only* in the hidden half, and are not fixed keywords.

    A value the learner can already read is not a disclosure: an option id, a
    blank id, a row header. Those appear on both sides, so naming one in an
    error tells nobody anything they were not already shown — and a scrub that
    withheld them would make almost every message unusable.
    """
    if not isinstance(raw, dict):
        return []
    visible = {
        value.casefold() for key in LEARNER_VISIBLE_KEYS for value in _all_strings(raw.get(key))
    }
    found: list[str] = []
    for key in HIDDEN_KEYS:
        found.extend(_all_strings(raw.get(key)))
    return [
        value
        for value in found
        if value and value.casefold() not in _ECHOABLE and value.casefold() not in visible
    ]


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _all_strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _all_strings(item)]
    return []


def _check_answer_references(
    spec: ComponentSpec, content: dict[str, Any], answer: dict[str, Any], path: str
) -> None:
    """Every id an answer names must be one the learner can actually see."""
    if not answer:
        return
    for ref in spec.refs:
        declared = _content_ids(content, ref.content_field)
        referenced = [str(value) for value in _walk(answer, ref.answer_path)]
        unknown = {value for value in referenced if value not in declared}
        if unknown:
            # The count, not the ids. Which option an answer names *is* the
            # answer, so echoing it back would disclose the key.
            raise ComponentError(
                f"{path}.answer.{ref.answer_path} names {len(unknown)} id(s) that "
                f"{ref.content_field} does not declare"
            )
        if ref.mode == "permutation" and sorted(referenced) != sorted(declared):
            raise ComponentError(
                f"{path}.answer.{ref.answer_path} must name every entry in "
                f"{ref.content_field} exactly once"
            )
        # A repeated id in a set-valued answer is not a stronger answer, it is
        # an ambiguous one: "these two options" listed twice grades differently
        # depending on whether the marker deduplicates.
        if ref.mode != "member" or ref.answer_path.endswith("[]"):
            duplicates = sorted({v for v in referenced if referenced.count(v) > 1})
            if duplicates:
                raise ComponentError(
                    f"{path}.answer.{ref.answer_path} names {', '.join(duplicates)} more than once"
                )


def _check_decision_path(content: dict[str, Any], answer: dict[str, Any], path: str) -> None:
    """A decision path's chosen option must belong to *that* step.

    Expressed as code rather than as a :class:`Ref` because the id space is
    nested: option ``a`` in step 1 and option ``a`` in step 2 are different
    options, and a flat membership check would happily accept the wrong one.
    """
    options_by_step = {
        step["id"]: {option["id"] for option in step.get("options", [])}
        for step in content.get("steps", [])
    }
    for index, entry in enumerate(answer.get("decisions", [])):
        available = options_by_step.get(entry["step_id"], set())
        if entry["option_id"] not in available:
            raise ComponentError(
                f"{path}.answer.decisions[{index}].option_id is not an option of that step"
            )


def _check_table_grid(content: dict[str, Any], answer: dict[str, Any], path: str) -> None:
    """Every cell in the grid is either filled in or expected, exactly once.

    A grid with an unaccounted cell is a question with no stated answer: the
    learner is shown an empty box nothing will ever mark.
    """
    rows = _content_ids(content, "rows")
    columns = _content_ids(content, "columns")
    grid = {(row, column) for row in rows for column in columns}

    prefilled: set[tuple[str, str]] = set()
    for index, cell in enumerate(content.get("prefilled_cells", [])):
        key = (cell["row_id"], cell["column_id"])
        where = f"{path}.content.prefilled_cells[{index}]"
        _require_cell(key, grid, rows, columns, where)
        if key in prefilled:
            raise ComponentError(f"{where} fills the same cell twice")
        prefilled.add(key)

    expected: set[tuple[str, str]] = set()
    for index, cell in enumerate(answer.get("cells", [])):
        key = (cell["row_id"], cell["column_id"])
        where = f"{path}.answer.cells[{index}]"
        _require_cell(key, grid, rows, columns, where)
        if key in expected:
            raise ComponentError(f"{where} defines the same cell twice")
        if key in prefilled:
            raise ComponentError(
                f"{where} expects an answer in a cell that is already filled in for the learner"
            )
        expected.add(key)

    unaccounted = sorted(grid - prefilled - expected)
    if unaccounted:
        first = unaccounted[0]
        raise ComponentError(
            f"{path} leaves cell ({first[0]}, {first[1]}) with neither a prefilled value nor "
            f"an expected answer; {len(unaccounted)} cell(s) are unaccounted for"
        )


def _require_cell(
    key: tuple[str, str], grid: set[tuple[str, str]], rows, columns, where: str
) -> None:
    if key[0] not in rows:
        raise ComponentError(f"{where}.row_id names '{key[0]}', which rows does not declare")
    if key[1] not in columns:
        raise ComponentError(f"{where}.column_id names '{key[1]}', which columns does not declare")
    if key not in grid:  # pragma: no cover - unreachable once both ids exist
        raise ComponentError(f"{where} is not a cell of this grid")


#: ``{{blank_id}}`` in a cloze passage. Declared here so the validator and the
#: schema description name the same syntax.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z0-9_-]+)\s*\}\}")


def _check_fill_blank(content: dict[str, Any], answer: dict[str, Any], path: str) -> None:
    """Every gap in the passage is a declared blank, and every blank is a gap.

    Both directions matter. A placeholder with no blank is a gap nothing can
    grade; a blank with no placeholder is an answer the learner is never
    given anywhere to write.
    """
    declared = _content_ids(content, "blanks")
    found = _PLACEHOLDER_RE.findall(content.get("text", ""))

    unknown = sorted({name for name in found if name not in declared})
    if unknown:
        raise ComponentError(
            f"{path}.content.text uses placeholder(s) {', '.join(unknown)}, which blanks "
            "does not declare"
        )
    repeated = sorted({name for name in found if found.count(name) > 1})
    if repeated:
        raise ComponentError(
            f"{path}.content.text repeats placeholder(s) {', '.join(repeated)}; each blank "
            "needs its own identifier"
        )
    missing = [name for name in declared if name not in found]
    if missing:
        raise ComponentError(
            f"{path}.content.text has no gap for blank(s) {', '.join(missing)}; write each "
            "one as {{blank_id}} where it belongs"
        )


#: How many coordinates each hotspot shape needs. A region with the wrong
#: count is not a near-miss — it is a shape nothing can be tested against.
_REGION_POINTS = {"rectangle": 4, "circle": 3}


def _check_hotspot(content: dict[str, Any], answer: dict[str, Any], path: str) -> None:
    for index, region in enumerate(answer.get("regions", [])):
        shape = region["shape"]
        points = region["points"]
        expected = _REGION_POINTS.get(shape)
        where = f"{path}.answer.regions[{index}].points"
        if expected is not None and len(points) != expected:
            raise ComponentError(f"{where} must have {expected} values for a {shape}")
        if shape == "polygon" and (len(points) < 6 or len(points) % 2):
            raise ComponentError(f"{where} must be at least three x,y pairs for a polygon")


def _check_confidence_scale(content: dict[str, Any], answer: dict[str, Any], path: str) -> None:
    low, high = content.get("scale_min"), content.get("scale_max")
    if low is not None and high is not None and low >= high:
        raise ComponentError(f"{path}.content.scale_min must be below scale_max")
    labels = content.get("scale_labels")
    if labels is None or low is None or high is None:
        return
    if len(labels) != high - low + 1:
        raise ComponentError(
            f"{path}.content.scale_labels must have one label per point on the scale"
        )


def _check_categorization(content: dict[str, Any], answer: dict[str, Any], path: str) -> None:
    """Grouping rules, which are per item rather than across the whole answer.

    Two items in one category is the normal shape of a grouping task. What is
    not allowed is the same category twice *for one item*, an item placed in
    several categories when the component did not say that was possible, or a
    category nobody declared.
    """
    declared = set(_content_ids(content, "categories"))
    allow_multiple = content.get("allow_multiple", False)

    for index, entry in enumerate(answer.get("assignments", [])):
        where = f"{path}.answer.assignments[{index}].category_ids"
        chosen = entry.get("category_ids", [])
        unknown = {value for value in chosen if value not in declared}
        if unknown:
            raise ComponentError(
                f"{where} names {len(unknown)} id(s) that categories does not declare"
            )
        if len(set(chosen)) != len(chosen):
            raise ComponentError(f"{where} names the same category more than once")
        if not allow_multiple and len(chosen) != 1:
            raise ComponentError(
                f"{where} places one item in {len(chosen)} categories, but this component "
                "did not set content.allow_multiple"
            )


def _check_error_correction(content: dict[str, Any], answer: dict[str, Any], path: str) -> None:
    """Each correction must claim a real, distinct place in the passage.

    Resolved to a *span* — a range of token positions in the passage — rather
    than compared as strings. Two entries reading ``are`` and ``are.`` are the
    same word in the same place once punctuation is normalised away, and a
    string comparison that normalises differently from the occurrence search
    lets both through: the learner is told to find two errors where there is
    one, and one of the two can never be marked correct.

    Spans are claimed in order, so a word that genuinely appears twice can be
    corrected twice — the second entry takes the second occurrence.
    """
    corrections = answer.get("corrections", [])
    passage = tokens(content.get("text", ""))
    claimed: set[int] = set()

    for index, correction in enumerate(corrections):
        where = f"{path}.answer.corrections[{index}]"
        wrong = tokens(correction["incorrect"])
        if not wrong:
            raise ComponentError(f"{where}.incorrect must name the text to be corrected")
        if wrong == tokens(correction["correct"]):
            raise ComponentError(f"{where} leaves the text unchanged, so there is nothing to fix")

        span = _first_unclaimed_span(passage, wrong, claimed)
        if span is None:
            raise ComponentError(
                f"{where}.incorrect does not appear in content.text at a place no other "
                "correction has already claimed, so the learner has nothing to correct there"
            )
        claimed.add(span)

    declared = content.get("error_count")
    if declared is not None and declared != len(claimed):
        raise ComponentError(
            f"{path}.content.error_count says {declared}, but the answer corrects "
            f"{len(claimed)} distinct place(s) in the passage. They must be the same number."
        )


def _first_unclaimed_span(passage: list[str], wrong: list[str], claimed: set[int]) -> int | None:
    """The start index of the first occurrence of *wrong* nobody has taken."""
    span = len(wrong)
    for start in range(len(passage) - span + 1):
        if start in claimed:
            continue
        if passage[start : start + span] == wrong:
            return start
    return None


#: Per-type checks that need to see the whole component at once.
_CROSS_CHECKS: dict[str, tuple[Any, ...]] = {
    "decision_path": (_check_decision_path,),
    "categorization": (_check_categorization,),
    "error_correction": (_check_error_correction,),
    "fill_blank": (_check_fill_blank,),
    "table_grid": (_check_table_grid,),
    "hotspot": (_check_hotspot,),
    "confidence_rating": (_check_confidence_scale,),
}


def _check_word_bounds(content: dict[str, Any], path: str) -> None:
    """A minimum above the maximum is a length requirement nothing can meet."""
    low, high = content.get("min_words"), content.get("max_words")
    if low is not None and high is not None and low > high:
        raise ComponentError(f"{path}.content.min_words must not exceed max_words")


def _check_scoring(spec: ComponentSpec, evaluation: dict[str, Any], path: str) -> None:
    """Refuse a scoring definition that contradicts the component itself.

    The mode enum in the schema already narrows to what this type can be
    marked with; this re-checks it, because the schema is guidance and the
    runtime is the boundary.
    """
    if spec.self_report and evaluation.get("rubric"):
        raise ComponentError(
            f"{path}.evaluation.rubric does not apply to a self-report component: nobody "
            "marks how confident someone says they feel"
        )

    scoring = evaluation.get("scoring")
    if scoring is None:
        return
    mode = scoring.get("mode")
    if mode not in spec.scoring_modes:
        raise ComponentError(
            f"{path}.evaluation.scoring.mode '{mode}' does not apply to a {spec.type} "
            f"component; use one of: {', '.join(spec.scoring_modes)}"
        )
    if mode == "rubric" and not evaluation.get("rubric"):
        raise ComponentError(f"{path}.evaluation.scoring.mode is 'rubric' but no rubric is defined")


def _check_feedback(spec: ComponentSpec, content: dict[str, Any], evaluation, path: str) -> None:
    """Per-option feedback must name an option this component actually has.

    Feedback attached to an option that does not exist is feedback nobody will
    ever see, and two entries for one option is a coin toss over which the
    learner gets.
    """
    per_option = (evaluation.get("feedback") or {}).get("per_option") or []
    if not per_option:
        return

    declared = set(_content_ids(content, "options"))
    if not declared:
        raise ComponentError(
            f"{path}.evaluation.feedback.per_option does not apply to a {spec.type} "
            "component, which has no options"
        )

    seen: set[str] = set()
    for index, entry in enumerate(per_option):
        option_id = entry["option_id"]
        where = f"{path}.evaluation.feedback.per_option[{index}].option_id"
        if option_id not in declared:
            raise ComponentError(f"{where} names an id that options does not declare")
        if option_id in seen:
            raise ComponentError(f"{where} gives the same option feedback twice")
        seen.add(option_id)


def _check_answer_leak(spec: ComponentSpec, component: Component, path: str) -> None:
    """Refuse an answer the learner can already read in the question.

    Applied to every component whose hidden answer holds text the learner is
    expected to *produce*: cloze gaps, short answers, translations, the back
    of a flashcard, a recall target, a corrected sentence, a reference
    solution, a grid cell. Selection components are exempt because their key
    is an opaque option id — the option *text* is meant to be visible, and
    that is the whole format.

    Three comparisons, over the complete recursive learner-visible projection:

    1. **Token sequence.** The answer's words, in order, on word boundaries.
       That is what lets ``Na`` be checked without matching inside
       "national", and keeps "Define photosynthesis" legal while refusing
       "Type Paris. The answer is Paris."
    2. **Separated spelling.** The same characters with separators between
       them — ``P.a.r.i.s`` is ``Paris`` written to defeat a tokeniser, and a
       learner reads it as the answer either way.
    3. **Symbol form.** An answer with no word characters at all (``+``,
       ``===``, a tick) tokenises to nothing, so it is compared as a
       normalised symbol string instead.

    None of these is fuzzy: no edit distance, no similarity, no substring
    rule over words, and nothing asks a model. And the refusal names the
    field, never the value — an error that quoted the answer back would be
    the disclosure this check exists to prevent.
    """
    if not spec.leak_paths:
        return
    visible = _flatten_text(_leak_surface(spec, component))
    visible_tokens = tokens(visible)
    visible_symbols = symbol_form(visible)

    for leak_path in spec.leak_paths:
        for value in _walk(component.answer, leak_path):
            for text in value if isinstance(value, list) else [value]:
                if isinstance(text, str) and _is_readable(
                    text, visible, visible_tokens, visible_symbols
                ):
                    raise ComponentError(
                        f"{path}.answer.{leak_path} is already readable in the part the "
                        "learner sees before answering. Rewrite the question so it does "
                        "not contain what it is asking for."
                    )


def _is_readable(
    answer: str, visible: str, visible_tokens: list[str], visible_symbols: str
) -> bool:
    """True when *answer* can be read off the visible half, by any of the three rules."""
    answer_tokens = tokens(answer)
    if answer_tokens and contains_token_sequence(visible_tokens, answer_tokens):
        return True

    symbols = symbol_form(answer)
    if not answer_tokens and symbols and symbols in visible_symbols:
        return True

    spelled = spelled_out_pattern(answer)
    return bool(spelled and spelled.search(unicodedata.normalize("NFKC", visible)))


def _leak_surface(spec: ComponentSpec, component: Component) -> dict[str, Any]:
    """The visible half, minus anything an answer may legitimately repeat.

    Only one exception exists, and it is a real one: a grid's prefilled cells
    are visible *worked examples*, and two cells of a comparison table may
    honestly hold the same value — "4" filled in for one organism and "4"
    expected for another. A prefilled cell that duplicates its own cell's
    answer is refused separately, by :func:`_check_table_grid`.
    """
    payload = component.learner_payload()
    if spec.type != "table_grid":
        return payload
    content = {k: v for k, v in payload.get("content", {}).items() if k != "prefilled_cells"}
    return {**payload, "content": content}


def _flatten_text(value: Any) -> str:
    """Every string anywhere in a structure, concatenated."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return ""


def component_schema(spec: ComponentSpec) -> dict[str, Any]:
    """The JSON Schema branch for one component type."""
    schema = object_schema(component_members(spec))
    # A discriminator the model can see and a validator can dispatch on. The
    # summary rides on it rather than on the branch, so it is stated once.
    schema["properties"]["type"]["const"] = spec.type
    return schema


def components_schema() -> dict[str, Any]:
    """The discriminated union of every component type."""
    return {"oneOf": [component_schema(spec) for spec in SPECS]}
