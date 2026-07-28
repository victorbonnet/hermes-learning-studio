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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .components import (
    SPEC_BY_TYPE,
    Bool,
    Component,
    ComponentError,
    Date,
    Enum,
    EnumList,
    Field,
    Ident,
    Int,
    Locale,
    Obj,
    ObjList,
    Text,
    build_component,
    object_schema,
    validate_object,
    without_hidden_data_across,
)
from .models import OBJECTIVE_TEXT_MAX, Provenance
from .safety import UnsafeContent, serialized_size

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

#: What an exercise may declare it needs, as a closed vocabulary.
#:
#: Every one of these describes the *exercise*: what it must provide in order
#: to be usable. None of them describes a person, and there is deliberately no
#: free-text field beside them — a box to type in is a box someone will type a
#: diagnosis into, and this metadata is neither consented to nor appropriate
#: for one. The same tokens are what a learner's accessibility needs are
#: recorded as, so authorisation is an exact match rather than a judgement.
ACCOMMODATIONS: tuple[str, ...] = (
    "captions",
    "transcript",
    "text_alternatives",
    "visual_description",
    "keyboard_only",
    "reduced_motion",
    "no_time_limit",
    "extended_time",
    "plain_language",
)

#: Accommodations that constrain how components must be built, and the check
#: that decides whether the experience can actually deliver them.
KEYBOARD_ONLY = "keyboard_only"
CAPTIONS = "captions"
TRANSCRIPT = "transcript"
TEXT_ALTERNATIVES = "text_alternatives"
VISUAL_DESCRIPTION = "visual_description"
NO_TIME_LIMIT = "no_time_limit"

#: Where accessibility metadata on an exercise may come from. One source.
#:
#: ``explicit_request`` went first: it was checked against a context row the
#: model had written in an earlier call, so the model was authorising itself
#: across two turns. ``confirmed_track`` went the same way for the same
#: reason, one turn shorter — ``track.confirmed: true``, the consent
#: statement, and the need are all fields of a single tool call the model
#: composes, so a "confirmed track" carrying an accommodation is a claim about
#: a claim. Restricting the value to a fixed vocabulary stops a diagnosis
#: being written down; it does not make the learner's agreement true.
#:
#: What is left is the operator's ``config.yaml`` — a file a person edits,
#: which no tool call can reach.
#:
#: A learner's accessibility need can still guide the current conversation:
#: pass it in ``current_request`` when resolving context. It does not authorise
#: manifest accessibility metadata; that remains operator-configured only.
#: What cannot happen is a durable record claiming they agreed to have it kept.
ACCESSIBILITY_SOURCES: tuple[str, ...] = (Provenance.PROFILE_CONFIG.value,)


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
        Date("published_on", description="YYYY, YYYY-MM, or YYYY-MM-DD."),
        Text("citation", max_chars=CITATION_MAX, description="A stable citation label."),
        Ident(
            "source_id",
            description="An approved source identifier, if this profile has one.",
        ),
        Text("note", max_chars=NOTE_MAX, multiline=True),
    ),
)

_ACCESSIBILITY = Obj(
    "accessibility",
    description=(
        "What this experience must provide in order to be usable. Describes the "
        "exercise, never the learner: there is no field for a diagnosis, a disability, "
        "or anything else about who is studying, and the accommodations are a fixed "
        "list rather than free text."
    ),
    members=(
        Enum(
            "source",
            required=True,
            choices=ACCESSIBILITY_SOURCES,
            description=(
                "Where this came from. Only operator profile configuration is accepted: "
                "a tool call cannot create or alter that source. Explicit requests and "
                "model-created tracks do not authorise durable accessibility metadata."
            ),
        ),
        EnumList(
            "accommodations",
            required=True,
            choices=ACCOMMODATIONS,
            description=(
                "Each one must already be recorded in the operator's profile configuration."
            ),
        ),
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

    # Cross-component validation happens inside the scrub, not outside it.
    # Component-level checks are redacted where they are raised, but a branch
    # target lives in one component's ``evaluation`` and is validated against
    # *all* of them — so an invalid ``go_to`` used to travel back to the
    # caller verbatim, past every per-component guard.
    with _hidden_data_withheld(raw.get("components")):
        validate_branching(components)

        accessibility = validated.get("accessibility", {})
        validate_accessibility_support(
            tuple(accessibility.get("accommodations", ())),
            components,
            validated.get("delivery", {}),
        )

    references = tuple(validated.get("source_references", []))

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
        accessibility=accessibility,
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


@contextmanager
def _hidden_data_withheld(components: Any) -> Iterator[None]:
    """Redact evaluator-only values from any error raised inside the block.

    Applied across the whole component list, because whole-manifest checks
    compare one component's hidden fields against another's visible ones and
    an error can therefore name either.
    """
    try:
        yield
    except ManifestError as exc:
        raise ManifestError(
            without_hidden_data_across(str(exc), components, "this experience")
        ) from None


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


#: The terminal state of an experience: the learner has finished it. Modelled
#: explicitly so that "can this component still reach the end?" is a question
#: with an answer rather than an assumption about the last index.
COMPLETE = object()


def validate_branching(components: tuple[Component, ...]) -> None:
    """Refuse branches that dangle, repeat, loop on themselves, or trap.

    Four failures, each with its own reason:

    - **Dangling.** A branch to a component that is not in this manifest ends
      the experience somewhere undefined.
    - **Self-reference.** A component that branches to itself is a loop with
      no other exit.
    - **Ambiguous.** Two branches for the same outcome, or an ``always``
      branch beside a conditional one, leave it undecided where the learner
      goes.
    - **Unescapable.** A set of components from which *no* sequence of learner
      outcomes ever reaches the end. That is the check that matters, and it is
      the one the first version of this got wrong: it looked only at
      unconditional edges, so two components that sent each other back and
      forth on both ``correct`` and ``incorrect`` passed.

    A retry loop is still legal, because it has a way out. "Get this wrong and
    go back" branches only on ``incorrect``; answering correctly falls through
    to the next component, and falling through is a modelled transition here,
    not an assumption.
    """
    known = {component.id for component in components}
    for component in components:
        _check_branch_shape(component, known)
    _reject_traps(components)


def _check_branch_shape(component: Component, known: set[str]) -> None:
    seen: set[str] = set()
    for condition, target in component.branch_targets():
        if condition in seen:
            raise ManifestError(
                f"component '{component.id}' declares more than one '{condition}' branch, so "
                "where the learner goes on that outcome is undecided"
            )
        seen.add(condition)
        if target not in known:
            # The target itself is evaluator-only — which component a wrong
            # answer leads to is part of the answer — so the message names the
            # branch, not where it points.
            raise ManifestError(
                f"component '{component.id}' has an '{condition}' branch whose go_to is "
                "not a component of this experience"
            )
        if target == component.id:
            raise ManifestError(f"component '{component.id}' branches to itself")

    if "always" in seen and len(seen) > 1:
        raise ManifestError(
            f"component '{component.id}' mixes an unconditional branch with conditional "
            "ones, so the conditional branches could never be taken"
        )


def _transitions(components: tuple[Component, ...]) -> dict[str, set[Any]]:
    """Every state each component can move to, over all learner outcomes.

    Three kinds of transition, and the third is the one that is easy to
    forget: when a component declares no branch for an outcome, that outcome
    falls through to the next component in order — or, from the last one, to
    completion.
    """
    order = [component.id for component in components]
    moves: dict[str, set[Any]] = {}
    for index, component in enumerate(components):
        branches = dict(component.branch_targets())
        targets: set[Any] = set(branches.values())
        unconditional = "always" in branches
        covered = {"correct", "incorrect"} <= set(branches)
        if not unconditional and not covered:
            targets.add(order[index + 1] if index + 1 < len(order) else COMPLETE)
        moves[component.id] = targets
    return moves


def _reject_traps(components: tuple[Component, ...]) -> None:
    """Every component must be able to reach the end by some route.

    Computed as reachability *backwards* from completion: whatever cannot
    reach it is, by definition, a set the learner can enter and never leave —
    whichever answers they give.
    """
    moves = _transitions(components)
    reaches_end = {COMPLETE}
    changed = True
    while changed:
        changed = False
        for component_id, targets in moves.items():
            if component_id not in reaches_end and targets & reaches_end:
                reaches_end.add(component_id)
                changed = True

    trapped = [c.id for c in components if c.id not in reaches_end]
    if trapped:
        raise ManifestError(
            f"component(s) {', '.join(trapped)} can never reach the end of the experience: "
            "whatever the learner answers, the branches lead back into the same group. "
            "Leave at least one outcome unbranched so it falls through."
        )


# ── Accessibility the experience can actually deliver ──────────────────────

#: The field that marks a managed asset wherever it appears in content.
_ASSET_MARKER = "asset_ref"


def validate_accessibility_support(
    accommodations: tuple[str, ...], components: tuple[Component, ...], delivery: dict[str, Any]
) -> None:
    """Refuse an experience that cannot honour what it declares.

    A manifest claiming ``keyboard_only`` while containing a hotspot with no
    keyboard alternative is worse than one that claims nothing: it tells a
    future runtime, and the learner, that the exercise is usable when it is
    not. Each accommodation is checked independently — captions do not stand
    in for a transcript, and a transcript does not stand in for captions;
    they are different accommodations for different people.
    """
    required = set(accommodations)

    if NO_TIME_LIMIT in required and delivery.get("time_limit_seconds"):
        raise ManifestError(
            "this experience declares no_time_limit but delivery.time_limit_seconds sets "
            "one. Remove the limit, or do not declare the accommodation."
        )

    for component in components:
        access = component.accessibility
        spec = SPEC_BY_TYPE[component.type]
        assets = _component_assets(component)

        if (
            KEYBOARD_ONLY in required
            and spec.pointer_interaction
            and not access.get("keyboard_alternative")
        ):
            raise ManifestError(
                f"component '{component.id}' is answered by pointing or dragging, but this "
                "experience declares keyboard_only. Give it "
                "accessibility.keyboard_alternative describing how to answer with a "
                "keyboard, or use a component that does not need a pointer."
            )

        if not assets:
            if (
                TEXT_ALTERNATIVES in required
                and spec.family == "visual"
                and not access.get("alt_text")
            ):
                raise ManifestError(
                    f"component '{component.id}' is a visual component with no asset and no "
                    "accessibility.alt_text, but this experience declares text_alternatives."
                )
            continue

        if CAPTIONS in required and not access.get("caption"):
            raise ManifestError(
                f"component '{component.id}' carries an asset, but this experience declares "
                "captions and the component has no accessibility.caption. A caption is the "
                "caption text itself, not a flag saying one is needed."
            )
        if TRANSCRIPT in required and not access.get("transcript"):
            raise ManifestError(
                f"component '{component.id}' carries an asset, but this experience declares "
                "transcript and the component has no accessibility.transcript. A caption is "
                "not a transcript; write the spoken content out."
            )
        if VISUAL_DESCRIPTION in required and not (
            all(asset.get("long_description") for asset in assets) or access.get("long_description")
        ):
            raise ManifestError(
                f"component '{component.id}' carries an asset, but this experience "
                "declares visual_description and the component has no long description."
            )


def _component_assets(component: Component) -> list[dict[str, Any]]:
    """Every managed asset in the visible half, however deeply nested.

    Found by structure rather than by field name: ``image_choice`` carries one
    asset per option, and a check that only looked at ``content.image`` would
    have declared that component caption-free and let the claim stand.
    """
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _ASSET_MARKER in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(component.content)
    return found


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
