"""The scoring engine, exercised once per component type.

Fixtures come from :mod:`tests.component_examples` (a canary in every hidden
field) and :mod:`tests.served_responses` (a response built independently from
the visible content, the way a learner's client would). Reusing them here
means a scoring bug and a validation bug cannot both hide behind hand-tuned
fixtures that happen to agree with each other.
"""

from __future__ import annotations

import pytest

from learning_studio.components import COMPONENT_TYPES, build_component
from learning_studio.evaluation import (
    INITIAL_REVIEW_STATE,
    REMEMBERED_QUALITY,
    ComponentResult,
    EvaluationError,
    ReviewState,
    component_quality,
    objective_quality,
    score_component,
    sm2_update,
)
from tests.component_examples import CANARY, example
from tests.served_responses import response_for


def _hidden(component_type: str, **overrides):
    component = build_component(example(component_type, **overrides), "component")
    return component, component.hidden()


def _score(component_type: str, **overrides):
    component, hidden = _hidden(component_type, **overrides)
    response = response_for(component_type, component.content)
    return score_component(component_type=component_type, hidden=hidden, response=response)


# ── Every type produces a result, and never leaks ──────────────────────────


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_every_component_type_is_scored(component_type: str) -> None:
    result = _score(component_type)
    assert isinstance(result, ComponentResult)
    assert result.component_type == component_type
    # graded implies a numeric mark; not graded implies no numeric mark.
    if result.graded:
        assert result.correct is None or isinstance(result.correct, bool)
    else:
        assert result.score is None
        assert result.correct is None


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_no_hidden_field_other_than_feedback_leaves_the_result(component_type: str) -> None:
    """Only the matched half of ``feedback`` may ever appear on a result.

    Every other hidden string in the fixtures — hints, notes, rubric
    criteria and descriptors, per-option text for options nobody chose, an
    answer key, a mnemonic, a reference solution — carries a canary that must
    never equal, or be embedded default-empty in, anything this module
    returns.
    """
    component, hidden = _hidden(component_type)
    response = response_for(component_type, component.content)
    result = score_component(component_type=component_type, hidden=hidden, response=response)

    allowed = {
        None,
        CANARY + f"-{component_type}-feedback-correct",
        CANARY + f"-{component_type}-feedback-incorrect",
    }
    if component_type == "multiple_choice":
        allowed.add(CANARY + "-multiple_choice-per-option")
    assert result.feedback in allowed

    # Self-report never carries the learner's own free text: only a closed
    # vocabulary (self_rating) or a bounded integer (rating).
    if result.self_report is not None:
        for value in result.self_report.values():
            assert not isinstance(value, str) or value in {"again", "hard", "good", "easy"}


# ── Per-mode behaviour ──────────────────────────────────────────────────


def test_exact_single_choice_correct_and_incorrect() -> None:
    result = _score("multiple_choice")
    assert result.graded is True
    assert result.correct is True
    assert result.score == result.max_score == 1.0
    assert result.feedback == CANARY + "-multiple_choice-feedback-correct"

    wrong = _score("multiple_choice")
    component, hidden = _hidden("multiple_choice")
    wrong = score_component(
        component_type="multiple_choice", hidden=hidden, response={"option_id": "cytosol"}
    )
    assert wrong.correct is False
    assert wrong.score == 0.0
    # The per-option branch wins over the generic incorrect text.
    assert wrong.feedback == CANARY + "-multiple_choice-per-option"


def test_true_false_exact() -> None:
    result = _score("true_false")
    assert result.correct is True
    component, hidden = _hidden("true_false")
    wrong = score_component(component_type="true_false", hidden=hidden, response={"value": False})
    assert wrong.correct is False


def test_normalised_text_answer_is_case_insensitive_but_wrong_word_fails() -> None:
    component, hidden = _hidden("short_answer")
    right = score_component(
        component_type="short_answer", hidden=hidden, response={"text": "ACETYL-COA"}
    )
    assert right.correct is True
    wrong = score_component(component_type="short_answer", hidden=hidden, response={"text": "word"})
    assert wrong.correct is False


def test_translation_accent_insensitive_flag_is_honoured() -> None:
    component, hidden = _hidden("translation")
    accented = score_component(
        component_type="translation",
        hidden=hidden,
        response={"text": "I still do not know whéther I will manage to arrive in time."},
    )
    assert accented.correct is True  # accent_sensitive is False in the fixture


def test_numeric_mode_uses_tolerance() -> None:
    raw = example(
        "short_answer",
        answer={"accepted": ["9.8"], "case_sensitive": False},
        evaluation={"scoring": {"mode": "numeric", "tolerance": 0.2}},
    )
    component = build_component(raw, "component")
    hidden = component.hidden()

    close = score_component(component_type="short_answer", hidden=hidden, response={"text": "9.9"})
    assert close.correct is True

    far = score_component(component_type="short_answer", hidden=hidden, response={"text": "12"})
    assert far.correct is False


def test_set_mode_partial_credit_multi_select() -> None:
    result = _score("multi_select")  # served response picks only one of two
    assert result.correct is False
    assert result.score == pytest.approx(0.5)


def test_set_mode_all_or_nothing_without_partial_credit() -> None:
    result = _score("categorization")
    assert result.correct is False
    assert result.score == 0.0


def test_set_mode_matching_partial_credit() -> None:
    result = _score("matching")
    assert result.correct is False
    assert result.score == pytest.approx(0.5)


def test_ordered_mode_position_match_fraction() -> None:
    # sequence_order's served response reverses a 3-item list; the middle
    # item lands back in its own position.
    result = _score("sequence_order")
    assert result.correct is False
    assert result.score == 0.0  # no partial_credit flag on this fixture


def test_ordered_mode_decision_path_is_keyed_by_step_not_position() -> None:
    result = _score("decision_path")
    assert result.graded is True
    assert result.correct is True


def test_hotspot_inside_and_outside_tolerance() -> None:
    component, hidden = _hidden("hotspot")
    inside = score_component(
        component_type="hotspot", hidden=hidden, response={"points": [{"x": 0.46, "y": 0.55}]}
    )
    assert inside.correct is True
    just_outside = score_component(
        component_type="hotspot", hidden=hidden, response={"points": [{"x": 0.9, "y": 0.9}]}
    )
    assert just_outside.correct is False
    # Within the declared tolerance of the rectangle's edge.
    edge = score_component(
        component_type="hotspot", hidden=hidden, response={"points": [{"x": 0.415, "y": 0.55}]}
    )
    assert edge.correct is True


def test_table_grid_partial_credit() -> None:
    result = _score("table_grid")
    assert result.correct is False
    assert result.score == 0.0  # neither served cell text matches


def test_error_correction_deterministic_phrase_check() -> None:
    component, hidden = _hidden("error_correction")
    right = score_component(
        component_type="error_correction",
        hidden=hidden,
        response={
            "text": "The committee have published their findings, which were widely reported."
        },
    )
    assert right.correct is True
    wrong = score_component(
        component_type="error_correction", hidden=hidden, response={"text": "word"}
    )
    assert wrong.correct is False


def test_code_response_must_include_must_not_include() -> None:
    raw = example(
        "code_response",
        answer={
            "reference_solution": CANARY + "-code_response-solution",
            "must_include": ["MEDIAN_MARKER"],
            "must_not_include": ["FORBIDDEN_MARKER"],
        },
        evaluation={"scoring": {"mode": "exact"}},
    )
    component = build_component(raw, "component")
    hidden = component.hidden()

    right = score_component(
        component_type="code_response",
        hidden=hidden,
        response={"code": "def f():\n    return MEDIAN_MARKER(values)"},
    )
    assert right.correct is True

    wrong = score_component(
        component_type="code_response", hidden=hidden, response={"code": "def f(): pass"}
    )
    assert wrong.correct is False

    forbidden = score_component(
        component_type="code_response",
        hidden=hidden,
        response={"code": "FORBIDDEN_MARKER\nreturn MEDIAN_MARKER(values)"},
    )
    assert forbidden.correct is False
    assert forbidden.score == 0.0


# ── rubric ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "component_type",
    ["free_response", "image_observation", "case_study", "self_explanation", "rubric_response"],
)
def test_rubric_without_levels_is_recorded_but_ungraded(component_type: str) -> None:
    result = _score(component_type)
    assert result.graded is False
    assert result.correct is None
    assert result.score is None
    assert result.max_score == 3  # the fixture's top rubric level is worth 3


def test_rubric_with_levels_aggregates_deterministically() -> None:
    component, hidden = _hidden("free_response")
    criterion = hidden["evaluation"]["rubric"][0]["criterion"]
    result = score_component(
        component_type="free_response",
        hidden=hidden,
        response={"text": "irrelevant, never inspected"},
        rubric_levels={criterion: "Secure"},
    )
    assert result.graded is True
    assert result.correct is None  # rubric marks are a score, not a verdict
    assert result.score == 3
    assert result.max_score == 3


def test_code_response_rubric_mode_is_ungraded_without_levels() -> None:
    result = _score("code_response")
    # The fixture's scoring.mode is "rubric" even though the type also
    # supports exact/normalised, so the rubric path applies.
    assert result.graded is False


# ── self_check ─────────────────────────────────────────────────────────


def test_flashcard_is_never_machine_graded() -> None:
    result = _score("flashcard")
    assert result.graded is False
    assert result.correct is None
    assert result.self_report == {"self_rating": "good"}


def test_confidence_rating_is_never_machine_graded() -> None:
    result = _score("confidence_rating")
    assert result.graded is False
    assert result.self_report == {"rating": 1}


def test_reflection_records_no_self_report_payload() -> None:
    result = _score("reflection")
    assert result.graded is False
    assert result.self_report is None


def test_missing_scoring_mode_raises() -> None:
    with pytest.raises(EvaluationError):
        score_component(
            component_type="multiple_choice",
            hidden={"answer": {"option_id": "matrix"}, "evaluation": {}},
            response={"option_id": "matrix"},
        )


# ── Spaced repetition ────────────────────────────────────────────────────


def test_sm2_low_quality_resets_repetitions_and_interval() -> None:
    state = ReviewState(interval_days=30, ease_factor=2.5, repetitions=5)
    updated = sm2_update(quality=1, state=state)
    assert updated.repetitions == 0
    assert updated.interval_days == 1
    assert updated.ease_factor < state.ease_factor


def test_sm2_progresses_intervals_on_repeated_success() -> None:
    state = INITIAL_REVIEW_STATE
    state = sm2_update(quality=5, state=state)
    assert state.interval_days == 1
    assert state.repetitions == 1
    state = sm2_update(quality=5, state=state)
    assert state.interval_days == 6
    assert state.repetitions == 2
    state = sm2_update(quality=5, state=state)
    assert state.interval_days > 6
    assert state.repetitions == 3


def test_sm2_ease_factor_has_a_floor() -> None:
    state = ReviewState(interval_days=1, ease_factor=1.3, repetitions=0)
    for _ in range(10):
        state = sm2_update(quality=0, state=state)
    assert state.ease_factor >= 1.3


def test_component_quality_from_self_rating_and_from_score_fraction() -> None:
    flashcard = ComponentResult(
        component_type="flashcard",
        mode="self_check",
        graded=False,
        correct=None,
        score=None,
        max_score=None,
        self_report={"self_rating": "good"},
    )
    assert component_quality(flashcard) == 4

    graded = ComponentResult(
        component_type="multiple_choice",
        mode="exact",
        graded=True,
        correct=True,
        score=1.0,
        max_score=1.0,
    )
    assert component_quality(graded) == 5

    ungraded = ComponentResult(
        component_type="reflection",
        mode="self_check",
        graded=False,
        correct=None,
        score=None,
        max_score=None,
    )
    assert component_quality(ungraded) is None


def test_objective_quality_averages_and_ignores_ungraded() -> None:
    results = [
        ComponentResult("a", "exact", True, True, 1.0, 1.0),
        ComponentResult("b", "exact", True, False, 0.0, 1.0),
        ComponentResult("c", "self_check", False, None, None, None),
    ]
    quality = objective_quality(results)
    assert quality == round((5 + 0) / 2)


def test_objective_quality_with_nothing_gradable_is_none() -> None:
    results = [ComponentResult("a", "self_check", False, None, None, None)]
    assert objective_quality(results) is None


def test_remembered_quality_threshold_is_three() -> None:
    assert REMEMBERED_QUALITY == 3
