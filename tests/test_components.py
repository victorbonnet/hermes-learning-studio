"""The trusted component registry, exercised once per component type.

Every test here is parametrized over the whole registry rather than over a
sample. A component type nobody wrote a test for is exactly the one whose
answer key ends up in the learner's payload, so the parametrisation is derived
from ``COMPONENT_TYPES`` and a type without an example fails immediately.
"""

from __future__ import annotations

import copy
import json

import pytest

from learning_studio.components import (
    COMPONENT_TYPES,
    FAMILIES,
    HIDDEN_KEYS,
    LEARNER_VISIBLE_KEYS,
    SPEC_BY_TYPE,
    SPECS,
    ComponentError,
    build_component,
    component_schema,
    components_schema,
    shared_definitions,
)
from learning_studio.validation import SchemaViolation, validate
from tests.component_examples import CANARY, EXAMPLES, example


def build(component_type: str, **overrides):
    return build_component(example(component_type, **overrides), "component")


def strings_in(value) -> list[str]:
    """Every string anywhere in a structure, however deeply nested."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in strings_in(item)]
    if isinstance(value, list):
        return [s for item in value for s in strings_in(item)]
    return []


# ── The registry covers what it claims to ─────────────────────────────────


def test_every_required_family_is_represented():
    assert set(FAMILIES) == {
        "selection",
        "text_input",
        "ordering",
        "recall",
        "visual",
        "timeline",
        "structured",
        "scenario",
        "reflection",
    }


def test_the_registry_contains_exactly_the_agreed_types():
    """The list is the PR's contract; a rename is a breaking change."""
    assert sorted(COMPONENT_TYPES) == sorted(
        [
            "multiple_choice",
            "multi_select",
            "true_false",
            "classification",
            "fill_blank",
            "short_answer",
            "free_response",
            "translation",
            "error_correction",
            "code_response",
            "sentence_order",
            "sequence_order",
            "matching",
            "categorization",
            "flashcard",
            "typed_recall",
            "image_observation",
            "image_choice",
            "diagram",
            "hotspot",
            "labeling",
            "timeline",
            "process_flow",
            "table_grid",
            "scenario_choice",
            "decision_path",
            "case_study",
            "confidence_rating",
            "self_explanation",
            "reflection",
            "rubric_response",
        ]
    )


def test_every_type_has_an_example():
    """Guards the parametrisation below against silently shrinking."""
    assert sorted(EXAMPLES) == sorted(COMPONENT_TYPES)


def test_the_registry_names_no_subject():
    """A type that presumed a discipline would not be a generic registry."""
    blob = " ".join(f"{spec.type} {spec.family} {spec.summary}" for spec in SPECS).lower()

    for subject in ("spanish", "japanese", "python", "chemistry", "history", "verb", "grammar"):
        assert subject not in blob, f"the registry presumes the subject '{subject}'"


# ── Parsing and round-tripping ────────────────────────────────────────────


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_valid_component_parses(component_type: str):
    component = build(component_type)

    assert component.type == component_type
    assert component.prompt


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_component_round_trips_through_json(component_type: str):
    """Everything stored is JSON, so everything validated must serialise."""
    component = build(component_type)

    payload = json.loads(json.dumps(component.learner_payload()))
    hidden = json.loads(json.dumps(component.hidden()))

    assert payload == component.learner_payload()
    assert hidden == component.hidden()


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_validation_is_idempotent(component_type: str):
    """Re-validating a validated component must produce the same component."""
    once = build(component_type)
    twice = build_component(
        {
            **once.learner_payload(),
            **once.hidden(),
        },
        "component",
    )

    assert twice.learner_payload() == once.learner_payload()
    assert twice.hidden() == once.hidden()


# ── The learner payload is safe by construction ───────────────────────────


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_the_learner_payload_holds_only_allowlisted_keys(component_type: str):
    payload = build(component_type).learner_payload()

    assert set(payload) <= set(LEARNER_VISIBLE_KEYS)


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_the_learner_payload_carries_no_hidden_key(component_type: str):
    payload = build(component_type).learner_payload()

    for hidden_key in HIDDEN_KEYS:
        assert hidden_key not in payload


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_no_canary_reaches_the_learner_payload(component_type: str):
    """Recursive, not top-level: a nested leak is still a leak."""
    payload = build(component_type).learner_payload()

    leaked = [text for text in strings_in(payload) if CANARY in text]
    assert leaked == [], f"{component_type} leaked evaluator-only text: {leaked}"


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_the_hidden_half_actually_holds_the_evaluator_data(component_type: str):
    """The mirror of the test above — proves it is not passing vacuously."""
    hidden = build(component_type).hidden()

    assert any(CANARY in text for text in strings_in(hidden)), (
        f"{component_type}'s example carries no canary, so the leak test above proves nothing"
    )


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_an_answer_key_never_appears_in_the_learner_payload(component_type: str):
    """Whatever shape the answer takes, none of its text may be visible.

    Only strings long enough to be distinctive are compared: a two-character
    option id such as "a" appears in ordinary prose, and asserting on it would
    fail every well-formed exercise while catching nothing.
    """
    component = build(component_type)
    spec = SPEC_BY_TYPE[component_type]
    if spec.answer is None:
        pytest.skip("this type has no answer key")

    visible = " ".join(strings_in(component.learner_payload())).casefold()
    answer_texts = [
        text for text in strings_in(component.answer) if len(text) >= 8 and CANARY in text
    ]
    for text in answer_texts:
        assert text.casefold() not in visible


# ── Closed at every level ─────────────────────────────────────────────────


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_an_unknown_top_level_field_is_rejected(component_type: str):
    with pytest.raises(ComponentError, match="smuggled"):
        build(component_type, smuggled="value")


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_an_unknown_nested_content_field_is_rejected(component_type: str):
    payload = example(component_type)
    if "content" not in payload:
        pytest.skip("this type has no content block")
    payload["content"]["smuggled"] = "value"

    with pytest.raises(ComponentError, match="smuggled"):
        build_component(payload, "component")


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_an_unknown_nested_evaluation_field_is_rejected(component_type: str):
    payload = example(component_type)
    payload.setdefault("evaluation", {})["smuggled"] = "value"

    with pytest.raises(ComponentError, match="smuggled"):
        build_component(payload, "component")


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_null_where_a_value_belongs_is_rejected(component_type: str):
    """``null`` is a caller bug, and honouring it as "absent" hides the bug."""
    with pytest.raises(ComponentError, match="null"):
        build(component_type, prompt=None)


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_missing_prompt_is_rejected(component_type: str):
    payload = example(component_type)
    del payload["prompt"]

    with pytest.raises(ComponentError, match="prompt"):
        build_component(payload, "component")


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_missing_id_is_rejected(component_type: str):
    payload = example(component_type)
    del payload["id"]

    with pytest.raises(ComponentError, match="id"):
        build_component(payload, "component")


def test_an_unknown_component_type_is_rejected():
    with pytest.raises(ComponentError, match="known component types"):
        build_component({"id": "x", "type": "mind_reading", "prompt": "?"}, "component")


def test_a_component_that_is_not_an_object_is_rejected():
    with pytest.raises(ComponentError, match="must be an object"):
        build_component("multiple_choice", "component")


# ── Answer definitions ────────────────────────────────────────────────────


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_malformed_answer_is_rejected(component_type: str):
    spec = SPEC_BY_TYPE[component_type]
    if spec.answer is None:
        pytest.skip("this type has no answer key")

    with pytest.raises(ComponentError):
        build(component_type, answer={"not_a_real_answer_field": "x"})


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_missing_answer_is_rejected(component_type: str):
    spec = SPEC_BY_TYPE[component_type]
    if spec.answer is None:
        pytest.skip("this type has no answer key")

    payload = example(component_type)
    del payload["answer"]

    with pytest.raises(ComponentError, match="answer"):
        build_component(payload, "component")


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_open_work_without_a_rubric_is_rejected(component_type: str):
    spec = SPEC_BY_TYPE[component_type]
    if not spec.requires_rubric:
        pytest.skip("this type is graded against an answer key")

    payload = example(component_type)
    del payload["evaluation"]["rubric"]
    payload["evaluation"]["scoring"] = {"mode": "self_check"}

    with pytest.raises(ComponentError, match="rubric"):
        build_component(payload, "component")


@pytest.mark.parametrize(
    ("component_type", "answer", "message"),
    [
        ("multiple_choice", {"option_id": "nonexistent"}, "does not declare"),
        ("multi_select", {"option_ids": ["matrix"]}, "does not declare"),
        (
            "classification",
            {"assignments": [{"item_id": "third", "category_id": "consonant"}]},
            "exactly once",
        ),
        (
            "matching",
            {
                "pairs": [
                    {"left_id": "monet", "right_id": "nope"},
                    {"left_id": "pollock", "right_id": "impressionism"},
                ]
            },
            "does not declare",
        ),
        ("sentence_order", {"order": ["t1", "t2", "t3"]}, "exactly once"),
        ("timeline", {"order": ["perry", "perry", "alliance"]}, "exactly once"),
        ("labeling", {"labels": [{"marker_id": "m1", "label_id": "porto"}]}, "exactly once"),
        (
            "decision_path",
            {
                "decisions": [
                    {"step_id": "opening", "option_id": "dates"},
                    {"step_id": "close", "option_id": "dates"},
                ]
            },
            "not an option of step",
        ),
    ],
)
def test_an_answer_naming_something_undeclared_is_rejected(component_type, answer, message):
    with pytest.raises(ComponentError, match=message):
        build(component_type, answer=answer)


def test_a_duplicate_option_id_is_rejected():
    """Two options with one id makes every reference to it ambiguous."""
    payload = example("multiple_choice")
    payload["content"]["options"][1]["id"] = "matrix"

    with pytest.raises(ComponentError, match="duplicate"):
        build_component(payload, "component")


def test_a_hotspot_region_with_the_wrong_number_of_points_is_rejected():
    with pytest.raises(ComponentError, match="4 values for a rectangle"):
        build(
            "hotspot",
            answer={"regions": [{"id": "r", "shape": "rectangle", "points": [0.1, 0.2]}]},
        )


def test_a_table_answer_for_an_already_filled_cell_is_rejected():
    payload = example("table_grid")
    payload["content"]["prefilled_cells"] = [
        {"row_id": "mitosis", "column_id": "daughters", "text": "2"}
    ]

    with pytest.raises(ComponentError, match="already filled in"):
        build_component(payload, "component")


def test_a_confidence_scale_that_runs_backwards_is_rejected():
    with pytest.raises(ComponentError, match="below scale_max"):
        build("confidence_rating", content={"scale_min": 5, "scale_max": 2})


def test_a_word_count_minimum_above_the_maximum_is_rejected():
    payload = example("rubric_response")
    payload["content"]["min_words"] = 400
    payload["content"]["max_words"] = 100

    with pytest.raises(ComponentError, match="min_words"):
        build_component(payload, "component")


# ── Scoring definitions ───────────────────────────────────────────────────


def test_rubric_scoring_without_a_rubric_is_rejected():
    payload = example("multiple_choice")
    payload["evaluation"]["scoring"] = {"mode": "rubric"}

    with pytest.raises(ComponentError, match="no rubric is defined"):
        build_component(payload, "component")


def test_numeric_scoring_on_a_selection_component_is_rejected():
    payload = example("multiple_choice")
    payload["evaluation"]["scoring"] = {"mode": "numeric", "tolerance": 0.1}

    with pytest.raises(ComponentError, match="does not apply"):
        build_component(payload, "component")


def test_a_self_report_component_cannot_be_marked():
    """Nobody grades how confident someone says they feel."""
    payload = example("confidence_rating")
    payload["evaluation"]["scoring"] = {"mode": "exact"}

    with pytest.raises(ComponentError, match="self_check"):
        build_component(payload, "component")


def test_an_unknown_scoring_mode_is_rejected():
    payload = example("short_answer")
    payload["evaluation"]["scoring"] = {"mode": "vibes"}

    with pytest.raises(ComponentError, match="must be one of"):
        build_component(payload, "component")


def test_a_malformed_rubric_is_rejected():
    payload = example("free_response")
    payload["evaluation"]["rubric"] = [{"criterion": "Clarity"}]

    with pytest.raises(ComponentError, match="levels"):
        build_component(payload, "component")


def test_a_rubric_level_without_points_is_rejected():
    payload = example("free_response")
    payload["evaluation"]["rubric"][0]["levels"][0].pop("points")

    with pytest.raises(ComponentError, match="points"):
        build_component(payload, "component")


# ── Answers must not be visible in the question ───────────────────────────


def test_a_cloze_answer_inside_the_visible_passage_is_rejected():
    payload = example("fill_blank")
    payload["content"]["text"] = "A divergent boundary forms where plates move {{boundary}}."

    with pytest.raises(ComponentError, match="shows its own answer"):
        build_component(payload, "component")


def test_a_flashcard_whose_front_gives_away_the_back_is_rejected():
    payload = example("flashcard")
    payload["content"]["front"] = "The mountain: yama"
    payload["answer"]["back"] = "yama"

    with pytest.raises(ComponentError, match="shows its own answer"):
        build_component(payload, "component")


def test_a_recall_cue_that_contains_the_answer_is_rejected():
    payload = example("typed_recall")
    payload["content"]["cue"] = "The tibia is the weight-bearing bone"

    with pytest.raises(ComponentError, match="shows its own answer"):
        build_component(payload, "component")


def test_a_prompt_may_legitimately_contain_the_word_it_asks_about():
    """The leak rule must not reject ordinary, correct exercises.

    "Define photosynthesis" contains its own answer's subject, and a blanket
    substring rule would refuse it. The rule applies only where concealment is
    the point of the format.
    """
    component = build(
        "short_answer",
        prompt="Define photosynthesis in one sentence.",
        answer={"accepted": ["photosynthesis is how plants convert light into chemical energy"]},
    )

    assert component.type == "short_answer"


# ── Schema and runtime agree ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def prepare_parameters():
    from learning_studio.schemas import PREPARE_SCHEMA

    return PREPARE_SCHEMA["parameters"]


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_the_schema_accepts_every_valid_example(component_type: str, prepare_parameters):
    """What the runtime accepts, the advertised schema must accept too."""
    from tests.component_examples import manifest

    validate({"manifest": manifest([example(component_type)])}, prepare_parameters)


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_the_schema_rejects_an_unknown_nested_field(component_type: str, prepare_parameters):
    """And what the runtime refuses, the schema must refuse too."""
    from tests.component_examples import manifest

    payload = example(component_type)
    payload.setdefault("evaluation", {})["smuggled"] = "value"

    with pytest.raises(SchemaViolation, match="smuggled"):
        validate({"manifest": manifest([payload])}, prepare_parameters)


def test_the_schema_names_an_unknown_component_type_precisely(prepare_parameters):
    """A tagged union must not degrade to "does not match any accepted form"."""
    from tests.component_examples import manifest

    payload = example("multiple_choice")
    payload["type"] = "mind_reading"

    with pytest.raises(SchemaViolation, match="must be one of"):
        validate({"manifest": manifest([payload])}, prepare_parameters)


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_every_component_branch_is_closed(component_type: str):
    """``additionalProperties: false`` at every level of every branch."""
    lax: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            if (
                node.get("type") == "object"
                and "properties" in node
                and node.get("additionalProperties") is not False
            ):
                lax.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(component_schema(SPEC_BY_TYPE[component_type]), component_type)
    walk(shared_definitions(), "$defs")
    assert lax == []


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_every_branch_pins_its_discriminator(component_type: str):
    schema = component_schema(SPEC_BY_TYPE[component_type])

    assert schema["properties"]["type"]["const"] == component_type
    assert schema["properties"]["type"]["enum"] == [component_type]


def test_the_union_has_one_branch_per_type():
    assert len(components_schema()["oneOf"]) == len(COMPONENT_TYPES)


def test_every_reference_in_the_union_resolves():
    """A dangling ``$ref`` would be a schema the model cannot interpret."""
    definitions = shared_definitions()
    dangling: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            pointer = node.get("$ref")
            if isinstance(pointer, str):
                name = pointer.removeprefix("#/$defs/")
                if name not in definitions:
                    dangling.append(pointer)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(components_schema())
    walk(definitions)
    assert dangling == []


def test_the_shared_definitions_are_all_used():
    """An unused definition is schema the model reads and never needs.

    Searched across the union *and* the definitions themselves, because some
    are referenced only from another definition — the rubric is reached
    through ``evaluation``, never directly from a component branch.
    """
    definitions = shared_definitions()
    blob = json.dumps(components_schema()) + json.dumps(definitions)
    unused = [name for name in definitions if f'"#/$defs/{name}"' not in blob]

    assert unused == []


def test_deep_copying_an_example_does_not_share_state():
    """Guards the fixtures: a mutated example would corrupt later tests."""
    first = example("multiple_choice")
    first["content"]["options"][0]["text"] = "changed"

    assert EXAMPLES["multiple_choice"]["content"]["options"][0]["text"] != "changed"
    assert copy.deepcopy(EXAMPLES) == EXAMPLES
