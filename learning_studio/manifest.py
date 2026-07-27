"""The experience manifest: the envelope around an ordered set of components.

A manifest is the whole contract between the agent that designs a learning
experience and everything that will later deliver one. It is data — never
renderer code — and this module is where it becomes trustworthy data:

- **The learner is not in it.** Ownership comes from the Hermes session
  principal, and there is deliberately no field through which a caller could
  name a learner, a platform user, a username, a display name, a session, or a
  profile. See :mod:`learning_studio.identity`.
- **The identifier is not in it either.** The experience id is generated on
  storage. A caller who could choose it could try to overwrite someone else's.
- **Accessibility metadata says where it came from.** The only accepted
  sources are the three that PR 03 already treats as authoritative: what the
  learner asked for now, what they confirmed on a track, and what the operator
  configured. Inference and evidence are not among them, so an exercise cannot
  encode a guess about somebody's health, and preparing one never creates a
  durable fact or a memory candidate.
- **Sources are described, not linked.** Provenance is bounded descriptive
  metadata. There is no URL and no path, because this PR must not be able to
  fetch anything and a field that looks fetchable invites a later PR to try.

Every bound and every rule below is checked at runtime against the same
declarations the JSON Schema is generated from, so the advertised contract and
the enforced one are the same object.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .components import (
    Bool,
    Component,
    ComponentError,
    Enum,
    Field,
    Int,
    Locale,
    Obj,
    ObjList,
    Text,
    build_component,
    object_schema,
    validate_object,
)
from .models import OBJECTIVE_TEXT_MAX, Provenance
from .safety import UnsafeContent, safe_date, safe_identifier, serialized_size

#: The manifest format's own version, independent of the database schema
#: version. A stored experience records the format it was written in, so a
#: later reader never has to guess which rules produced it.
MANIFEST_SCHEMA_VERSION = 1

MAX_COMPONENTS = 40
MAX_SOURCE_REFERENCES = 10

#: Measured on the compact JSON form, because that is what is stored and what
#: a client would eventually be sent.
MAX_MANIFEST_BYTES = 128 * 1024

TITLE_MAX = 200
INSTRUCTIONS_MAX = 2000
NOTE_MAX = 1000
CITATION_MAX = 300

MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 240

#: Deliberately generic. Nothing here names a subject, a year group, or a
#: national curriculum band, because the registry has to serve a physics
#: revision session and a beginner's alphabet drill equally.
DIFFICULTIES: tuple[str, ...] = ("introductory", "intermediate", "advanced", "expert")

DELIVERY_MODES: tuple[str, ...] = ("practice", "assessment", "review")

READING_LEVELS: tuple[str, ...] = ("standard", "plain", "simplified")

#: Where accessibility metadata on an exercise may come from, as PR 03's
#: provenance vocabulary. Built from :class:`Provenance` so the two cannot
#: drift: adding a provenance there does not silently widen what an exercise
#: may claim here.
ACCESSIBILITY_SOURCES: tuple[str, ...] = (
    Provenance.EXPLICIT_REQUEST.value,
    Provenance.CONFIRMED_TRACK.value,
    Provenance.PROFILE_CONFIG.value,
)


class ManifestError(ValueError):
    """A manifest failed validation. The message is safe to show the agent."""


# ── Declared shape ────────────────────────────────────────────────────────

_OBJECTIVE = Obj(
    "objective",
    required=True,
    description=(
        "The measurable objective this experience assesses: observable behaviour, "
        "the condition, and the standard that says when it is met."
    ),
    members=(
        Text("behavior", required=True, max_chars=OBJECTIVE_TEXT_MAX),
        Text("condition", required=True, max_chars=OBJECTIVE_TEXT_MAX),
        Text("standard", required=True, max_chars=OBJECTIVE_TEXT_MAX),
    ),
)

_SOURCE_REFERENCE = ObjList(
    "source_references",
    max_items=MAX_SOURCE_REFERENCES,
    description=(
        "Where the material came from, as description only. No links, no paths, "
        "no credentials — nothing here is fetched."
    ),
    members=(
        Text("title", required=True, max_chars=CITATION_MAX),
        Text("author", max_chars=TITLE_MAX, description="Author or organisation."),
        Text("published_on", max_chars=10, description="YYYY, YYYY-MM, or YYYY-MM-DD."),
        Text("citation", max_chars=CITATION_MAX, description="A stable citation label."),
        Text(
            "source_id",
            max_chars=64,
            description="An approved source identifier, if this profile has one.",
        ),
        Text("note", max_chars=NOTE_MAX, multiline=True),
    ),
)

_ACCESSIBILITY = Obj(
    "accessibility",
    description=(
        "What this experience needs in order to be usable. Describes the exercise, "
        "never the learner: no diagnosis, no disability, no inference."
    ),
    members=(
        Enum(
            "source",
            required=True,
            choices=ACCESSIBILITY_SOURCES,
            description=(
                "Where this came from. Only an explicit request, a confirmed track, or "
                "operator configuration. Never your own inference."
            ),
        ),
        Bool("text_alternatives_required"),
        Bool("captions_required"),
        Bool("visual_description_required"),
        Bool("keyboard_only"),
        Bool("reduced_motion"),
        Bool("no_time_limit"),
        Enum("reading_level", choices=READING_LEVELS),
        Text("notes", max_chars=NOTE_MAX, multiline=True),
    ),
)

_DELIVERY = Obj(
    "delivery",
    description="How a later runtime should present this. No styling, ever.",
    members=(
        Enum("mode", choices=DELIVERY_MODES),
        Bool("allow_back"),
        Bool("allow_skip"),
        Int("time_limit_seconds", minimum=0, maximum=7200),
    ),
)


def manifest_members() -> tuple[Field, ...]:
    """Every top-level manifest field, in the order they read best."""
    return (
        Int(
            "schema_version",
            required=True,
            minimum=MANIFEST_SCHEMA_VERSION,
            maximum=MANIFEST_SCHEMA_VERSION,
            description=f"Must be {MANIFEST_SCHEMA_VERSION}.",
        ),
        Text("title", required=True, max_chars=TITLE_MAX),
        _OBJECTIVE,
        Text(
            "instructions",
            required=True,
            max_chars=INSTRUCTIONS_MAX,
            multiline=True,
            description="What the learner is told before they start, in their words.",
        ),
        Locale("ui_locale", required=True, description="Language of instructions and feedback."),
        Locale("content_locale", description="Language of the material, when it differs."),
        Int(
            "expected_duration_minutes",
            required=True,
            minimum=MIN_DURATION_MINUTES,
            maximum=MAX_DURATION_MINUTES,
        ),
        Enum("difficulty", required=True, choices=DIFFICULTIES),
        _SOURCE_REFERENCE,
        _ACCESSIBILITY,
        _DELIVERY,
    )


# ── The validated manifest ────────────────────────────────────────────────


@dataclass(frozen=True)
class Manifest:
    """A validated experience, ready to store.

    Holds no learner and no experience id: both are supplied by the service
    from trusted state, never by the caller.
    """

    schema_version: int
    title: str
    objective: dict[str, str]
    instructions: str
    ui_locale: str
    content_locale: str | None
    expected_duration_minutes: int
    difficulty: str
    source_references: tuple[dict[str, Any], ...]
    accessibility: dict[str, Any]
    delivery: dict[str, Any]
    components: tuple[Component, ...]

    @property
    def component_count(self) -> int:
        return len(self.components)

    def learner_payloads(self) -> list[dict[str, Any]]:
        """Every component's safe projection, in delivery order."""
        return [component.learner_payload() for component in self.components]

    def learner_summary(self) -> dict[str, Any]:
        """What is safe to hand back to the agent, and through it the learner.

        Built from the manifest's visible half only. The component entries
        carry the prompt — which the learner is going to be shown anyway — and
        nothing from ``evaluation``, so an agent can narrate the exercise
        without ever holding an answer key it might repeat.
        """
        summary: dict[str, Any] = {
            "schema_version": self.schema_version,
            "title": self.title,
            "objective": dict(self.objective),
            "instructions": self.instructions,
            "ui_locale": self.ui_locale,
            "expected_duration_minutes": self.expected_duration_minutes,
            "difficulty": self.difficulty,
            "component_count": self.component_count,
            "components": [
                {
                    "position": position,
                    "component_id": component.id,
                    "type": component.type,
                    "prompt": component.prompt,
                }
                for position, component in enumerate(self.components, start=1)
            ],
        }
        if self.content_locale:
            summary["content_locale"] = self.content_locale
        if self.accessibility:
            summary["accessibility"] = dict(self.accessibility)
        if self.source_references:
            summary["source_references"] = [dict(ref) for ref in self.source_references]
        if self.delivery:
            summary["delivery"] = dict(self.delivery)
        return summary


def build_manifest(raw: Any) -> Manifest:
    """Validate a complete manifest, or refuse the whole thing.

    Whole-manifest: one bad component rejects the request rather than storing
    the rest, because an experience missing the item that failed is not a
    smaller experience, it is a broken one the agent believes is intact.
    """
    if not isinstance(raw, dict):
        raise ManifestError("the manifest must be an object")

    _check_size(raw)

    envelope = {key: value for key, value in raw.items() if key != "components"}
    try:
        validated = validate_object(manifest_members(), envelope, "manifest")
    except ComponentError as exc:
        raise ManifestError(str(exc)) from exc
    except UnsafeContent as exc:  # pragma: no cover - normalised inside validate_object
        raise ManifestError(str(exc)) from exc

    if validated["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"manifest.schema_version must be {MANIFEST_SCHEMA_VERSION}, "
            f"got {validated['schema_version']}"
        )

    components = _build_components(raw.get("components"))
    validate_branching(components)

    references = tuple(
        _validated_reference(reference, index)
        for index, reference in enumerate(validated.get("source_references", []))
    )

    manifest = Manifest(
        schema_version=validated["schema_version"],
        title=validated["title"],
        objective=validated["objective"],
        instructions=validated["instructions"],
        ui_locale=validated["ui_locale"],
        content_locale=validated.get("content_locale"),
        expected_duration_minutes=validated["expected_duration_minutes"],
        difficulty=validated["difficulty"],
        source_references=references,
        accessibility=validated.get("accessibility", {}),
        delivery=validated.get("delivery", {}),
        components=components,
    )
    # The bound applies to what will actually be stored, not only to what
    # arrived: validation normalises text, and a manifest that grew past the
    # limit on the way through is still too big to keep.
    _check_size(
        {
            "envelope": validated,
            "components": [
                {"payload": c.learner_payload(), "hidden": c.hidden()} for c in components
            ],
        }
    )
    return manifest


def _check_size(payload: Any) -> None:
    size = serialized_size(payload)
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"the manifest is {size} bytes, above the {MAX_MANIFEST_BYTES}-byte limit. "
            "Split it into shorter experiences."
        )


def _build_components(raw: Any) -> tuple[Component, ...]:
    if not isinstance(raw, list):
        raise ManifestError("manifest.components must be an array")
    if not raw:
        raise ManifestError("manifest.components must contain at least one component")
    if len(raw) > MAX_COMPONENTS:
        raise ManifestError(
            f"manifest.components must have at most {MAX_COMPONENTS} components, got {len(raw)}"
        )

    components: list[Component] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        try:
            component = build_component(entry, f"manifest.components[{index}]")
        except (ComponentError, UnsafeContent) as exc:
            raise ManifestError(str(exc)) from exc
        if component.id in seen:
            raise ManifestError(
                f"manifest.components[{index}].id is a duplicate: '{component.id}'. "
                "Component ids must be unique within an experience."
            )
        seen.add(component.id)
        components.append(component)
    return tuple(components)


def _validated_reference(reference: dict[str, Any], index: int) -> dict[str, Any]:
    """Second pass over a source reference, for its two shaped fields.

    ``published_on`` and ``source_id`` arrive as bounded safe text, which
    stops markup and credentials; this narrows them further to a date and to
    an identifier, so neither can carry prose that a later reader would be
    tempted to treat as a locator.
    """
    out = dict(reference)
    where = f"manifest.source_references[{index}]"
    try:
        if "published_on" in out:
            out["published_on"] = safe_date(out["published_on"], f"{where}.published_on")
        if "source_id" in out:
            out["source_id"] = safe_identifier(out["source_id"], f"{where}.source_id")
    except UnsafeContent as exc:
        raise ManifestError(str(exc)) from exc
    return out


def validate_branching(components: tuple[Component, ...]) -> None:
    """Refuse branches that dangle, loop on themselves, or cannot be escaped.

    Three separate failures, each with its own reason:

    - **Dangling.** A branch to a component that is not in this manifest ends
      the experience somewhere undefined.
    - **Self-reference.** A component that branches to itself is a loop with
      no other exit.
    - **Unescapable cycle.** ``always`` is unconditional, so a cycle made only
      of ``always`` edges never terminates whatever the learner does. Cycles
      through ``correct``/``incorrect`` are left alone on purpose: "get it
      wrong, go back and try again" is a legitimate retry loop, and the
      learner's own answer is the way out.
    """
    known = {component.id for component in components}

    always_edges: dict[str, str] = {}
    for component in components:
        conditions = [condition for condition, _ in component.branch_targets()]
        if conditions.count("always") > 1:
            raise ManifestError(
                f"component '{component.id}' declares more than one unconditional branch"
            )
        if "always" in conditions and len(conditions) > 1:
            raise ManifestError(
                f"component '{component.id}' mixes an unconditional branch with conditional "
                "ones, so the conditional branches could never be taken"
            )
        for condition, target in component.branch_targets():
            if target not in known:
                raise ManifestError(
                    f"component '{component.id}' branches to '{target}', which is not a "
                    "component of this experience"
                )
            if target == component.id:
                raise ManifestError(f"component '{component.id}' branches to itself")
            if condition == "always":
                always_edges[component.id] = target

    _reject_cycles(always_edges)


def _reject_cycles(edges: dict[str, str]) -> None:
    """Walk each unconditional chain; a repeat visit is a closed loop."""
    for start in edges:
        seen = {start}
        node = start
        while node in edges:
            node = edges[node]
            if node in seen:
                raise ManifestError(
                    f"the unconditional branches starting at '{start}' form a loop with no "
                    "way out; a learner would never reach the end"
                )
            seen.add(node)


# ── Schema ────────────────────────────────────────────────────────────────


def manifest_schema(components_schema: dict[str, Any]) -> dict[str, Any]:
    """The manifest's JSON Schema, with the component union spliced in."""
    schema = object_schema(manifest_members())
    schema["properties"]["components"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_COMPONENTS,
        "description": "The components, in the order the learner works through them.",
        "items": components_schema,
    }
    schema["required"] = [*schema.get("required", []), "components"]
    return schema


def dumps(value: Any) -> str:
    """Compact JSON, for storage columns."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
