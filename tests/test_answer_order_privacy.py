"""The order of a visible list must not be the answer.

For most component types the answer is an opaque id, and the order entries happen
to appear in discloses nothing. For two groups it discloses everything:

- the ordering families are graded on ``answer.order``, and an author writes the
  steps of a titration in the order they happen — so the list *is* the key;
- ``matching.right`` and ``labeling.label_bank`` are the option lists behind each
  row's ``<select>``, and an author writes them parallel to the rows — so "the
  first option of the first dropdown" is the key.

Every canonical fixture in this repository had both properties, which means the
Mini App displayed the correct answer to a learner who had only to press Submit.
The projection now rearranges those lists, and these tests are the ones that
would have caught it.
"""

from __future__ import annotations

import json

import pytest

from learning_studio.components import (
    ANSWER_BEARING_ORDER,
    COMPONENT_TYPES,
    GATED_CONTENT,
    build_component,
    shuffled_content,
)
from tests.component_examples import CANARY, example

#: How the answer of each affected type is spelled, and which visible list it
#: corresponds to positionally.
LEAK_SHAPES = {
    "sentence_order": ("tokens", "order"),
    "sequence_order": ("steps", "order"),
    "timeline": ("events", "order"),
    "process_flow": ("stages", "order"),
}

CHOICE_LIST_SHAPES = {
    "matching": ("right", lambda answer: [pair["right_id"] for pair in answer["pairs"]]),
    "labeling": ("label_bank", lambda answer: [entry["label_id"] for entry in answer["labels"]]),
}

ORDERING_TYPES = tuple(LEAK_SHAPES)


def build(component_type: str):
    return build_component(example(component_type), "component")


def ids(entries: list[dict]) -> list[str]:
    return [entry["id"] for entry in entries]


def served(component, **kwargs):
    """The projection a learner receives, and the key to reading it back."""
    return component.project(**kwargs)


def canonical_order(projection, field: str) -> list[str]:
    """The visible arrangement, translated back into canonical identifiers.

    Every assertion about *order* has to go through this now: the ids on the card
    are aliases, so comparing them to an answer key directly would compare two
    different vocabularies and pass for the wrong reason.
    """
    return [projection.aliases[entry["id"]] for entry in projection.payload["content"][field]]


# ── The leak itself ───────────────────────────────────────────────────────


@pytest.mark.parametrize("component_type", ORDERING_TYPES)
def test_the_fixture_would_leak_if_the_projection_copied_it(component_type: str):
    """Proves these tests are not vacuous.

    Every canonical example really is authored in its correct order, so a
    projection that passed the list through would show the answer. If an example
    is ever rewritten to be pre-scrambled, this fails and says so — the leak
    would then be untested rather than absent.
    """
    component = build(component_type)
    visible_field, answer_field = LEAK_SHAPES[component_type]

    assert ids(component.content[visible_field]) == component.answer[answer_field]


@pytest.mark.parametrize("component_type", ORDERING_TYPES)
def test_the_projected_order_is_not_the_answer(component_type: str):
    """Repeated, because a shuffle that is right on average is not right."""
    component = build(component_type)
    visible_field, answer_field = LEAK_SHAPES[component_type]
    correct = component.answer[answer_field]

    for _attempt in range(40):
        shown = canonical_order(served(component), visible_field)
        assert shown != correct, f"{component_type} was served in its correct order"


@pytest.mark.parametrize("component_type", tuple(CHOICE_LIST_SHAPES))
def test_a_choice_list_is_not_served_parallel_to_its_rows(component_type: str):
    component = build(component_type)
    visible_field, answer_order = CHOICE_LIST_SHAPES[component_type]

    assert ids(component.content[visible_field]) == answer_order(component.answer), (
        "the fixture is no longer authored in answer order, so this test proves nothing"
    )
    for _attempt in range(40):
        shown = canonical_order(served(component), visible_field)
        assert shown != answer_order(component.answer)


@pytest.mark.parametrize("component_type", ORDERING_TYPES + tuple(CHOICE_LIST_SHAPES))
def test_submitting_the_order_as_shown_is_not_correct_by_construction(component_type: str):
    """The learner-visible arrangement must not double as a correct submission."""
    component = build(component_type)
    visible_field = ANSWER_BEARING_ORDER[component_type][0].field
    shown = canonical_order(served(component), visible_field)

    if component_type in LEAK_SHAPES:
        assert shown != component.answer["order"]
    else:
        _field, answer_order = CHOICE_LIST_SHAPES[component_type]
        assert shown != answer_order(component.answer)


# ── The shuffle is a permutation, not an edit ─────────────────────────────


@pytest.mark.parametrize("component_type", ORDERING_TYPES + tuple(CHOICE_LIST_SHAPES))
def test_every_entry_survives_exactly_once(component_type: str):
    component = build(component_type)
    visible_field = ANSWER_BEARING_ORDER[component_type][0].field
    source = component.content[visible_field]

    # Compared as an id → entry map so the assertion is about identity rather
    # than position, and so a gated field the projection withholds (a timeline's
    # `date_label`) does not read as a lost entry.
    def by_id(entries: list[dict], translate=None) -> dict[str, dict]:
        return {
            (translate[entry["id"]] if translate else entry["id"]): {
                key: value for key, value in entry.items() if key not in {"date_label", "id"}
            }
            for entry in entries
        }

    for _attempt in range(20):
        projection = served(component)
        shown = projection.payload["content"][visible_field]
        assert len(shown) == len(source)
        assert sorted(projection.aliases[entry["id"]] for entry in shown) == sorted(ids(source))
        # Entries travel whole: an alias never picks up another entry's text.
        assert by_id(shown, projection.aliases) == by_id(source)


def test_the_source_component_is_never_mutated():
    """A projection that shuffled in place would corrupt the evaluator's copy."""
    component = build("sequence_order")
    before = json.dumps(component.content)
    answer_before = json.dumps(component.answer)

    for _attempt in range(10):
        component.learner_payload()

    assert json.dumps(component.content) == before
    assert json.dumps(component.answer) == answer_before


def test_a_single_entry_list_is_returned_unchanged():
    """One arrangement exists; returning it is the whole truth."""
    content = {"steps": [{"id": "only", "text": "The only step."}]}

    assert shuffled_content("sequence_order", content) == content


def test_an_empty_list_is_handled_without_raising():
    assert shuffled_content("sequence_order", {"steps": []}) == {"steps": []}


def test_a_two_entry_list_is_always_rearranged():
    """The case a fair shuffle gets wrong half the time if nobody checks."""
    content = {"steps": [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}]}

    for _attempt in range(40):
        assert ids(shuffled_content("sequence_order", content)["steps"]) == ["b", "a"]


def test_duplicate_visible_labels_do_not_confuse_identity():
    """Identity is the opaque id; two entries may read identically."""
    content = {
        "steps": [
            {"id": "first", "text": "Stir."},
            {"id": "second", "text": "Stir."},
            {"id": "third", "text": "Stir."},
        ]
    }

    shown = shuffled_content("sequence_order", content)["steps"]

    assert sorted(ids(shown)) == ["first", "second", "third"]
    assert len(shown) == 3


def test_an_already_scrambled_source_is_still_rearranged_safely():
    """Nothing here depends on the source being in the correct order."""
    content = {
        "steps": [
            {"id": "c", "text": "Third"},
            {"id": "a", "text": "First"},
            {"id": "b", "text": "Second"},
        ]
    }

    shown = ids(shuffled_content("sequence_order", content)["steps"])

    assert sorted(shown) == ["a", "b", "c"]
    assert shown != ["c", "a", "b"]


def test_a_degenerate_shuffle_cannot_leave_the_answer_showing():
    """An injected shuffle that does nothing must still not return the answer.

    The guard matters because the shuffle is the only thing standing between the
    learner and the key: a no-op has to be corrected, not trusted.
    """
    content = {"steps": [{"id": name, "text": name} for name in ("a", "b", "c", "d")]}
    answer = {"order": ["a", "b", "c", "d"]}

    shown = ids(
        shuffled_content("sequence_order", content, answer=answer, shuffle=lambda items: None)[
            "steps"
        ]
    )

    assert shown != answer["order"]
    assert sorted(shown) == ["a", "b", "c", "d"]


def test_a_shuffle_that_proposes_the_answer_is_overruled():
    """The defect this replaced: the projection compared against the *source*.

    A manifest may legitimately be authored pre-scrambled — the source order is
    then not the answer — and a shuffle landing on the answer passed the old
    check, because it was different from what the author had typed.
    """
    content = {"steps": [{"id": "b", "text": "B"}, {"id": "a", "text": "A"}]}
    answer = {"order": ["a", "b"]}

    def propose_the_answer(items):
        items.sort(key=lambda entry: answer["order"].index(entry["id"]))

    for _attempt in range(20):
        shown = ids(
            shuffled_content("sequence_order", content, answer=answer, shuffle=propose_the_answer)[
                "steps"
            ]
        )
        assert shown != answer["order"]
        assert sorted(shown) == ["a", "b"]


@pytest.mark.parametrize("component_type", ORDERING_TYPES + tuple(CHOICE_LIST_SHAPES))
def test_a_pre_scrambled_manifest_is_still_never_served_in_answer_order(component_type: str):
    """Source order and answer order genuinely differ, and only one is forbidden."""
    component = build(component_type)
    field = ANSWER_BEARING_ORDER[component_type][0]
    forbidden = field.forbidden(component.answer, component.content)

    # Author the source in an order that is not the answer.
    scrambled = list(reversed(component.content[field.field]))
    content = {**component.content, field.field: scrambled}
    assert ids(scrambled) != forbidden

    def propose_the_answer(items):
        items.sort(key=lambda entry: forbidden.index(entry["id"]))

    shown = ids(
        shuffled_content(
            component_type, content, answer=component.answer, shuffle=propose_the_answer
        )[field.field]
    )

    assert shown != forbidden
    assert sorted(shown) == sorted(ids(scrambled))


def test_a_type_with_no_answer_bearing_order_is_left_alone():
    """Option order is not the answer when the answer is an id."""
    component = build("multiple_choice")
    source = ids(component.content["options"])

    for _attempt in range(20):
        projection = served(component)
        shown = [
            projection.aliases[entry["id"]] for entry in projection.payload["content"]["options"]
        ]
        assert shown == source


# ── Gated content ─────────────────────────────────────────────────────────


def test_a_timeline_that_hides_dates_is_not_served_its_dates():
    """A date label is an ordering clue, so `show_dates: false` means absent."""
    component = build("timeline")

    assert component.content["show_dates"] is False
    assert any("date_label" in event for event in component.content["events"])

    events = component.learner_payload()["content"]["events"]
    assert all("date_label" not in event for event in events)
    assert "1853" not in json.dumps(events)


def test_a_timeline_that_shows_dates_keeps_each_date_with_its_own_event():
    """Shuffling must move the label with the event, not independently."""
    component = build_component(
        example(
            "timeline",
            content={
                "show_dates": True,
                "events": [
                    {"id": "perry", "text": "Squadron arrives", "date_label": "1853"},
                    {"id": "alliance", "text": "Alliance formed", "date_label": "1866"},
                    {"id": "restoration", "text": "Restoration", "date_label": "1868"},
                ],
            },
        ),
        "component",
    )
    expected = {"perry": "1853", "alliance": "1866", "restoration": "1868"}

    for _attempt in range(20):
        projection = served(component)
        events = projection.payload["content"]["events"]
        assert {
            projection.aliases[event["id"]]: event["date_label"] for event in events
        } == expected


def test_the_gate_list_names_only_real_fields():
    """Guards the table against a typo that would silently disable the gate."""
    from learning_studio.components import SPEC_BY_TYPE

    for component_type, gates in GATED_CONTENT.items():
        content_fields = {field.name for field in SPEC_BY_TYPE[component_type].content}
        for list_field, _entry_field, flag in gates:
            assert list_field in content_fields
            assert flag in content_fields


def test_the_order_table_names_only_real_fields():
    from learning_studio.components import SPEC_BY_TYPE

    for component_type, rules in ANSWER_BEARING_ORDER.items():
        content_fields = {field.name for field in SPEC_BY_TYPE[component_type].content}
        for rule in rules:
            assert rule.field in content_fields, f"{component_type}.{rule.field}"
            # And the rule really reconstructs an arrangement, rather than
            # silently returning nothing and forbidding nothing.
            component = build(component_type)
            assert rule.forbidden(component.answer, component.content)


# ── Nothing else leaks along the way ─────────────────────────────────────


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_shuffling_introduces_no_canary_and_loses_no_field(component_type: str):
    """The projection stays a projection: no evaluator text arrives, and no
    content field disappears except one a gate deliberately withholds."""
    component = build(component_type)
    payload = component.learner_payload()

    assert CANARY not in json.dumps(payload)
    if component.content:
        assert set(payload["content"]) == set(component.content)


# ── An answer record is a set of statements, not a sequence ───────────────
#
# `pairs` and `labels` mean the same thing whatever order they are written in.
# The first version of the guard read the forbidden arrangement straight off
# those records, so an author who happened to list them in a different order
# from the rows produced a "forbidden" list that was not the answer — the guard
# rejected candidates for matching a phantom, and settled on the real one.


PARALLEL_SHAPES = {
    # component -> (option bank, answer field, row field, row key, value key)
    "matching": ("right", "pairs", "left", "left_id", "right_id"),
    "labeling": ("label_bank", "labels", "markers", "marker_id", "label_id"),
}


def real_answer_arrangement(component, component_type: str) -> list[str]:
    """The order the bank must never be served in, read the way a card is read.

    Built here independently of the implementation — row by row, looking each row
    up in the answer — so this agrees with `_parallel_order` only if both are
    right.
    """
    _bank, answer_field, row_field, row_key, value_key = PARALLEL_SHAPES[component_type]
    chosen = {entry[row_key]: entry[value_key] for entry in component.answer[answer_field]}
    return [chosen[row["id"]] for row in component.content[row_field]]


@pytest.mark.parametrize("component_type", tuple(PARALLEL_SHAPES))
@pytest.mark.parametrize("reversed_records", [False, True])
def test_an_option_bank_is_never_served_in_answer_order(
    component_type: str, reversed_records: bool
):
    """Both record orders, because the exercise is identical either way."""
    bank, answer_field, _row_field, _row_key, _value_key = PARALLEL_SHAPES[component_type]
    source = example(component_type)
    if reversed_records:
        source["answer"][answer_field] = list(reversed(source["answer"][answer_field]))
    component = build_component(source, "component")
    forbidden = real_answer_arrangement(component, component_type)

    for _attempt in range(100):
        shown = ids(
            shuffled_content(component_type, component.content, answer=component.answer)[bank]
        )
        assert shown != forbidden, (
            f"{component_type} served the real answer with the answer records "
            f"{'reversed' if reversed_records else 'as authored'}"
        )


@pytest.mark.parametrize("component_type", tuple(PARALLEL_SHAPES))
def test_reordering_the_answer_records_does_not_change_what_is_forbidden(component_type: str):
    """The rule reads the rows, so record order cannot move the target."""
    bank, answer_field, *_ = PARALLEL_SHAPES[component_type]
    rule = ANSWER_BEARING_ORDER[component_type][0]
    assert rule.field == bank

    as_authored = build_component(example(component_type), "component")
    source = example(component_type)
    source["answer"][answer_field] = list(reversed(source["answer"][answer_field]))
    reordered = build_component(source, "component")

    assert rule.forbidden(as_authored.answer, as_authored.content) == rule.forbidden(
        reordered.answer, reordered.content
    )
    assert rule.forbidden(as_authored.answer, as_authored.content) == real_answer_arrangement(
        as_authored, component_type
    )


@pytest.mark.parametrize("component_type", tuple(PARALLEL_SHAPES))
def test_a_two_row_card_is_the_case_the_old_guard_got_exactly_backwards(component_type: str):
    """With two entries there are two arrangements, so a wrong exclusion picks
    the answer every single time rather than half of them."""
    bank, answer_field, row_field, row_key, value_key = PARALLEL_SHAPES[component_type]
    source = example(component_type)
    source["answer"][answer_field] = list(reversed(source["answer"][answer_field]))
    component = build_component(source, "component")

    assert len(component.content[bank]) == 2, "this fixture is no longer the two-entry case"
    forbidden = real_answer_arrangement(component, component_type)

    served = {
        tuple(
            ids(shuffled_content(component_type, component.content, answer=component.answer)[bank])
        )
        for _attempt in range(100)
    }

    assert tuple(forbidden) not in served
    assert len(served) == 1, "with two entries there is exactly one other arrangement"


def test_a_rule_whose_answer_does_not_cover_every_row_forbids_nothing_rather_than_guessing():
    """Validation makes this unreachable; the fallback is still stated.

    A partial arrangement would be a guess, and excluding a guess is how the
    defect above worked. Forbidding nothing leaves the "not the source order"
    half of the guard doing the work.
    """
    from learning_studio.components import ANSWER_BEARING_ORDER as RULES

    rule = RULES["matching"][0]
    content = {
        "left": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
        "right": [{"id": "one", "text": "One"}, {"id": "two", "text": "Two"}],
    }

    assert rule.forbidden({"pairs": [{"left_id": "a", "right_id": "one"}]}, content) == []
    assert rule.forbidden({}, content) == []
