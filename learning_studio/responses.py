"""What a learner may submit, per component type, and what it means.

Generic JSON bounds — size, depth, breadth — are necessary and were all the API
had. They are not sufficient: ``{"component_id": "tf"}`` carries no response at
all, and a ceiling on how large a value may be says nothing about whether the
value is an answer to the question that was asked. A ``true_false`` card advanced
on a missing response, and a ``matching`` card advanced on half of one.

So each type declares a closed contract here, and a response is checked against
the component the learner was actually shown before any session state moves.
Three things fall out of that:

1. **Closed, like the component registry it mirrors.** An unknown field is
   refused rather than ignored, because a field nobody declared is either a
   client bug or somebody trying something, and neither should be stored.
2. **Identifiers are checked against what was served.** A response may only name
   options, items, markers and blanks that appear in *this* learner's projection
   of *this* component. That is what makes the aliasing in
   :mod:`learning_studio.components` enforceable rather than decorative.
3. **Aliases are translated back here.** The contract knows which fields hold
   identifiers, so it is the one place that can resolve them — and it returns a
   response in canonical terms, which is what an evaluator will eventually read.

The frontend builds exactly these shapes. ``tests/test_response_contracts.py``
checks both directions: every type, and every renderer's output against the
contract for its own type.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .components import COMPONENT_TYPES, SPEC_BY_TYPE

#: Said to a client whose response does not fit its component's contract. One
#: message for every reason, as everywhere else in this API: the detail goes to
#: the log as a fixed ``reason`` token, never to the caller.
INVALID_RESPONSE_MESSAGE = "That answer was not in a form this exercise accepts."

#: The self-ratings a flashcard accepts. Matches the frontend's own list.
SELF_RATINGS: tuple[str, ...] = ("again", "hard", "good", "easy")

#: The one response accepted for any type: a card the *client* could not render.
#: A registry the server validates against can legitimately be ahead of a
#: frontend nobody updated, and a learner should not be stuck on a card their app
#: cannot draw. Spelled exactly, so it cannot be confused with an answer.
SKIPPED_RESPONSE = {"skipped": True}


class ResponseContractError(ValueError):
    """A response did not match its component's contract.

    ``reason`` is a fixed token for logs. It never contains a submitted value.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(INVALID_RESPONSE_MESSAGE)
        self.reason = reason


def _fail(reason: str) -> None:
    raise ResponseContractError(reason)


# ── Reading the component that was served ─────────────────────────────────


def _entries(content: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = content.get(field)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _ids(content: dict[str, Any], field: str) -> list[str]:
    return [str(item["id"]) for item in _entries(content, field) if isinstance(item.get("id"), str)]


# ── Primitive checks ──────────────────────────────────────────────────────


def _object(value: Any, reason: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(reason)
    return value


def _fields(
    response: dict[str, Any], *, required: Iterable[str], optional: Iterable[str] = ()
) -> None:
    """Exactly the declared fields: none missing, none extra."""
    allowed = set(required) | set(optional)
    missing = [name for name in required if name not in response]
    if missing:
        _fail("field_missing")
    unknown = set(response) - allowed
    if unknown:
        _fail("field_unknown")


def _text(value: Any, *, required: bool = True) -> str:
    if not isinstance(value, str):
        _fail("not_text")
    if required and not value.strip():
        _fail("text_empty")
    return value


def _identifier(value: Any, known: set[str]) -> str:
    if not isinstance(value, str):
        _fail("identifier_not_text")
    if value not in known:
        # Either a stale client or somebody naming an option that was never
        # served. The message does not distinguish them.
        _fail("identifier_unknown")
    return value


def _list(value: Any, reason: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(reason)
    return value


def _exact_cover(chosen: list[str], expected: list[str], reason: str) -> None:
    """One entry per declared item, none repeated, none invented."""
    if sorted(chosen) != sorted(expected):
        _fail(reason)


# ── The contracts ─────────────────────────────────────────────────────────
#
# Each takes the *visible* content and the submitted response, and returns the
# response in canonical terms. `resolve` turns a served alias back into the
# identifier the evaluator knows.

Resolver = Callable[[str], str]


def _single_choice(field: str, option_field: str = "options") -> Callable[..., dict[str, Any]]:
    def contract(content, response, resolve):
        _fields(response, required=[field])
        chosen = _identifier(response[field], set(_ids(content, option_field)))
        return {field: resolve(chosen)}

    return contract


def _open_text(field: str = "text") -> Callable[..., dict[str, Any]]:
    def contract(content, response, resolve):
        _fields(response, required=[field])
        text = _text(response[field])
        _word_bounds(content, [text])
        return {field: text}

    return contract


def _word_bounds(content: dict[str, Any], written: list[str]) -> None:
    """The component's own declared length, checked against the total.

    Checked here as well as in the browser because the browser is a convenience.
    `min_words` on a multi-prompt component is a total, which is the reading the
    frontend uses and the README documents.
    """
    counted = len(" ".join(written).split())
    minimum = content.get("min_words")
    maximum = content.get("max_words")
    if isinstance(minimum, int) and counted < minimum:
        _fail("too_few_words")
    if isinstance(maximum, int) and counted > maximum:
        _fail("too_many_words")


def _ordering(field: str) -> Callable[..., dict[str, Any]]:
    def contract(content, response, resolve):
        _fields(response, required=["order"])
        submitted = _list(response["order"], "order_not_a_list")
        known = set(_ids(content, field))
        chosen = [_identifier(entry, known) for entry in submitted]
        # A partial ordering is not a partial answer, it is an unusable one.
        _exact_cover(chosen, list(known), "order_incomplete")
        return {"order": [resolve(entry) for entry in chosen]}

    return contract


def _prompt_list(field: str) -> Callable[..., dict[str, Any]]:
    def contract(content, response, resolve):
        _fields(response, required=["responses"])
        submitted = _list(response["responses"], "responses_not_a_list")
        prompts = content.get(field)
        expected = len(prompts) if isinstance(prompts, list) else 0
        if len(submitted) != expected or expected == 0:
            _fail("responses_incomplete")
        written = [_text(entry) for entry in submitted]
        _word_bounds(content, written)
        return {"responses": written}

    return contract


def _true_false(content, response, resolve):
    _fields(response, required=["value"])
    if not isinstance(response["value"], bool):
        _fail("value_not_boolean")
    return {"value": response["value"]}


def _multi_select(content, response, resolve):
    _fields(response, required=["option_ids"])
    submitted = _list(response["option_ids"], "option_ids_not_a_list")
    if not submitted:
        _fail("nothing_chosen")
    known = set(_ids(content, "options"))
    chosen = [_identifier(entry, known) for entry in submitted]
    if len(set(chosen)) != len(chosen):
        _fail("option_repeated")
    return {"option_ids": [resolve(entry) for entry in chosen]}


def _classification(content, response, resolve):
    _fields(response, required=["assignments"])
    items = set(_ids(content, "items"))
    categories = set(_ids(content, "categories"))
    assignments = _list(response["assignments"], "assignments_not_a_list")
    seen: list[str] = []
    projected = []
    for raw in assignments:
        entry = _object(raw, "assignment_not_an_object")
        _fields(entry, required=["item_id", "category_id"])
        item = _identifier(entry["item_id"], items)
        category = _identifier(entry["category_id"], categories)
        seen.append(item)
        projected.append({"item_id": resolve(item), "category_id": resolve(category)})
    _exact_cover(seen, list(items), "items_incomplete")
    return {"assignments": projected}


def _categorization(content, response, resolve):
    _fields(response, required=["assignments"])
    items = set(_ids(content, "items"))
    categories = set(_ids(content, "categories"))
    allow_multiple = content.get("allow_multiple") is True
    assignments = _list(response["assignments"], "assignments_not_a_list")
    seen: list[str] = []
    projected = []
    for raw in assignments:
        entry = _object(raw, "assignment_not_an_object")
        _fields(entry, required=["item_id", "category_ids"])
        item = _identifier(entry["item_id"], items)
        chosen = _list(entry["category_ids"], "category_ids_not_a_list")
        if not chosen:
            _fail("nothing_chosen")
        if not allow_multiple and len(chosen) > 1:
            _fail("multiple_not_allowed")
        resolved = [_identifier(value, categories) for value in chosen]
        if len(set(resolved)) != len(resolved):
            _fail("category_repeated")
        seen.append(item)
        projected.append(
            {"item_id": resolve(item), "category_ids": [resolve(value) for value in resolved]}
        )
    _exact_cover(seen, list(items), "items_incomplete")
    return {"assignments": projected}


def _fill_blank(content, response, resolve):
    _fields(response, required=["blanks"])
    known = set(_ids(content, "blanks"))
    submitted = _list(response["blanks"], "blanks_not_a_list")
    seen: list[str] = []
    projected = []
    for raw in submitted:
        entry = _object(raw, "blank_not_an_object")
        _fields(entry, required=["blank_id", "text"])
        blank = _identifier(entry["blank_id"], known)
        seen.append(blank)
        projected.append({"blank_id": resolve(blank), "text": _text(entry["text"])})
    _exact_cover(seen, list(known), "blanks_incomplete")
    return {"blanks": projected}


def _matching(content, response, resolve):
    _fields(response, required=["pairs"])
    left = set(_ids(content, "left"))
    right = set(_ids(content, "right"))
    submitted = _list(response["pairs"], "pairs_not_a_list")
    seen: list[str] = []
    projected = []
    for raw in submitted:
        entry = _object(raw, "pair_not_an_object")
        _fields(entry, required=["left_id", "right_id"])
        one = _identifier(entry["left_id"], left)
        other = _identifier(entry["right_id"], right)
        seen.append(one)
        projected.append({"left_id": resolve(one), "right_id": resolve(other)})
    _exact_cover(seen, list(left), "pairs_incomplete")
    return {"pairs": projected}


def _labeling(content, response, resolve):
    _fields(response, required=["labels"])
    markers = set(_ids(content, "markers"))
    bank = set(_ids(content, "label_bank"))
    submitted = _list(response["labels"], "labels_not_a_list")
    seen: list[str] = []
    projected = []
    for raw in submitted:
        entry = _object(raw, "label_not_an_object")
        _fields(entry, required=["marker_id", "label_id"])
        marker = _identifier(entry["marker_id"], markers)
        label = _identifier(entry["label_id"], bank)
        seen.append(marker)
        projected.append({"marker_id": resolve(marker), "label_id": resolve(label)})
    _exact_cover(seen, list(markers), "labels_incomplete")
    return {"labels": projected}


def _table_grid(content, response, resolve):
    _fields(response, required=["cells"])
    rows = set(_ids(content, "rows"))
    columns = set(_ids(content, "columns"))
    prefilled = {
        (str(cell.get("row_id")), str(cell.get("column_id")))
        for cell in _entries(content, "prefilled_cells")
    }
    expected = sorted(
        (row, column) for row in rows for column in columns if (row, column) not in prefilled
    )
    submitted = _list(response["cells"], "cells_not_a_list")
    seen: list[tuple[str, str]] = []
    projected = []
    for raw in submitted:
        entry = _object(raw, "cell_not_an_object")
        _fields(entry, required=["row_id", "column_id", "text"])
        row = _identifier(entry["row_id"], rows)
        column = _identifier(entry["column_id"], columns)
        if (row, column) in prefilled:
            _fail("cell_already_filled")
        seen.append((row, column))
        projected.append(
            {
                "row_id": resolve(row),
                "column_id": resolve(column),
                "text": _text(entry["text"]),
            }
        )
    if sorted(seen) != expected:
        _fail("cells_incomplete")
    return {"cells": projected}


def _decision_path(content, response, resolve):
    _fields(response, required=["decisions"])
    steps = {
        str(step["id"]): {str(option["id"]) for option in _entries(step, "options")}
        for step in _entries(content, "steps")
        if isinstance(step.get("id"), str)
    }
    submitted = _list(response["decisions"], "decisions_not_a_list")
    seen: list[str] = []
    projected = []
    for raw in submitted:
        entry = _object(raw, "decision_not_an_object")
        _fields(entry, required=["step_id", "option_id"])
        step = _identifier(entry["step_id"], set(steps))
        # The option has to belong to *this* step, not merely to the card.
        option = _identifier(entry["option_id"], steps[step])
        seen.append(step)
        projected.append({"step_id": resolve(step), "option_id": resolve(option)})
    _exact_cover(seen, list(steps), "decisions_incomplete")
    return {"decisions": projected}


def _confidence_rating(content, response, resolve):
    _fields(response, required=["rating"])
    rating = response["rating"]
    # `bool` is an `int` in Python, and `True` is not a rating.
    if not isinstance(rating, int) or isinstance(rating, bool):
        _fail("rating_not_an_integer")
    low = content.get("scale_min")
    high = content.get("scale_max")
    if not isinstance(low, int) or not isinstance(high, int) or not low <= rating <= high:
        _fail("rating_out_of_range")
    return {"rating": rating}


def _hotspot(content, response, resolve):
    _fields(response, required=["points"])
    submitted = _list(response["points"], "points_not_a_list")
    if len(submitted) != 1:
        _fail("points_not_one")
    projected = []
    for raw in submitted:
        entry = _object(raw, "point_not_an_object")
        _fields(entry, required=["x", "y"])
        point = {}
        for axis in ("x", "y"):
            value = entry[axis]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _fail("coordinate_not_a_number")
            if not 0.0 <= float(value) <= 1.0:
                _fail("coordinate_out_of_range")
            point[axis] = float(value)
        projected.append(point)
    return {"points": projected}


def _flashcard(content, response, resolve):
    _fields(response, required=["text", "self_rating"])
    if response["self_rating"] not in SELF_RATINGS:
        _fail("self_rating_unknown")
    # The recall must be present. Whether it is *the* recall — the one frozen
    # when the card was turned over — is the session's business, checked in the
    # API where the session is.
    return {"text": _text(response["text"]), "self_rating": response["self_rating"]}


def _code_response(content, response, resolve):
    _fields(response, required=["code"])
    # Stored as a string, never parsed, compiled or run.
    return {"code": _text(response["code"])}


CONTRACTS: dict[str, Callable[..., dict[str, Any]]] = {
    "multiple_choice": _single_choice("option_id"),
    "multi_select": _multi_select,
    "true_false": _true_false,
    "classification": _classification,
    "fill_blank": _fill_blank,
    "short_answer": _open_text(),
    "free_response": _open_text(),
    "translation": _open_text(),
    "error_correction": _open_text(),
    "code_response": _code_response,
    "sentence_order": _ordering("tokens"),
    "sequence_order": _ordering("steps"),
    "matching": _matching,
    "categorization": _categorization,
    "flashcard": _flashcard,
    "typed_recall": _open_text(),
    "image_observation": _open_text(),
    "image_choice": _single_choice("option_id"),
    "diagram": _open_text(),
    "hotspot": _hotspot,
    "labeling": _labeling,
    "timeline": _ordering("events"),
    "process_flow": _ordering("stages"),
    "table_grid": _table_grid,
    "scenario_choice": _single_choice("option_id"),
    "decision_path": _decision_path,
    "case_study": _prompt_list("questions"),
    "confidence_rating": _confidence_rating,
    "self_explanation": _prompt_list("prompts"),
    "reflection": _prompt_list("prompts"),
    "rubric_response": _open_text(),
}


def validate_component_response(
    component_type: str,
    content: dict[str, Any],
    response: Any,
    *,
    resolve: Resolver | None = None,
) -> dict[str, Any]:
    """Check a response against its component, and return it in canonical terms.

    ``content`` is the *visible* projection the learner was served — aliased ids
    and all — because that is what they could legitimately have named. ``resolve``
    turns each of those aliases back into the identifier the evaluator knows;
    without one the response is returned as submitted, which is what a caller with
    no aliases wants.
    """
    if component_type not in CONTRACTS:
        _fail("type_unknown")
    if not isinstance(response, dict):
        # Includes `None`, which is what a request body with no `response` at all
        # produced — and which used to advance the exercise.
        _fail("response_missing")
    if response == SKIPPED_RESPONSE:
        return dict(SKIPPED_RESPONSE)

    identity: Resolver = resolve or (lambda value: value)
    return CONTRACTS[component_type](content or {}, response, identity)


def contract_covers_every_type() -> bool:
    """True when every registry type has a contract. Asserted by the tests."""
    return set(CONTRACTS) == set(COMPONENT_TYPES) == set(SPEC_BY_TYPE)
