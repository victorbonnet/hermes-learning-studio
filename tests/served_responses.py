"""Build a valid response for a component *as it was served*.

Written against the visible projection rather than against the manifest, which is
the whole point: the identifiers a learner may name are the aliased ones they were
given, and a helper that reached for the canonical ids would be testing a path no
client has.

Deliberately not a second copy of the contract. Each builder here answers the
question a learner would answer — "pick one", "put these in order", "fill every
gap" — from what is on the card. If it agrees with
:mod:`learning_studio.responses`, that is two independent readings of the same
component agreeing, which is worth something; a table derived from the contract
would agree with it by construction and prove nothing.
"""

from __future__ import annotations

from typing import Any


def _ids(content: dict[str, Any], field: str) -> list[str]:
    return [entry["id"] for entry in content.get(field, []) if isinstance(entry, dict)]


def _words(content: dict[str, Any], parts: int = 1) -> str:
    """Enough words to satisfy a declared minimum, split across ``parts``."""
    minimum = int(content.get("min_words") or 0)
    maximum = int(content.get("max_words") or 0)
    total = max(minimum, parts)
    if maximum:
        total = min(total, maximum)
    per_part = max(1, -(-total // parts))
    return " ".join(["word"] * per_part)


def response_for(component_type: str, content: dict[str, Any]) -> dict[str, Any]:
    """A response the API will accept for this component, as served."""
    builder = _BUILDERS.get(component_type)
    if builder is None:
        raise KeyError(f"no response builder for {component_type}")
    return builder(content or {})


def _first_option(content):
    return {"option_id": _ids(content, "options")[0]}


def _text(content):
    return {"text": _words(content)}


def _order(field):
    def build(content):
        # Reversed, so the submitted order is rarely the one displayed and the
        # test is not accidentally asserting that nothing moved.
        return {"order": list(reversed(_ids(content, field)))}

    return build


def _prompts(field):
    def build(content):
        prompts = content.get(field) or []
        return {"responses": [_words(content, len(prompts)) for _ in prompts]}

    return build


_BUILDERS = {
    "multiple_choice": _first_option,
    "image_choice": _first_option,
    "scenario_choice": _first_option,
    "multi_select": lambda content: {"option_ids": _ids(content, "options")[:1]},
    "true_false": lambda content: {"value": True},
    "classification": lambda content: {
        "assignments": [
            {"item_id": item, "category_id": _ids(content, "categories")[0]}
            for item in _ids(content, "items")
        ]
    },
    "categorization": lambda content: {
        "assignments": [
            {"item_id": item, "category_ids": _ids(content, "categories")[:1]}
            for item in _ids(content, "items")
        ]
    },
    "fill_blank": lambda content: {
        "blanks": [{"blank_id": blank, "text": "filled"} for blank in _ids(content, "blanks")]
    },
    "short_answer": _text,
    "free_response": _text,
    "translation": _text,
    "error_correction": _text,
    "typed_recall": _text,
    "image_observation": _text,
    "diagram": _text,
    "rubric_response": _text,
    "code_response": lambda content: {"code": "def solve():\n    return 1"},
    "sentence_order": _order("tokens"),
    "sequence_order": _order("steps"),
    "timeline": _order("events"),
    "process_flow": _order("stages"),
    "matching": lambda content: {
        "pairs": [
            {"left_id": left, "right_id": _ids(content, "right")[0]}
            for left in _ids(content, "left")
        ]
    },
    "labeling": lambda content: {
        "labels": [
            {"marker_id": marker, "label_id": _ids(content, "label_bank")[0]}
            for marker in _ids(content, "markers")
        ]
    },
    "table_grid": lambda content: {
        "cells": [
            {"row_id": row, "column_id": column, "text": "x"}
            for row in _ids(content, "rows")
            for column in _ids(content, "columns")
            if (row, column)
            not in {
                (cell["row_id"], cell["column_id"]) for cell in content.get("prefilled_cells", [])
            }
        ]
    },
    "decision_path": lambda content: {
        "decisions": [
            {"step_id": step["id"], "option_id": step["options"][0]["id"]}
            for step in content.get("steps", [])
        ]
    },
    "case_study": _prompts("questions"),
    "self_explanation": _prompts("prompts"),
    "reflection": _prompts("prompts"),
    "confidence_rating": lambda content: {"rating": content["scale_min"]},
    "hotspot": lambda content: {"points": [{"x": 0.25, "y": 0.75}]},
    "flashcard": lambda content: {"text": "my recall", "self_rating": "good"},
}
