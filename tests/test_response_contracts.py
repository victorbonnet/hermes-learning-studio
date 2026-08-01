"""Every component type's response contract, and both sides of it.

The defect this exists for: ``POST /api/session/answer`` checked only that a
response was JSON of a bounded size. ``{"component_id": "tf"}`` — no response at
all — advanced a ``true_false`` card, and half a ``matching`` answer advanced
that one.

Two directions are checked here, and both matter:

- **the contract refuses what it should** — missing, null, mistyped, incomplete,
  over-complete, extra-field, unknown-identifier, out-of-range;
- **the contract accepts what the frontend actually produces**, per type, built
  from the component *as served* rather than from the manifest.

A contract that only did the first would be a contract nothing could satisfy.
"""

from __future__ import annotations

import copy

import pytest

from learning_studio.components import COMPONENT_TYPES, build_component
from learning_studio.responses import (
    CONTRACTS,
    SELF_RATINGS,
    SKIPPED_RESPONSE,
    ResponseContractError,
    contract_covers_every_type,
    validate_component_response,
)
from tests.component_examples import example
from tests.served_responses import response_for


def projection(component_type: str):
    """A component as a learner receives it, plus the key to read it back."""
    return build_component(example(component_type), "component").project()


def served_content(component_type: str) -> dict:
    return projection(component_type).payload.get("content", {})


def check(component_type: str, response, *, content=None, resolve=None):
    return validate_component_response(
        component_type,
        content if content is not None else served_content(component_type),
        response,
        resolve=resolve,
    )


def refused(component_type: str, response, *, content=None) -> str:
    with pytest.raises(ResponseContractError) as raised:
        check(component_type, response, content=content)
    return raised.value.reason


# ── Coverage ──────────────────────────────────────────────────────────────


def test_every_component_type_has_a_contract():
    assert contract_covers_every_type()
    assert set(CONTRACTS) == set(COMPONENT_TYPES)


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_the_response_a_learner_would_give_is_accepted(component_type: str):
    """The positive case, for all thirty-one."""
    content = served_content(component_type)

    accepted = check(component_type, response_for(component_type, content), content=content)

    assert isinstance(accepted, dict) and accepted


# ── Nothing is not an answer ──────────────────────────────────────────────


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
@pytest.mark.parametrize("nothing", [None, "", "an answer", 0, [], 42, True])
def test_a_response_that_is_not_an_object_is_refused(component_type: str, nothing):
    """`{"component_id": "tf"}` used to advance the exercise."""
    assert refused(component_type, nothing) in {"response_missing", "type_unknown"}


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_an_empty_object_is_refused(component_type: str):
    assert refused(component_type, {}) == "field_missing"


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_an_unknown_field_is_refused(component_type: str):
    """Closed, like the component registry it mirrors."""
    content = served_content(component_type)
    response = {**response_for(component_type, content), "extra": "smuggled"}

    with pytest.raises(ResponseContractError) as raised:
        check(component_type, response, content=content)
    assert raised.value.reason == "field_unknown"


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_null_field_value_is_refused(component_type: str):
    content = served_content(component_type)
    response = response_for(component_type, content)
    for field in response:
        broken = {**response, field: None}
        with pytest.raises(ResponseContractError):
            check(component_type, broken, content=content)


# ── Identifiers must be ones this learner was served ──────────────────────

ID_TYPES = [
    ("multiple_choice", "option_id"),
    ("image_choice", "option_id"),
    ("scenario_choice", "option_id"),
]


@pytest.mark.parametrize(("component_type", "field"), ID_TYPES)
def test_an_option_that_was_never_served_is_refused(component_type: str, field: str):
    content = served_content(component_type)
    response = {**response_for(component_type, content), field: "not-an-option"}

    assert refused(component_type, response, content=content) == "identifier_unknown"


def test_a_canonical_identifier_is_not_accepted_from_a_client():
    """The aliasing would be decorative if the real ids still worked.

    A client that somehow learned the canonical name of an option must not be
    able to use it: the only identifiers this learner may name are the ones they
    were served.
    """
    component = build_component(example("multiple_choice"), "component")
    projected = component.project()
    canonical = component.content["options"][0]["id"]

    assert (
        refused(
            "multiple_choice",
            {"option_id": canonical},
            content=projected.payload["content"],
        )
        == "identifier_unknown"
    )


def test_a_served_alias_is_translated_back_to_the_canonical_identifier():
    component = build_component(example("multiple_choice"), "component")
    projected = component.project()
    alias = projected.payload["content"]["options"][0]["id"]

    accepted = validate_component_response(
        "multiple_choice",
        projected.payload["content"],
        {"option_id": alias},
        resolve=lambda value: projected.aliases.get(value, value),
    )

    assert accepted == {"option_id": projected.aliases[alias]}
    assert accepted["option_id"] in {entry["id"] for entry in component.content["options"]}


@pytest.mark.parametrize("component_type", ["sentence_order", "sequence_order", "timeline"])
def test_an_ordering_is_translated_entry_by_entry(component_type: str):
    component = build_component(example(component_type), "component")
    projected = component.project()
    field = {"sentence_order": "tokens", "sequence_order": "steps", "timeline": "events"}[
        component_type
    ]
    shown = [entry["id"] for entry in projected.payload["content"][field]]

    accepted = validate_component_response(
        component_type,
        projected.payload["content"],
        {"order": shown},
        resolve=lambda value: projected.aliases.get(value, value),
    )

    assert accepted["order"] == [projected.aliases[alias] for alias in shown]
    assert sorted(accepted["order"]) == sorted(component.answer["order"])


# ── Per-type refusals ─────────────────────────────────────────────────────


def test_true_false_requires_a_boolean():
    assert refused("true_false", {"value": "true"}) == "value_not_boolean"
    assert refused("true_false", {"value": 1}) == "value_not_boolean"
    assert check("true_false", {"value": False}) == {"value": False}


def test_multi_select_refuses_an_empty_or_repeated_selection():
    content = served_content("multi_select")
    options = [entry["id"] for entry in content["options"]]

    assert refused("multi_select", {"option_ids": []}, content=content) == "nothing_chosen"
    assert (
        refused("multi_select", {"option_ids": [options[0], options[0]]}, content=content)
        == "option_repeated"
    )


@pytest.mark.parametrize(
    ("component_type", "field"),
    [
        ("sentence_order", "tokens"),
        ("sequence_order", "steps"),
        ("timeline", "events"),
        ("process_flow", "stages"),
    ],
)
def test_an_ordering_must_be_a_complete_permutation(component_type: str, field: str):
    content = served_content(component_type)
    shown = [entry["id"] for entry in content[field]]

    assert refused(component_type, {"order": shown[:-1]}, content=content) == "order_incomplete"
    assert (
        refused(component_type, {"order": [*shown, shown[0]]}, content=content)
        == "order_incomplete"
    )
    assert refused(component_type, {"order": "abc"}, content=content) == "order_not_a_list"


def test_matching_must_answer_every_row():
    content = served_content("matching")
    complete = response_for("matching", content)

    assert (
        refused("matching", {"pairs": complete["pairs"][:-1]}, content=content)
        == "pairs_incomplete"
    )
    assert refused("matching", {"pairs": [{"left_id": "x"}]}, content=content) == "field_missing"


def test_labeling_must_label_every_marker():
    content = served_content("labeling")
    complete = response_for("labeling", content)

    assert (
        refused("labeling", {"labels": complete["labels"][:-1]}, content=content)
        == "labels_incomplete"
    )


def test_fill_blank_must_fill_every_gap():
    content = served_content("fill_blank")
    complete = response_for("fill_blank", content)

    assert refused("fill_blank", {"blanks": complete["blanks"][:-1]}, content=content) in {
        "blanks_incomplete",
        "field_missing",
    }
    empty = copy.deepcopy(complete)
    empty["blanks"][0]["text"] = "   "
    assert refused("fill_blank", empty, content=content) == "text_empty"


def test_table_grid_must_fill_every_empty_cell_and_no_prefilled_one():
    content = served_content("table_grid")
    complete = response_for("table_grid", content)

    assert (
        refused("table_grid", {"cells": complete["cells"][:-1]}, content=content)
        == "cells_incomplete"
    )
    prefilled = content.get("prefilled_cells")
    if prefilled:
        intruding = {
            "cells": [
                *complete["cells"],
                {
                    "row_id": prefilled[0]["row_id"],
                    "column_id": prefilled[0]["column_id"],
                    "text": "x",
                },
            ]
        }
        assert refused("table_grid", intruding, content=content) == "cell_already_filled"


def test_decision_path_options_must_belong_to_their_own_step():
    content = served_content("decision_path")
    steps = content["steps"]
    if len(steps) < 2:
        pytest.skip("the fixture has a single step, so there is no other step to borrow from")

    borrowed = {
        "decisions": [
            {"step_id": steps[0]["id"], "option_id": steps[1]["options"][0]["id"]},
            *[{"step_id": step["id"], "option_id": step["options"][0]["id"]} for step in steps[1:]],
        ]
    }

    assert refused("decision_path", borrowed, content=content) == "identifier_unknown"


def test_decision_path_must_answer_every_step():
    content = served_content("decision_path")
    complete = response_for("decision_path", content)
    if len(complete["decisions"]) < 2:
        pytest.skip("a single-step fixture cannot be partially answered")

    assert (
        refused("decision_path", {"decisions": complete["decisions"][:-1]}, content=content)
        == "decisions_incomplete"
    )


@pytest.mark.parametrize("rating", [-1, 0, 99, 1.5, "3", True, None])
def test_confidence_rating_refuses_anything_off_the_declared_scale(rating):
    content = served_content("confidence_rating")

    assert refused("confidence_rating", {"rating": rating}, content=content) in {
        "rating_not_an_integer",
        "rating_out_of_range",
    }


def test_confidence_rating_accepts_both_ends_of_the_scale():
    content = served_content("confidence_rating")

    for value in (content["scale_min"], content["scale_max"]):
        assert check("confidence_rating", {"rating": value}, content=content) == {"rating": value}


@pytest.mark.parametrize(
    "point",
    [
        {"x": -0.01, "y": 0.5},
        {"x": 1.01, "y": 0.5},
        {"x": 0.5, "y": -0.0001},
        {"x": 0.5, "y": 1.5},
        {"x": "0.5", "y": 0.5},
        {"x": True, "y": 0.5},
    ],
)
def test_hotspot_coordinates_must_be_numbers_inside_the_unit_square(point):
    assert refused("hotspot", {"points": [point]}) in {
        "coordinate_out_of_range",
        "coordinate_not_a_number",
    }


@pytest.mark.parametrize("point", [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 0, "y": 1}])
def test_hotspot_accepts_the_boundaries(point):
    assert check("hotspot", {"points": [point]}) == {
        "points": [{"x": float(point["x"]), "y": float(point["y"])}]
    }


def test_hotspot_takes_exactly_one_point():
    assert refused("hotspot", {"points": []}) == "points_not_one"
    assert (
        refused("hotspot", {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}]})
        == "points_not_one"
    )


@pytest.mark.parametrize("rating", ["", "brilliant", None, 3, "GOOD"])
def test_a_flashcard_needs_a_known_self_rating(rating):
    assert (
        refused("flashcard", {"text": "my recall", "self_rating": rating}) == "self_rating_unknown"
    )


@pytest.mark.parametrize("rating", SELF_RATINGS)
def test_every_declared_self_rating_is_accepted(rating: str):
    assert check("flashcard", {"text": "recall", "self_rating": rating})["self_rating"] == rating


def test_a_flashcard_needs_a_recall_as_well_as_a_rating():
    assert refused("flashcard", {"self_rating": "good"}) == "field_missing"
    assert refused("flashcard", {"text": "  ", "self_rating": "good"}) == "text_empty"


def test_an_open_response_must_actually_say_something():
    assert refused("short_answer", {"text": ""}) == "text_empty"
    assert refused("short_answer", {"text": "   \n "}) == "text_empty"
    assert refused("short_answer", {"text": 12}) == "not_text"


def test_word_bounds_are_enforced_on_the_server_too():
    content = {"min_words": 5, "max_words": 8}

    assert refused("free_response", {"text": "too short"}, content=content) == "too_few_words"
    assert (
        refused("free_response", {"text": " ".join(["w"] * 9)}, content=content) == "too_many_words"
    )
    assert check("free_response", {"text": " ".join(["w"] * 6)}, content=content)


def test_a_multi_prompt_minimum_counts_the_whole_response():
    content = {"prompts": ["One?", "Two?"], "min_words": 6}

    assert (
        refused("reflection", {"responses": ["one two", "three"]}, content=content)
        == "too_few_words"
    )
    assert check("reflection", {"responses": ["one two three", "four five six"]}, content=content)


def test_a_prompt_list_needs_one_response_per_prompt():
    content = {"prompts": ["One?", "Two?"]}

    assert (
        refused("reflection", {"responses": ["only one"]}, content=content)
        == "responses_incomplete"
    )
    assert (
        refused("reflection", {"responses": ["a", "b", "c"]}, content=content)
        == "responses_incomplete"
    )


def test_classification_must_place_every_item_exactly_once():
    content = served_content("classification")
    complete = response_for("classification", content)

    assert (
        refused("classification", {"assignments": complete["assignments"][:-1]}, content=content)
        == "items_incomplete"
    )
    doubled = {"assignments": [*complete["assignments"], complete["assignments"][0]]}
    assert refused("classification", doubled, content=content) == "items_incomplete"


def test_categorization_refuses_several_groups_unless_the_card_allows_it():
    content = dict(served_content("categorization"))
    categories = [entry["id"] for entry in content["categories"]]
    if len(categories) < 2:
        pytest.skip("the fixture declares a single category")

    content["allow_multiple"] = False
    both = {
        "assignments": [
            {"item_id": item["id"], "category_ids": categories[:2]} for item in content["items"]
        ]
    }
    assert refused("categorization", both, content=content) == "multiple_not_allowed"

    content["allow_multiple"] = True
    assert check("categorization", both, content=content)


# ── The universal skip ────────────────────────────────────────────────────


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_client_that_cannot_draw_a_card_may_skip_it(component_type: str):
    """The one response any type accepts, spelled exactly."""
    assert check(component_type, dict(SKIPPED_RESPONSE)) == SKIPPED_RESPONSE


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_near_miss_of_the_skip_is_not_a_skip(component_type: str):
    for impostor in ({"skipped": False}, {"skipped": "true"}, {"skipped": True, "x": 1}):
        with pytest.raises(ResponseContractError):
            check(component_type, impostor)


def test_an_unknown_component_type_has_no_contract_to_satisfy():
    assert (
        refused("holographic_interpretive_dance", {"anything": True}, content={}) == "type_unknown"
    )


# ── The error says nothing about the submission ───────────────────────────


def test_the_client_message_is_fixed_and_carries_no_submitted_value():
    from learning_studio.responses import INVALID_RESPONSE_MESSAGE

    with pytest.raises(ResponseContractError) as raised:
        check("short_answer", {"text": "ok", "smuggled": "a very distinctive secret string"})

    assert str(raised.value) == INVALID_RESPONSE_MESSAGE
    assert "distinctive" not in str(raised.value)
    assert "distinctive" not in raised.value.reason
    assert "smuggled" not in raised.value.reason


# ── Translation fails closed ──────────────────────────────────────────────
#
# `aliases.get(alias, alias)` reads as a harmless default and is not one. An
# absent or incomplete mapping made a learner-facing alias look like a resolved
# evaluator identifier, and it was stored as the learner's answer — a well-formed
# identifier that names nothing in the answer key, which nothing downstream could
# have noticed.


def resolver(aliases):
    """The API's own resolver, so this tests the shipped behaviour."""
    from learning_studio.web.app import _identifier_resolver

    return _identifier_resolver(aliases)


def test_an_identifier_with_no_mapping_is_refused_rather_than_passed_through():
    component = build_component(example("multiple_choice"), "component")
    projected = component.project()
    alias = projected.payload["content"]["options"][0]["id"]

    with pytest.raises(ResponseContractError) as raised:
        validate_component_response(
            "multiple_choice",
            projected.payload["content"],
            {"option_id": alias},
            resolve=resolver({}),
        )

    assert raised.value.reason == "identifier_unresolvable"


def test_an_incomplete_mapping_is_refused_for_the_identifier_it_omits():
    """One missing entry is enough; the rest resolving is not a defence."""
    component = build_component(example("sequence_order"), "component")
    projected = component.project()
    shown = [entry["id"] for entry in projected.payload["content"]["steps"]]
    partial = {
        alias: canonical for alias, canonical in projected.aliases.items() if alias != shown[0]
    }

    with pytest.raises(ResponseContractError) as raised:
        validate_component_response(
            "sequence_order",
            projected.payload["content"],
            {"order": shown},
            resolve=resolver(partial),
        )

    assert raised.value.reason == "identifier_unresolvable"


def test_a_complete_mapping_still_resolves_every_identifier():
    """The mirror: failing closed must not fail on the ordinary case."""
    component = build_component(example("matching"), "component")
    projected = component.project()
    left = [entry["id"] for entry in projected.payload["content"]["left"]]
    right = [entry["id"] for entry in projected.payload["content"]["right"]]

    accepted = validate_component_response(
        "matching",
        projected.payload["content"],
        {"pairs": [{"left_id": one, "right_id": right[0]} for one in left]},
        resolve=resolver(projected.aliases),
    )

    canonical_left = {entry["id"] for entry in component.content["left"]}
    canonical_right = {entry["id"] for entry in component.content["right"]}
    for pair in accepted["pairs"]:
        assert pair["left_id"] in canonical_left
        assert pair["right_id"] in canonical_right


def test_a_component_prepared_before_aliasing_passes_identifiers_through():
    """`None` means "no alias record", which is a different claim from "empty".

    Collapsing the two is what let an untranslatable identifier through. A
    component that predates aliasing served canonical identifiers, so there is
    genuinely nothing to translate.
    """
    component = build_component(example("multiple_choice"), "component")
    canonical = component.content["options"][0]["id"]

    accepted = validate_component_response(
        "multiple_choice",
        component.content,
        {"option_id": canonical},
        resolve=resolver(None),
    )

    assert accepted == {"option_id": canonical}


def test_the_legacy_path_still_refuses_an_identifier_that_was_never_served():
    """Passing identifiers through is not the same as accepting anything."""
    component = build_component(example("multiple_choice"), "component")

    with pytest.raises(ResponseContractError) as raised:
        validate_component_response(
            "multiple_choice",
            component.content,
            {"option_id": "invented"},
            resolve=resolver(None),
        )

    assert raised.value.reason == "identifier_unknown"


def test_the_refusal_names_no_identifier():
    component = build_component(example("multiple_choice"), "component")
    projected = component.project()
    alias = projected.payload["content"]["options"][0]["id"]

    with pytest.raises(ResponseContractError) as raised:
        validate_component_response(
            "multiple_choice",
            projected.payload["content"],
            {"option_id": alias},
            resolve=resolver({}),
        )

    assert alias not in str(raised.value)
    assert alias not in raised.value.reason
