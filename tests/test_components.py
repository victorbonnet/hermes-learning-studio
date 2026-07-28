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
            "not an option of that step",
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
    """``code_response`` may be marked against a rubric — but only a real one."""
    payload = example("code_response")
    payload["evaluation"]["scoring"] = {"mode": "rubric"}
    del payload["evaluation"]["rubric"]

    with pytest.raises(ComponentError, match="no rubric is defined"):
        build_component(payload, "component")


@pytest.mark.parametrize(
    ("component_type", "mode"),
    [
        ("multiple_choice", "ordered"),
        ("multiple_choice", "self_check"),
        ("multiple_choice", "numeric"),
        ("short_answer", "ordered"),
        ("short_answer", "set"),
        ("flashcard", "exact"),
        ("timeline", "exact"),
        ("matching", "ordered"),
        ("free_response", "normalised"),
        ("hotspot", "numeric"),
    ],
)
def test_a_scoring_mode_that_cannot_mark_this_component_is_rejected(component_type, mode):
    """An ordered score over a single-answer question grades nothing."""
    payload = example(component_type)
    payload.setdefault("evaluation", {})["scoring"] = {"mode": mode}

    with pytest.raises(ComponentError, match="scoring.mode"):
        build_component(payload, "component")


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_every_declared_scoring_mode_is_accepted(component_type: str):
    """The positive half: each type accepts every mode its spec declares."""
    spec = SPEC_BY_TYPE[component_type]
    for mode in spec.scoring_modes:
        payload = example(component_type)
        payload.setdefault("evaluation", {})["scoring"] = {"mode": mode}
        if mode == "rubric":
            payload["evaluation"].setdefault(
                "rubric",
                [{"criterion": "c", "levels": [{"label": "l", "descriptor": "d", "points": 1}]}],
            )
        assert build_component(payload, "component").type == component_type


def test_a_self_report_component_refuses_a_rubric_outright():
    """Not merely discouraged: the field does not exist for these types."""
    payload = example("confidence_rating")
    payload["evaluation"]["rubric"] = [
        {"criterion": "c", "levels": [{"label": "l", "descriptor": "d", "points": 1}]}
    ]

    with pytest.raises(ComponentError, match="rubric"):
        build_component(payload, "component")


def test_a_self_report_component_cannot_be_marked():
    """Nobody grades how confident someone says they feel."""
    payload = example("confidence_rating")
    payload["evaluation"]["scoring"] = {"mode": "exact"}

    with pytest.raises(ComponentError, match="scoring.mode"):
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

    with pytest.raises(ComponentError, match="already readable"):
        build_component(payload, "component")


def test_a_flashcard_whose_front_gives_away_the_back_is_rejected():
    payload = example("flashcard")
    payload["content"]["front"] = "The mountain: yama"
    payload["answer"]["back"] = "yama"

    with pytest.raises(ComponentError, match="already readable"):
        build_component(payload, "component")


def test_a_recall_cue_that_contains_the_answer_is_rejected():
    payload = example("typed_recall")
    payload["content"]["cue"] = "The tibia is the weight-bearing bone"

    with pytest.raises(ComponentError, match="already readable"):
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


# ── Answer duplication, for every keyed text component ────────────────────


@pytest.mark.parametrize(
    ("component_type", "overrides"),
    [
        (
            "short_answer",
            {"prompt": "Type Paris. The answer is Paris.", "answer": {"accepted": ["Paris"]}},
        ),
        (
            "typed_recall",
            {"content": {"cue": "Type H2O"}, "answer": {"accepted": ["H2O"]}},
        ),
        (
            "typed_recall",
            {"content": {"cue": "The symbol is Na"}, "answer": {"accepted": ["Na"]}},
        ),
        (
            "short_answer",
            {"prompt": "The value is 42. What is it?", "answer": {"accepted": ["42"]}},
        ),
        (
            "translation",
            {
                "content": {
                    "source_text": "Ainda nao sei. It means I still do not know.",
                    "source_locale": "pt-BR",
                    "target_locale": "en",
                },
                "answer": {"accepted": ["I still do not know"]},
            },
        ),
        (
            "diagram",
            {
                "prompt": "Which component is a rheostat in this circuit?",
                "answer": {"accepted": ["a rheostat"]},
            },
        ),
    ],
)
def test_an_answer_repeated_in_the_visible_half_is_rejected(component_type, overrides):
    """Short answers included: ``H2O`` and ``Na`` are exactly what gets leaked."""
    with pytest.raises(ComponentError, match="already readable"):
        build(component_type, **overrides)


def test_an_answer_leaked_through_component_accessibility_text_is_rejected():
    """The whole recursive visible half is compared, not only the prompt."""
    with pytest.raises(ComponentError, match="already readable"):
        build(
            "short_answer",
            accessibility={"caption": "Remember: the answer is Paris"},
            answer={"accepted": ["Paris"]},
        )


def test_an_answer_leaked_through_a_content_label_is_rejected():
    payload = example("table_grid")
    payload["content"]["rows"][0]["header"] = "Mitosis produces two daughter cells"
    payload["answer"]["cells"][0]["accepted"] = ["two daughter cells"]

    with pytest.raises(ComponentError, match="already readable"):
        build_component(payload, "component")


def test_a_short_answer_is_not_matched_inside_a_longer_word():
    """Token boundaries, not substrings: ``Na`` is not hiding in "national"."""
    component = build(
        "typed_recall",
        content={"cue": "The national symbol for this element"},
        answer={"accepted": ["Na"]},
    )

    assert component.type == "typed_recall"


def test_a_multi_word_answer_scattered_across_the_prompt_is_allowed():
    """The tokens must appear together and in order to count as a leak."""
    component = build(
        "short_answer",
        prompt="Which cycle follows glycolysis, and where does it occur?",
        answer={"accepted": ["the citric acid cycle, in the mitochondrial matrix"]},
    )

    assert component.type == "short_answer"


def test_a_selection_component_may_show_its_option_text():
    """Its key is an opaque option id; showing the options is the format."""
    component = build("multiple_choice")

    assert any("matrix" in option["text"].lower() for option in component.content["options"])


def test_a_grid_may_prefill_a_value_another_cell_expects():
    """Two cells of a comparison can honestly hold the same value."""
    payload = example("table_grid")
    payload["content"]["rows"].append({"id": "binary", "header": "Binary fission"})
    payload["content"]["prefilled_cells"] = [
        {"row_id": "binary", "column_id": "daughters", "text": "2"}
    ]

    component = build_component(payload, "component")

    assert component.type == "table_grid"


# ── Feedback, sets, blanks and grids ──────────────────────────────────────


def test_feedback_for_an_option_that_does_not_exist_is_rejected():
    payload = example("multiple_choice")
    payload["evaluation"]["feedback"]["per_option"] = [{"option_id": "ghost", "text": "no"}]

    with pytest.raises(ComponentError, match="does not declare"):
        build_component(payload, "component")


def test_two_feedback_entries_for_the_same_option_are_rejected():
    payload = example("multiple_choice")
    payload["evaluation"]["feedback"]["per_option"] = [
        {"option_id": "cytosol", "text": "one"},
        {"option_id": "cytosol", "text": "two"},
    ]

    with pytest.raises(ComponentError, match="feedback twice"):
        build_component(payload, "component")


def test_per_option_feedback_on_a_component_without_options_is_rejected():
    payload = example("short_answer")
    payload["evaluation"].setdefault("feedback", {})["per_option"] = [
        {"option_id": "anything", "text": "no"}
    ]

    with pytest.raises(ComponentError, match="has no options"):
        build_component(payload, "component")


def test_a_repeated_id_in_a_set_answer_is_rejected():
    with pytest.raises(ComponentError, match="more than once"):
        build("multi_select", answer={"option_ids": ["lifejackets", "lifejackets"]})


def test_a_placeholder_naming_an_undeclared_blank_is_rejected():
    payload = example("fill_blank")
    payload["content"]["text"] = "Complete {{ghost}}."

    with pytest.raises(ComponentError, match="which blanks does not declare"):
        build_component(payload, "component")


def test_a_declared_blank_with_no_gap_in_the_passage_is_rejected():
    payload = example("fill_blank")
    payload["content"]["text"] = "A passage with no gap in it at all."

    with pytest.raises(ComponentError, match="no gap for blank"):
        build_component(payload, "component")


def test_a_repeated_placeholder_is_rejected():
    payload = example("fill_blank")
    payload["content"]["text"] = "A {{boundary}} boundary is a {{boundary}} boundary."

    with pytest.raises(ComponentError, match="repeats placeholder"):
        build_component(payload, "component")


def test_a_prefilled_cell_naming_an_unknown_row_is_rejected():
    payload = example("table_grid")
    payload["content"]["prefilled_cells"] = [
        {"row_id": "ghost", "column_id": "daughters", "text": "2"}
    ]

    with pytest.raises(ComponentError, match="which rows does not declare"):
        build_component(payload, "component")


def test_a_prefilled_cell_naming_an_unknown_column_is_rejected():
    payload = example("table_grid")
    payload["content"]["prefilled_cells"] = [
        {"row_id": "mitosis", "column_id": "ghost", "text": "2"}
    ]

    with pytest.raises(ComponentError, match="which columns does not declare"):
        build_component(payload, "component")


def test_a_grid_cell_left_with_neither_a_value_nor_an_answer_is_rejected():
    """An empty box nothing will ever mark is not a question."""
    payload = example("table_grid")
    payload["content"]["columns"].append({"id": "chromosomes", "header": "Chromosome number"})

    with pytest.raises(ComponentError, match="unaccounted for"):
        build_component(payload, "component")


def test_the_same_cell_prefilled_twice_is_rejected():
    payload = example("table_grid")
    payload["content"]["prefilled_cells"] = [
        {"row_id": "mitosis", "column_id": "daughters", "text": "2"},
        {"row_id": "mitosis", "column_id": "daughters", "text": "two"},
    ]

    with pytest.raises(ComponentError, match="fills the same cell twice"):
        build_component(payload, "component")


# ── Normalisation bypasses in the answer-leak check ───────────────────────


@pytest.mark.parametrize(
    ("component_type", "overrides"),
    [
        (
            "short_answer",
            {"prompt": "Type P.a.r.i.s as your answer.", "answer": {"accepted": ["Paris"]}},
        ),
        (
            "short_answer",
            {"prompt": "Spell it P-a-r-i-s exactly.", "answer": {"accepted": ["Paris"]}},
        ),
        (
            "typed_recall",
            {"content": {"cue": "It is H.2.O"}, "answer": {"accepted": ["H2O"]}},
        ),
        (
            "short_answer",
            {"prompt": "Type + exactly. The answer is +.", "answer": {"accepted": ["+"]}},
        ),
        ("short_answer", {"prompt": "Type === here.", "answer": {"accepted": ["==="]}}),
        ("typed_recall", {"content": {"cue": "Enter ✓"}, "answer": {"accepted": ["✓"]}}),
    ],
)
def test_an_obfuscated_or_symbolic_answer_is_still_caught(component_type, overrides):
    """Separators and symbols were both ways past the tokeniser."""
    with pytest.raises(ComponentError, match="already readable"):
        build(component_type, **overrides)


@pytest.mark.parametrize(
    ("component_type", "overrides"),
    [
        # Word boundaries hold: the separated rule needs the whole answer.
        (
            "typed_recall",
            {"content": {"cue": "The national anthem"}, "answer": {"accepted": ["Na"]}},
        ),
        (
            "short_answer",
            {"prompt": "Explain what is different about it.", "answer": {"accepted": ["if"]}},
        ),
        # A topic mentioned without giving the answer away.
        (
            "short_answer",
            {
                "prompt": "Which city is the capital of France?",
                "answer": {"accepted": ["Paris"]},
            },
        ),
        # Purely numeric answers are not treated as separated spellings, so a
        # decimal is not read as an obfuscated integer.
        ("short_answer", {"prompt": "The mean was 4.2 overall.", "answer": {"accepted": ["42"]}}),
        # A symbol the answer does not use.
        ("short_answer", {"prompt": "Simplify 3 - 1 fully.", "answer": {"accepted": ["+"]}}),
    ],
)
def test_a_near_miss_is_not_mistaken_for_a_leak(component_type, overrides):
    assert build(component_type, **overrides).type == component_type


@pytest.mark.parametrize(
    "component_type",
    [
        "fill_blank",
        "short_answer",
        "translation",
        "error_correction",
        "code_response",
        "flashcard",
        "typed_recall",
        "diagram",
        "table_grid",
    ],
)
def test_every_keyed_text_component_is_leak_checked(component_type: str):
    """The registry declares a leak path for each of them."""
    assert SPEC_BY_TYPE[component_type].leak_paths


# ── Categorization groups items; it does not forbid shared categories ─────


def test_two_items_may_share_a_category():
    """The commonest correct grouping answer there is."""
    payload = example("categorization")
    payload["answer"]["assignments"] = [
        {"item_id": "glass", "category_ids": ["recycling"]},
        {"item_id": "peel", "category_ids": ["recycling"]},
    ]

    assert build_component(payload, "component").type == "categorization"


def test_multiple_categories_are_allowed_when_the_component_says_so():
    payload = example("categorization")
    payload["content"]["allow_multiple"] = True
    payload["answer"]["assignments"] = [
        {"item_id": "glass", "category_ids": ["recycling", "compost"]},
        {"item_id": "peel", "category_ids": ["compost"]},
    ]

    assert build_component(payload, "component").type == "categorization"


def test_multiple_categories_are_refused_when_the_component_does_not():
    payload = example("categorization")
    payload["content"]["allow_multiple"] = False
    payload["answer"]["assignments"] = [
        {"item_id": "glass", "category_ids": ["recycling", "compost"]},
        {"item_id": "peel", "category_ids": ["compost"]},
    ]

    with pytest.raises(ComponentError, match="allow_multiple"):
        build_component(payload, "component")


def test_multiple_categories_are_refused_when_allow_multiple_is_omitted():
    payload = example("categorization")
    del payload["content"]["allow_multiple"]
    payload["answer"]["assignments"] = [
        {"item_id": "glass", "category_ids": ["recycling", "compost"]},
        {"item_id": "peel", "category_ids": ["compost"]},
    ]

    with pytest.raises(ComponentError, match="allow_multiple"):
        build_component(payload, "component")


def test_the_same_category_twice_for_one_item_is_refused():
    payload = example("categorization")
    payload["content"]["allow_multiple"] = True
    payload["answer"]["assignments"] = [
        {"item_id": "glass", "category_ids": ["recycling", "recycling"]},
        {"item_id": "peel", "category_ids": ["compost"]},
    ]

    with pytest.raises(ComponentError, match="same category more than once"):
        build_component(payload, "component")


def test_an_undeclared_category_is_still_refused():
    payload = example("categorization")
    payload["answer"]["assignments"] = [
        {"item_id": "glass", "category_ids": ["landfill"]},
        {"item_id": "peel", "category_ids": ["compost"]},
    ]

    with pytest.raises(ComponentError, match="does not declare"):
        build_component(payload, "component")


def test_an_item_without_an_assignment_is_still_refused():
    payload = example("categorization")
    payload["answer"]["assignments"] = [{"item_id": "glass", "category_ids": ["recycling"]}]

    with pytest.raises(ComponentError, match="exactly once"):
        build_component(payload, "component")


# ── Error correction: the count must be the count ─────────────────────────


def test_the_stated_error_count_must_match_the_key():
    payload = example("error_correction")
    payload["content"]["error_count"] = 2

    with pytest.raises(ComponentError, match="error_count says 2"):
        build_component(payload, "component")


def test_an_error_count_below_the_key_is_refused():
    payload = example("error_correction")
    payload["answer"]["corrections"].append(
        {"incorrect": "The committee have", "correct": "The committee has"}
    )

    with pytest.raises(ComponentError, match="error_count says 1"):
        build_component(payload, "component")


def test_a_matching_error_count_is_accepted():
    payload = example("error_correction")
    payload["content"]["error_count"] = 1

    assert build_component(payload, "component").type == "error_correction"


def test_an_error_count_mismatch_does_not_quote_the_correction():
    payload = example("error_correction")
    payload["content"]["error_count"] = 2

    with pytest.raises(ComponentError) as refusal:
        build_component(payload, "component")

    assert "which were widely reported" not in str(refusal.value)


def test_a_correction_that_changes_nothing_is_refused():
    payload = example("error_correction")
    payload["answer"]["corrections"][0]["correct"] = payload["answer"]["corrections"][0][
        "incorrect"
    ]

    with pytest.raises(ComponentError, match="nothing to fix"):
        build_component(payload, "component")


def test_a_correction_of_text_that_is_not_in_the_passage_is_refused():
    payload = example("error_correction")
    payload["answer"]["corrections"][0]["incorrect"] = "a phrase that never appears"

    with pytest.raises(ComponentError, match="nothing to correct"):
        build_component(payload, "component")


# ── Obfuscation with unbounded separator runs ─────────────────────────────


@pytest.mark.parametrize(
    ("component_type", "overrides"),
    [
        (
            "short_answer",
            {"prompt": "Type P...a...r...i...s with no dots.", "answer": {"accepted": ["Paris"]}},
        ),
        (
            "short_answer",
            {
                "prompt": "Spell it P-----a-----r-----i-----s here.",
                "answer": {"accepted": ["Paris"]},
            },
        ),
        (
            "short_answer",
            {"prompt": "Write P _ a _ r _ i _ s out.", "answer": {"accepted": ["Paris"]}},
        ),
        (
            "short_answer",
            {"prompt": "Type P·a·r·i·s exactly.", "answer": {"accepted": ["Paris"]}},
        ),
        (
            "short_answer",
            {"prompt": "Enter =.=.= but omit the dots.", "answer": {"accepted": ["==="]}},
        ),
        (
            "typed_recall",
            {"content": {"cue": "It is H..2..O"}, "answer": {"accepted": ["H2O"]}},
        ),
        (
            "flashcard",
            {
                "content": {"front": "The reading is y-a-m-a"},
                "answer": {"back": "yama"},
            },
        ),
    ],
)
def test_a_separator_run_of_any_length_is_still_a_leak(component_type, overrides):
    """The old rule capped the run at two, which three dots defeated."""
    with pytest.raises(ComponentError, match="already readable"):
        build(component_type, **overrides)


def test_the_obfuscation_refusal_does_not_quote_the_answer():
    with pytest.raises(ComponentError) as refusal:
        build(
            "short_answer",
            prompt="Type P...a...r...i...s with no dots.",
            answer={"accepted": ["Paris"]},
        )

    assert "Paris" not in str(refusal.value)


@pytest.mark.parametrize(
    ("component_type", "overrides"),
    [
        # The separator run may not cross a word character, so an answer
        # cannot be assembled out of unrelated words.
        (
            "typed_recall",
            {"content": {"cue": "Pack a rack in Silesia"}, "answer": {"accepted": ["Paris"]}},
        ),
        (
            "typed_recall",
            {"content": {"cue": "The national anthem"}, "answer": {"accepted": ["Na"]}},
        ),
        (
            "short_answer",
            {"prompt": "Explain what is different here.", "answer": {"accepted": ["if"]}},
        ),
        (
            "short_answer",
            {"prompt": "The mean was 4.2 overall.", "answer": {"accepted": ["42"]}},
        ),
        (
            "short_answer",
            {"prompt": "Which city is the capital of France?", "answer": {"accepted": ["Paris"]}},
        ),
    ],
)
def test_separator_matching_does_not_cross_word_characters(component_type, overrides):
    assert build(component_type, **overrides).type == component_type


# ── Corrections must claim distinct source spans ──────────────────────────


def correcting(text: str, count, corrections):
    payload = example("error_correction", prompt="Correct the passage.")
    payload["content"] = {"text": text}
    if count is not None:
        payload["content"]["error_count"] = count
    payload["answer"] = {"corrections": corrections}
    return payload


def test_two_corrections_of_the_same_occurrence_are_rejected():
    """``are`` and ``are.`` are one word in one place once punctuation goes."""
    payload = correcting(
        "They are ready.",
        2,
        [
            {"incorrect": "are", "correct": "was-ready"},
            {"incorrect": "are.", "correct": "became-ready"},
        ],
    )

    with pytest.raises(ComponentError, match="no other correction has already claimed"):
        build_component(payload, "component")


@pytest.mark.parametrize(
    "second",
    ["are.", "ARE", "“are”", "are,", "  are  "],
)
def test_punctuation_case_and_spacing_do_not_create_a_second_span(second: str):
    payload = correcting(
        "They are ready.",
        2,
        [
            {"incorrect": "are", "correct": "was-ready"},
            {"incorrect": second, "correct": "became-ready"},
        ],
    )

    with pytest.raises(ComponentError):
        build_component(payload, "component")


def test_a_word_that_genuinely_appears_twice_may_be_corrected_twice():
    payload = correcting(
        "The the cat sat on the mat.",
        2,
        [
            {"incorrect": "the", "correct": "a-first"},
            {"incorrect": "the", "correct": "a-second"},
        ],
    )

    assert build_component(payload, "component").type == "error_correction"


def test_two_genuinely_different_spans_are_accepted():
    payload = correcting(
        "They are ready and they are set.",
        2,
        [
            {"incorrect": "They are ready", "correct": "They were ready"},
            {"incorrect": "they are set", "correct": "they were set"},
        ],
    )

    assert build_component(payload, "component").type == "error_correction"


def test_the_error_count_is_the_number_of_distinct_spans():
    payload = correcting(
        "They are ready.",
        2,
        [{"incorrect": "are", "correct": "were"}],
    )

    with pytest.raises(ComponentError, match="distinct place"):
        build_component(payload, "component")


def test_a_span_mismatch_does_not_quote_the_correction():
    payload = correcting(
        "They are ready.",
        2,
        [
            {"incorrect": "are", "correct": "was-ready"},
            {"incorrect": "are.", "correct": "became-ready"},
        ],
    )

    with pytest.raises(ComponentError) as refusal:
        build_component(payload, "component")

    assert "became-ready" not in str(refusal.value)
