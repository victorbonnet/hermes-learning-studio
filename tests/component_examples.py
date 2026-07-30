"""One valid example of every component type, for the registry tests.

Two rules make these useful rather than decorative:

1. **Every hidden field carries a canary.** Each answer, rubric, hint, scoring
   note, per-option feedback, branch and evaluator note contains a string
   found nowhere else. A test can then assert that a learner-facing payload
   contains no canary at all, recursively, instead of checking a hand-written
   list of field names that will fall behind the code.
2. **The subjects are deliberately unrelated.** Cell biology, Portuguese,
   maritime navigation, fire safety, music theory, road law. A registry that
   had quietly grown a favourite discipline would show up here as a file that
   reads like one syllabus.
"""

from __future__ import annotations

from typing import Any

#: Prefix shared by every string that must never reach a learner.
CANARY = "ZZCANARY"

#: The back of the flashcard example. Not a canary, deliberately — see the note
#: on that example. Distinctive enough to search a response body for.
FLASHCARD_BACK = "yama"


def canary(component_type: str, field: str) -> str:
    """A distinctive marker for one hidden field of one component type."""
    return f"{CANARY}-{component_type}-{field}"


def _evaluation(component_type: str, **extra: Any) -> dict[str, Any]:
    """The judging half, with a canary in every field that supports one."""
    evaluation: dict[str, Any] = {
        "hints": [canary(component_type, "hint")],
        "feedback": {
            "correct": canary(component_type, "feedback-correct"),
            "incorrect": canary(component_type, "feedback-incorrect"),
        },
        "notes": canary(component_type, "notes"),
    }
    evaluation.update(extra)
    return evaluation


def _rubric(component_type: str) -> list[dict[str, Any]]:
    return [
        {
            "criterion": canary(component_type, "criterion"),
            "levels": [
                {
                    "label": "Secure",
                    "descriptor": canary(component_type, "descriptor"),
                    "points": 3,
                },
                {"label": "Emerging", "descriptor": "Partly there.", "points": 1},
            ],
        }
    ]


def _open_component(component_type: str, prompt: str, content: dict[str, Any]) -> dict[str, Any]:
    """A component judged against a rubric rather than an answer key."""
    return {
        "id": component_type.replace("_", "-"),
        "type": component_type,
        "prompt": prompt,
        "content": content,
        "evaluation": _evaluation(
            component_type,
            rubric=_rubric(component_type),
            scoring={"mode": "rubric", "points": 6},
        ),
    }


#: Every component type, keyed by type name. Ordered as the registry declares
#: them so a reader can compare the two side by side.
EXAMPLES: dict[str, dict[str, Any]] = {
    # ── Selection ─────────────────────────────────────────────────────────
    "multiple_choice": {
        "id": "resp-01",
        "type": "multiple_choice",
        "prompt": "In eukaryotes, where does the Krebs cycle take place?",
        "content": {
            "options": [
                {"id": "matrix", "text": "The mitochondrial matrix"},
                {"id": "cytosol", "text": "The cytosol"},
                {"id": "thylakoid", "text": "The thylakoid membrane"},
            ],
            "shuffle": True,
        },
        "answer": {"option_id": "matrix"},
        "evaluation": _evaluation(
            "multiple_choice",
            scoring={"mode": "exact", "points": 1},
            feedback={
                "correct": canary("multiple_choice", "feedback-correct"),
                "incorrect": canary("multiple_choice", "feedback-incorrect"),
                "per_option": [
                    {"option_id": "cytosol", "text": canary("multiple_choice", "per-option")}
                ],
            },
        ),
    },
    "multi_select": {
        "id": "nav-02",
        "type": "multi_select",
        "prompt": "Which of these are required on a vessel under 12 metres?",
        "content": {
            "options": [
                {"id": "lifejackets", "text": "One lifejacket per person"},
                {"id": "flares", "text": "In-date distress flares"},
                {"id": "anchor-light", "text": "A masthead anchor light"},
                {"id": "radar", "text": "Radar"},
            ]
        },
        "answer": {"option_ids": ["lifejackets", "flares"], "partial_credit": True},
        "evaluation": _evaluation("multi_select", scoring={"mode": "set", "partial_credit": True}),
    },
    "true_false": {
        "id": "law-03",
        "type": "true_false",
        "prompt": "Decide whether the statement is true.",
        "content": {"statement": "A cyclist may ride two abreast on a road with no cycle lane."},
        "answer": {"value": True},
        "evaluation": _evaluation("true_false", scoring={"mode": "exact"}),
    },
    "classification": {
        "id": "mus-04",
        "type": "classification",
        "prompt": "Put each interval in its family.",
        "content": {
            "items": [
                {"id": "third", "text": "Major third"},
                {"id": "fifth", "text": "Perfect fifth"},
                {"id": "tritone", "text": "Tritone"},
            ],
            "categories": [
                {"id": "consonant", "label": "Consonant"},
                {"id": "dissonant", "label": "Dissonant"},
            ],
        },
        "answer": {
            "assignments": [
                {"item_id": "third", "category_id": "consonant"},
                {"item_id": "fifth", "category_id": "consonant"},
                {"item_id": "tritone", "category_id": "dissonant"},
            ]
        },
        "evaluation": _evaluation("classification", scoring={"mode": "set"}),
    },
    # ── Text input ────────────────────────────────────────────────────────
    "fill_blank": {
        "id": "geo-05",
        "type": "fill_blank",
        "prompt": "Complete the sentence about plate boundaries.",
        "content": {
            "text": "Where two plates move apart, new crust forms at a {{boundary}} boundary.",
            "blanks": [{"id": "boundary", "label": "type of boundary"}],
        },
        "answer": {
            "blanks": [{"blank_id": "boundary", "accepted": ["divergent", "constructive"]}],
            "case_sensitive": False,
        },
        "evaluation": _evaluation("fill_blank", scoring={"mode": "normalised"}),
    },
    "short_answer": {
        "id": "chem-06",
        "type": "short_answer",
        "prompt": "Which molecule enters the citric acid cycle after glycolysis?",
        "content": {"max_words": 6},
        "answer": {"accepted": ["acetyl-CoA", "acetyl coenzyme A"], "case_sensitive": False},
        "evaluation": _evaluation("short_answer", scoring={"mode": "normalised", "points": 2}),
    },
    "free_response": _open_component(
        "free_response",
        "Explain why the 1867 reform widened the franchise less than its supporters hoped.",
        {"min_words": 150, "max_words": 400},
    ),
    "translation": {
        "id": "pt-08",
        "type": "translation",
        "prompt": "Put this sentence into English.",
        "content": {
            "source_text": "Ainda não sei se vou conseguir chegar a tempo.",
            "source_locale": "pt-BR",
            "target_locale": "en",
        },
        "answer": {
            "accepted": [
                "I still do not know whether I will manage to arrive in time.",
                "I do not yet know if I will be able to get there in time.",
            ],
            "accent_sensitive": False,
        },
        "evaluation": _evaluation("translation", scoring={"mode": "normalised"}),
    },
    "error_correction": {
        "id": "wri-09",
        "type": "error_correction",
        "prompt": "One verb form is wrong. Correct it.",
        "content": {
            "text": "The committee have published their findings, which was widely reported.",
            "error_count": 1,
        },
        "answer": {
            "corrections": [
                {
                    "incorrect": "which was widely reported",
                    "correct": "which were widely reported",
                    "explanation": canary("error_correction", "explanation"),
                }
            ]
        },
        "evaluation": _evaluation("error_correction", scoring={"mode": "normalised"}),
    },
    "code_response": {
        "id": "algo-10",
        "type": "code_response",
        "prompt": "Complete the function so it returns the median of a sorted list.",
        "content": {
            "language": "python",
            "starter_code": "def median(values):\n    ...",
            "requirements": ["Handle an even-length list"],
        },
        "answer": {
            "reference_solution": canary("code_response", "solution"),
            "must_include": [canary("code_response", "must-include")],
        },
        "evaluation": _evaluation(
            "code_response", scoring={"mode": "rubric"}, rubric=_rubric("code_response")
        ),
    },
    # ── Ordering and matching ─────────────────────────────────────────────
    "sentence_order": {
        "id": "de-11",
        "type": "sentence_order",
        "prompt": "Arrange the fragments into a grammatical sentence.",
        "content": {
            "tokens": [
                {"id": "t1", "text": "Ich"},
                {"id": "t2", "text": "habe"},
                {"id": "t3", "text": "das Buch"},
                {"id": "t4", "text": "gelesen"},
            ]
        },
        "answer": {"order": ["t1", "t2", "t3", "t4"]},
        "evaluation": _evaluation("sentence_order", scoring={"mode": "ordered"}),
    },
    "sequence_order": {
        "id": "lab-12",
        "type": "sequence_order",
        "prompt": "Put the titration steps in the order you carry them out.",
        "content": {
            "steps": [
                {"id": "rinse", "text": "Rinse the burette with the titrant"},
                {"id": "fill", "text": "Fill the burette and record the initial reading"},
                {"id": "titrate", "text": "Add titrant until the indicator changes"},
            ]
        },
        "answer": {"order": ["rinse", "fill", "titrate"]},
        "evaluation": _evaluation("sequence_order", scoring={"mode": "ordered"}),
    },
    "matching": {
        "id": "art-13",
        "type": "matching",
        "prompt": "Match each painter to their movement.",
        "content": {
            "left": [
                {"id": "monet", "text": "Monet"},
                {"id": "pollock", "text": "Pollock"},
            ],
            "right": [
                {"id": "impressionism", "text": "Impressionism"},
                {"id": "abstract-expressionism", "text": "Abstract expressionism"},
            ],
        },
        "answer": {
            "pairs": [
                {"left_id": "monet", "right_id": "impressionism"},
                {"left_id": "pollock", "right_id": "abstract-expressionism"},
            ],
            "partial_credit": True,
        },
        "evaluation": _evaluation("matching", scoring={"mode": "set", "partial_credit": True}),
    },
    "categorization": {
        "id": "eco-14",
        "type": "categorization",
        "prompt": "Group each material by how it can be disposed of.",
        "content": {
            "items": [
                {"id": "glass", "text": "Glass bottle"},
                {"id": "peel", "text": "Orange peel"},
            ],
            "categories": [
                {"id": "recycling", "label": "Kerbside recycling"},
                {"id": "compost", "label": "Compost"},
            ],
            "allow_multiple": True,
        },
        "answer": {
            "assignments": [
                {"item_id": "glass", "category_ids": ["recycling"]},
                {"item_id": "peel", "category_ids": ["compost"]},
            ]
        },
        "evaluation": _evaluation("categorization", scoring={"mode": "set"}),
    },
    # ── Recall ────────────────────────────────────────────────────────────
    "flashcard": {
        "id": "kanji-15",
        "type": "flashcard",
        "prompt": "Recall the reading, then turn the card over.",
        "content": {"front": "山", "front_note": "One character."},
        # `back` is the one evaluator-only field in the whole registry that a
        # learner may ever be shown, and only through an authorised reveal after
        # committing an attempt. So it carries a real value rather than a canary:
        # a canary here would mean "this string reached a learner" was both the
        # definition of a leak and the definition of the feature working, and
        # every canary test would have to carve out an exception. The mnemonic
        # beside it stays canaried, because nothing discloses that.
        "answer": {
            "back": FLASHCARD_BACK,
            "mnemonic": canary("flashcard", "mnemonic"),
        },
        "evaluation": _evaluation("flashcard", scoring={"mode": "self_check"}),
    },
    "typed_recall": {
        "id": "med-16",
        "type": "typed_recall",
        "prompt": "Type the Latin name for the shin bone.",
        "content": {"cue": "Lower leg, weight-bearing, anterior"},
        "answer": {"accepted": ["tibia"], "case_sensitive": False},
        "evaluation": _evaluation("typed_recall", scoring={"mode": "normalised"}),
    },
    # ── Visual and diagrammatic ───────────────────────────────────────────
    "image_observation": _open_component(
        "image_observation",
        "Describe what the micrograph shows about the tissue's structure.",
        {
            "image": {
                "asset_ref": "asset-micrograph-01",
                "alt_text": "A stained micrograph of plant tissue at 400x magnification.",
            },
            "focus_points": ["Cell wall thickness", "Arrangement of the vascular bundle"],
        },
    ),
    "image_choice": {
        "id": "sign-18",
        "type": "image_choice",
        "prompt": "Which sign means 'no through road'?",
        "content": {
            "options": [
                {
                    "id": "a",
                    "caption": "Sign A",
                    "image": {"asset_ref": "asset-sign-a", "alt_text": "A blue rectangular sign."},
                },
                {
                    "id": "b",
                    "caption": "Sign B",
                    "image": {"asset_ref": "asset-sign-b", "alt_text": "A red circular sign."},
                },
            ]
        },
        "answer": {"option_id": "a"},
        "evaluation": _evaluation("image_choice", scoring={"mode": "exact"}),
    },
    "diagram": {
        "id": "circ-19",
        "type": "diagram",
        "prompt": "What component is drawn between the battery and the lamp?",
        "content": {
            "image": {
                "asset_ref": "asset-circuit-01",
                "alt_text": "A circuit diagram with a battery, one component, and a lamp.",
            },
            "callouts": [{"id": "c1", "text": "Between the battery and the lamp"}],
        },
        "answer": {"accepted": ["a variable resistor", "a rheostat"], "case_sensitive": False},
        "evaluation": _evaluation("diagram", scoring={"mode": "normalised"}),
    },
    "hotspot": {
        "id": "anat-20",
        "type": "hotspot",
        "prompt": "Click on the atrioventricular node.",
        "content": {
            "image": {
                "asset_ref": "asset-heart-01",
                "alt_text": "A cross-section of the human heart, unlabelled.",
            },
            "show_grid": False,
        },
        "answer": {
            "regions": [
                {
                    "id": "av-node",
                    "shape": "rectangle",
                    "points": [0.42, 0.51, 0.5, 0.58],
                    "label": canary("hotspot", "region-label"),
                }
            ],
            "tolerance": 0.02,
        },
        "evaluation": _evaluation("hotspot", scoring={"mode": "exact"}),
    },
    "labeling": {
        "id": "map-21",
        "type": "labeling",
        "prompt": "Drag each name onto the right city, or type the number and the name.",
        "content": {
            "image": {
                "asset_ref": "asset-map-01",
                "alt_text": "An outline map with two numbered markers.",
            },
            "markers": [
                {"id": "m1", "x": 0.31, "y": 0.44},
                {"id": "m2", "x": 0.68, "y": 0.72},
            ],
            "label_bank": [
                {"id": "porto", "text": "Porto"},
                {"id": "faro", "text": "Faro"},
            ],
        },
        "answer": {
            "labels": [
                {"marker_id": "m1", "label_id": "porto"},
                {"marker_id": "m2", "label_id": "faro"},
            ]
        },
        "evaluation": _evaluation("labeling", scoring={"mode": "set"}),
    },
    # ── Timeline and process ──────────────────────────────────────────────
    "timeline": {
        "id": "hist-22",
        "type": "timeline",
        "prompt": "Put these events in the order they happened.",
        "content": {
            "events": [
                {"id": "perry", "text": "Perry's squadron arrives", "date_label": "1853"},
                {"id": "alliance", "text": "The Satsuma-Choshu alliance", "date_label": "1866"},
                {"id": "restoration", "text": "The imperial restoration", "date_label": "1868"},
            ],
            "show_dates": False,
        },
        "answer": {"order": ["perry", "alliance", "restoration"]},
        "evaluation": _evaluation("timeline", scoring={"mode": "ordered"}),
    },
    "process_flow": {
        "id": "safe-23",
        "type": "process_flow",
        "prompt": "Order the stages of evacuating a building.",
        "content": {
            "stages": [
                {"id": "raise", "text": "Raise the alarm"},
                {"id": "assemble", "text": "Go to the assembly point"},
                {"id": "roll", "text": "Take the roll call"},
            ],
            "start_stage_label": "On discovering a fire",
        },
        "answer": {
            "order": ["raise", "assemble", "roll"],
            "transitions": [
                {"from_id": "raise", "to_id": "assemble", "condition": "Exit route is clear"}
            ],
        },
        "evaluation": _evaluation("process_flow", scoring={"mode": "ordered"}),
    },
    # ── Structured information ────────────────────────────────────────────
    "table_grid": {
        "id": "comp-24",
        "type": "table_grid",
        "prompt": "Complete the comparison.",
        "content": {
            "rows": [
                {"id": "mitosis", "header": "Mitosis"},
                {"id": "meiosis", "header": "Meiosis"},
            ],
            "columns": [{"id": "daughters", "header": "Number of daughter cells"}],
        },
        "answer": {
            "cells": [
                {"row_id": "mitosis", "column_id": "daughters", "accepted": ["2", "two"]},
                {"row_id": "meiosis", "column_id": "daughters", "accepted": ["4", "four"]},
            ],
            "case_sensitive": False,
        },
        "evaluation": _evaluation("table_grid", scoring={"mode": "set", "partial_credit": True}),
    },
    # ── Scenarios and decisions ───────────────────────────────────────────
    "scenario_choice": {
        "id": "ward-25",
        "type": "scenario_choice",
        "prompt": "What do you do first?",
        "content": {
            "situation": (
                "A patient's monitor alarms. They are conscious, talking, and their colour "
                "is normal. The lead on the chest has come loose."
            ),
            "options": [
                {"id": "reattach", "text": "Reattach the lead and reassess"},
                {"id": "crash", "text": "Call the crash team"},
            ],
        },
        "answer": {
            "option_id": "reattach",
            "consequences": [
                {"option_id": "crash", "text": canary("scenario_choice", "consequence")}
            ],
        },
        "evaluation": _evaluation("scenario_choice", scoring={"mode": "exact"}),
    },
    "decision_path": {
        "id": "nego-26",
        "type": "decision_path",
        "prompt": "Work through the negotiation one decision at a time.",
        "content": {
            "situation": "A supplier has missed a delivery date for the second time this quarter.",
            "steps": [
                {
                    "id": "opening",
                    "prompt": "How do you open the conversation?",
                    "options": [
                        {"id": "facts", "text": "State the two missed dates and their effect"},
                        {"id": "threat", "text": "Threaten to end the contract"},
                    ],
                },
                {
                    "id": "close",
                    "prompt": "They offer a discount. What do you ask for?",
                    "options": [
                        {"id": "dates", "text": "A revised schedule with penalties"},
                        {"id": "accept", "text": "Accept the discount and move on"},
                    ],
                },
            ],
        },
        "answer": {
            "decisions": [
                {"step_id": "opening", "option_id": "facts"},
                {"step_id": "close", "option_id": "dates"},
            ]
        },
        "evaluation": _evaluation("decision_path", scoring={"mode": "ordered"}),
    },
    "case_study": _open_component(
        "case_study",
        "Read the case and answer each question in turn.",
        {
            "background": (
                "A town of 40,000 people draws its water from a single reservoir. Rainfall "
                "has been 30 percent below average for two years running."
            ),
            "questions": [
                "Which two supply-side measures would you evaluate first, and why?",
                "What would change your recommendation?",
            ],
            "materials": ["The reservoir level series for the last five years"],
        },
    ),
    # ── Reflection and assessment ─────────────────────────────────────────
    "confidence_rating": {
        "id": "conf-28",
        "type": "confidence_rating",
        "prompt": "How confident are you in that answer?",
        "content": {
            "scale_min": 1,
            "scale_max": 5,
            "scale_labels": ["Guessing", "Unsure", "Fairly sure", "Sure", "Certain"],
        },
        "evaluation": _evaluation("confidence_rating", scoring={"mode": "self_check"}),
    },
    "self_explanation": _open_component(
        "self_explanation",
        "Explain, in your own words, why your answer follows from the data.",
        {"prompts": ["What made you rule out the other option?"], "min_words": 60},
    ),
    "reflection": {
        "id": "refl-30",
        "type": "reflection",
        "prompt": "Look back over this session.",
        "content": {
            "prompts": ["What was hardest?", "What will you do differently next time?"],
            "min_words": 40,
        },
        "evaluation": _evaluation("reflection", scoring={"mode": "self_check"}),
    },
    "rubric_response": _open_component(
        "rubric_response",
        "Write a paragraph arguing for one of the two readings.",
        {
            "requirements": ["Cite one line of evidence", "Name the counter-argument"],
            "min_words": 120,
            "max_words": 250,
        },
    ),
}


def example(component_type: str, **overrides: Any) -> dict[str, Any]:
    """A deep-enough copy of one example, with top-level fields replaced."""
    import copy

    entry = copy.deepcopy(EXAMPLES[component_type])
    entry.update(overrides)
    return entry


def manifest(components: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    """A minimal valid manifest around *components*."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "title": "A short practice set",
        "objective": {
            "behavior": "state where the citric acid cycle occurs",
            "condition": "given a labelled cell diagram, unaided",
            "standard": "4 times in 5",
        },
        "instructions": "Work through each item in order. There is no time limit.",
        "ui_locale": "en",
        "expected_duration_minutes": 12,
        "difficulty": "intermediate",
        "components": components if components is not None else [example("multiple_choice")],
    }
    payload.update(overrides)
    return payload


def all_canaries() -> set[str]:
    """Every canary string that appears anywhere in the examples."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.startswith(CANARY):
                found.add(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(EXAMPLES)
    return found
