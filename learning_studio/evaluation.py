"""The scoring engine: pure functions that turn a stored answer and a
submitted response into a mark, for every one of the 31 component types.

Nothing here touches storage, the network, or Hermes. Every function takes
data and returns data, which is what makes the exhaustive per-type tests in
``tests/test_evaluation.py`` possible and is also the security property that
matters most: a pure function cannot leak through a side channel it does not
have. The one channel it does have — its return value — is disciplined on
purpose. :class:`ComponentResult` never carries the stored answer, the
rubric, or the learner's own text back out; it carries a mark and, where the
manifest author wrote one, the feedback string they meant to be shown *after*
an attempt. That string is the same ``evaluation.feedback`` block
:mod:`learning_studio.components` already keeps out of the learner payload
until now — this module is the "until now".

Dispatch is on the *stored* ``evaluation.scoring.mode``, not on the component
type. The type decides which modes are legal (``ComponentSpec.scoring_modes``,
enforced when the manifest was built), and the author picked one of them when
they wrote the exercise; the mode the author picked is what this module
trusts, because it is what a human or agent designing the exercise decided
"scored" means for this particular card.

What "graded" means here
-------------------------

Three of the seven modes cannot mark anything, by design, not by omission:

- ``self_check`` is a self-report (flashcard, confidence rating, reflection).
  Grading how sure someone says they feel, or overriding their own read of
  their recall, is not this plugin's place. ``graded`` is ``False``.
- ``rubric`` needs criterion-by-criterion judgement this module cannot invent
  from free text. When a caller supplies ``rubric_levels`` — the levels an
  evaluator actually selected — :func:`_score_rubric` aggregates them
  deterministically. Without that input the attempt is recorded as made and
  ``graded`` stays ``False`` rather than fabricating a mark. See
  ``docs/`` or the PR description for why an LLM-graded pass is future work,
  not silently invented here.

Everything else is graded: a bool or a fraction comes out, always.

Documented measures
--------------------

Where a mode admits more than one reasonable algorithm, the choice is written
down beside the function that makes it, because the choice is the answer to
"why did this get partial credit":

- **set** (multi_select, classification, matching, categorization, labeling):
  matched-minus-false-positive, divided by the number of expected entries,
  floored at zero. Exact when the type's own answer block has no
  ``partial_credit`` flag or it is unset.
- **table_grid** (``exact`` or ``normalised``, never ``set``): scored per
  cell against that cell's own accepted forms, not as a group — a table is a
  grid of independent text answers, not an unordered collection, so the set
  algorithm's "matched minus false positive" has nothing to subtract against.
  ``partial_credit`` is the fraction of cells matched; it is read from the
  answer block, the same as the other set-family types.
- **ordered** (sentence_order, sequence_order, timeline, process_flow):
  fraction of positions where the submitted item equals the expected item at
  that same index — "correctly placed", not edit distance. With
  ``partial_credit``, the fraction is taken over
  ``max(len(expected), len(submitted))``, not just ``len(expected)``, so
  padding the submission with extra items cannot buy back credit lost to
  wrong positions.
- **ordered** (decision_path): the response contract for this type is a set
  of ``{step_id, option_id}`` pairs, not a sequence — see
  ``responses._decision_path`` — so grading here is per-step option
  correctness, keyed by step, and averaged. Unaffected by the length penalty
  above: a step either has a submitted decision or it does not.
- **hotspot**: a submitted point counts inside a region when it falls within
  the region's own shape expanded by ``tolerance`` on every side. Polygon
  regions are checked *without* the tolerance expansion: growing an arbitrary
  polygon by a scalar margin needs a straight-skeleton offset this module does
  not implement, so a polygon's tolerance is accepted and stored but not
  applied. Rectangles and circles use it exactly.
- **error_correction**: the response contract collects one corrected passage,
  not a list of fixes, so grading checks that each declared correction's
  ``correct`` phrase appears in the submission and its ``incorrect`` phrase
  does not, both compared case- and accent-insensitively (this type declares
  neither flag, so the loosest reasonable comparison is the one applied).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

#: Component types whose scoring mode can never produce a machine mark.
SELF_CHECK_TYPES: frozenset[str] = frozenset({"flashcard", "confidence_rating", "reflection"})

#: Rubric-scored types with no answer key at all (``requires_rubric=True``).
RUBRIC_ONLY_TYPES: frozenset[str] = frozenset(
    {
        "free_response",
        "image_observation",
        "case_study",
        "self_explanation",
        "rubric_response",
    }
)

#: A comma used as a thousands separator: one to three leading digits, then
#: one or more groups of exactly three digits, with no stray digit glued to
#: either end of the run — see :func:`_parse_number`.
_THOUSANDS_RE = re.compile(r"(?<!\d)-?\d{1,3}(?:,\d{3})+(?!\d)(?:\.\d+)?")
#: A comma used as a decimal point (the French/European convention).
_DECIMAL_COMMA_RE = re.compile(r"-?\d+,\d+")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ComponentResult:
    """The mark for one submitted response, and nothing that produced it.

    ``score``/``max_score`` are ``None`` exactly when ``graded`` is ``False``:
    a self-report or an un-leveled rubric response has nothing to divide.
    ``feedback`` is the one piece of evaluator-only text this module is
    allowed to surface, and only the branch that matches the outcome — never
    both, never the rubric, never the answer key.
    """

    component_type: str
    mode: str
    graded: bool
    correct: bool | None
    score: float | None
    max_score: float | None
    feedback: str | None = None
    #: What a self-report recorded, e.g. ``{"self_rating": "good"}`` or
    #: ``{"rating": 7}``. Never the learner's free text — see the module
    #: docstring on why this engine does not carry that back out.
    self_report: dict[str, Any] | None = None

    @property
    def fraction(self) -> float | None:
        """``score / max_score``, or ``None`` when there is nothing to divide."""
        if self.score is None or self.max_score is None or self.max_score <= 0:
            return None
        return max(0.0, min(1.0, self.score / self.max_score))


class EvaluationError(ValueError):
    """The stored evaluation record could not be scored as it stands.

    Raised only for internal inconsistency (a missing answer block for a type
    that requires one) — never for anything about the *learner's* response,
    which is validated long before scoring runs.
    """


# ── Text comparison ──────────────────────────────────────────────────────


def _fold(text: str, *, case_sensitive: bool, accent_sensitive: bool) -> str:
    value = unicodedata.normalize("NFKC", text)
    if not accent_sensitive:
        value = "".join(
            char for char in unicodedata.normalize("NFD", value) if not unicodedata.combining(char)
        )
        value = unicodedata.normalize("NFC", value)
    if not case_sensitive:
        value = value.casefold()
    return value


def _prepare(text: str, *, mode: str, case_sensitive: bool, accent_sensitive: bool) -> str:
    folded = _fold(text, case_sensitive=case_sensitive, accent_sensitive=accent_sensitive)
    if mode == "normalised":
        return " ".join(folded.split())
    return folded.strip()


def _text_matches(
    submitted: str,
    accepted: list[str],
    *,
    mode: str,
    case_sensitive: bool = False,
    accent_sensitive: bool = False,
    tolerance: float = 0.0,
) -> bool:
    if mode == "numeric":
        return _numeric_matches(submitted, accepted, tolerance=tolerance)
    left = _prepare(
        submitted, mode=mode, case_sensitive=case_sensitive, accent_sensitive=accent_sensitive
    )
    return any(
        left
        == _prepare(
            candidate, mode=mode, case_sensitive=case_sensitive, accent_sensitive=accent_sensitive
        )
        for candidate in accepted
    )


def _parse_number(text: str) -> float | None:
    """Find the first number in ``text``, disambiguating a comma by context.

    A comma is a **thousands separator** only when it sits in a run that
    reads as digit-grouping: one to three leading digits, then one or more
    groups of exactly three digits, with nothing before or after that would
    make a group not-exactly-three (``1,234`` and ``1,234,567`` qualify;
    ``1,2345`` and ``12,3`` do not, because a group is the wrong size). Such a
    comma is dropped. Anything else with a comma between two digit runs is a
    **decimal comma** (the French/European convention) and is read as ``.``
    instead — so ``1,5`` is one-and-a-half, not fifteen. A plain ``.`` decimal
    point still works unchanged.
    """
    match = _THOUSANDS_RE.search(text)
    if match is not None:
        return float(match.group(0).replace(",", ""))
    match = _DECIMAL_COMMA_RE.search(text)
    if match is not None:
        return float(match.group(0).replace(",", "."))
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:  # pragma: no cover - regex already restricts the form
        return None


def _numeric_matches(submitted: str, accepted: list[str], *, tolerance: float) -> bool:
    value = _parse_number(submitted)
    if value is None:
        return False
    for candidate in accepted:
        target = _parse_number(candidate)
        if target is not None and abs(value - target) <= max(0.0, tolerance):
            return True
    return False


# ── Weight and feedback, shared by every graded mode ─────────────────────


def _weight(scoring: dict[str, Any]) -> float:
    points = scoring.get("points")
    return float(points) if isinstance(points, int | float) and points > 0 else 1.0


def _feedback_for(
    evaluation: dict[str, Any], *, correct: bool | None, option_id: str | None = None
) -> str | None:
    block = evaluation.get("feedback")
    if not isinstance(block, dict):
        return None
    if option_id is not None:
        for entry in block.get("per_option") or ():
            if isinstance(entry, dict) and entry.get("option_id") == option_id:
                text = entry.get("text")
                if isinstance(text, str) and text:
                    return text
    if correct is None:
        return None
    key = "correct" if correct else "incorrect"
    text = block.get(key)
    return text if isinstance(text, str) and text else None


def _ungraded(component_type: str, mode: str, *, max_score: float | None) -> ComponentResult:
    return ComponentResult(
        component_type=component_type,
        mode=mode,
        graded=False,
        correct=None,
        score=None,
        max_score=max_score,
    )


# ── exact / normalised / numeric ──────────────────────────────────────────


def _score_single_choice(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    expected = answer.get("option_id")
    submitted = response.get("option_id")
    correct = submitted is not None and submitted == expected
    weight = _weight(evaluation.get("scoring") or {})
    return ComponentResult(
        component_type=component_type,
        mode=mode,
        graded=True,
        correct=correct,
        score=weight if correct else 0.0,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct, option_id=submitted),
    )


def _score_true_false(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    correct = response.get("value") == answer.get("value")
    weight = _weight(evaluation.get("scoring") or {})
    return ComponentResult(
        component_type=component_type,
        mode=mode,
        graded=True,
        correct=correct,
        score=weight if correct else 0.0,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_text_answer(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    accepted = [str(item) for item in answer.get("accepted") or ()]
    submitted = str(response.get("text", ""))
    scoring = evaluation.get("scoring") or {}
    correct = _text_matches(
        submitted,
        accepted,
        mode=mode,
        case_sensitive=bool(answer.get("case_sensitive")),
        accent_sensitive=bool(answer.get("accent_sensitive")),
        tolerance=float(scoring.get("tolerance") or 0.0),
    )
    weight = _weight(scoring)
    return ComponentResult(
        component_type=component_type,
        mode=mode,
        graded=True,
        correct=correct,
        score=weight if correct else 0.0,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_fill_blank(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    case_sensitive = bool(answer.get("case_sensitive"))
    accent_sensitive = bool(answer.get("accent_sensitive"))
    by_id = {
        str(entry.get("blank_id")): [str(item) for item in entry.get("accepted") or ()]
        for entry in answer.get("blanks") or ()
        if isinstance(entry, dict)
    }
    submitted = {
        str(entry.get("blank_id")): str(entry.get("text", ""))
        for entry in response.get("blanks") or ()
        if isinstance(entry, dict)
    }
    scoring = evaluation.get("scoring") or {}
    tolerance = float(scoring.get("tolerance") or 0.0)
    total = len(by_id) or 1
    hits = sum(
        1
        for blank_id, accepted in by_id.items()
        if _text_matches(
            submitted.get(blank_id, ""),
            accepted,
            mode=mode,
            case_sensitive=case_sensitive,
            accent_sensitive=accent_sensitive,
            tolerance=tolerance,
        )
    )
    weight = _weight(scoring)
    partial = bool(scoring.get("partial_credit"))
    fraction = hits / total
    correct = hits == total
    return ComponentResult(
        component_type=component_type,
        mode=mode,
        graded=True,
        correct=correct,
        score=(fraction if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_error_correction(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    submitted = _prepare(
        str(response.get("text", "")),
        mode="normalised",
        case_sensitive=False,
        accent_sensitive=False,
    )
    corrections = [item for item in answer.get("corrections") or () if isinstance(item, dict)]
    total = len(corrections) or 1
    hits = 0
    for item in corrections:
        fixed = _prepare(
            str(item.get("correct", "")),
            mode="normalised",
            case_sensitive=False,
            accent_sensitive=False,
        )
        broken = _prepare(
            str(item.get("incorrect", "")),
            mode="normalised",
            case_sensitive=False,
            accent_sensitive=False,
        )
        if fixed and fixed in submitted and not (broken and broken in submitted):
            hits += 1
    scoring = evaluation.get("scoring") or {}
    weight = _weight(scoring)
    partial = bool(scoring.get("partial_credit"))
    fraction = hits / total
    correct = hits == total
    return ComponentResult(
        component_type=component_type,
        mode="normalised",
        graded=True,
        correct=correct,
        score=(fraction if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_table_grid(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    case_sensitive = bool(answer.get("case_sensitive"))
    accent_sensitive = bool(answer.get("accent_sensitive", True))
    by_key = {
        (str(entry.get("row_id")), str(entry.get("column_id"))): [
            str(item) for item in entry.get("accepted") or ()
        ]
        for entry in answer.get("cells") or ()
        if isinstance(entry, dict)
    }
    submitted = {
        (str(entry.get("row_id")), str(entry.get("column_id"))): str(entry.get("text", ""))
        for entry in response.get("cells") or ()
        if isinstance(entry, dict)
    }
    compare_mode = "normalised" if mode != "exact" else "exact"
    total = len(by_key) or 1
    hits = sum(
        1
        for key, accepted in by_key.items()
        if _text_matches(
            submitted.get(key, ""),
            accepted,
            mode=compare_mode,
            case_sensitive=case_sensitive,
            accent_sensitive=accent_sensitive,
        )
    )
    partial = bool(answer.get("partial_credit"))
    weight = _weight(evaluation.get("scoring") or {})
    fraction = hits / total
    correct = hits == total
    return ComponentResult(
        component_type=component_type,
        mode=mode,
        graded=True,
        correct=correct,
        score=(fraction if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_code_response(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    submitted = str(response.get("code", ""))
    must_include = [str(item) for item in answer.get("must_include") or ()]
    must_not_include = [str(item) for item in answer.get("must_not_include") or ()]
    weight = _weight(evaluation.get("scoring") or {})
    if must_include or must_not_include:
        present = [phrase for phrase in must_include if phrase in submitted]
        forbidden_hit = any(phrase in submitted for phrase in must_not_include)
        total = len(must_include) or 1
        fraction = (len(present) / total) if must_include else 1.0
        correct = len(present) == len(must_include) and not forbidden_hit
        score = 0.0 if forbidden_hit else fraction
    else:
        reference = str(answer.get("reference_solution", ""))
        correct = _text_matches(
            submitted, [reference], mode=mode, case_sensitive=True, accent_sensitive=True
        )
        score = float(correct)
    return ComponentResult(
        component_type=component_type,
        mode=mode,
        graded=True,
        correct=correct,
        score=score * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


# ── set ────────────────────────────────────────────────────────────────


def _score_multi_select(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    expected = {str(item) for item in answer.get("option_ids") or ()}
    submitted = {str(item) for item in response.get("option_ids") or ()}
    weight = _weight(evaluation.get("scoring") or {})
    correct = submitted == expected
    if bool(answer.get("partial_credit")) and expected:
        matches = len(submitted & expected)
        extra = len(submitted - expected)
        fraction = max(0.0, matches - extra) / len(expected)
    else:
        fraction = float(correct)
    return ComponentResult(
        component_type=component_type,
        mode="set",
        graded=True,
        correct=correct,
        score=fraction * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_classification(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    expected = {
        str(item.get("item_id")): str(item.get("category_id"))
        for item in answer.get("assignments") or ()
        if isinstance(item, dict)
    }
    submitted = {
        str(item.get("item_id")): str(item.get("category_id"))
        for item in response.get("assignments") or ()
        if isinstance(item, dict)
    }
    total = len(expected) or 1
    hits = sum(1 for item_id, category in expected.items() if submitted.get(item_id) == category)
    correct = hits == total
    partial = bool(answer.get("partial_credit"))
    weight = _weight(evaluation.get("scoring") or {})
    return ComponentResult(
        component_type=component_type,
        mode="set",
        graded=True,
        correct=correct,
        score=(hits / total if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_categorization(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    expected = {
        str(item.get("item_id")): {str(cat) for cat in item.get("category_ids") or ()}
        for item in answer.get("assignments") or ()
        if isinstance(item, dict)
    }
    submitted = {
        str(item.get("item_id")): {str(cat) for cat in item.get("category_ids") or ()}
        for item in response.get("assignments") or ()
        if isinstance(item, dict)
    }
    total = len(expected) or 1
    hits = sum(
        1 for item_id, categories in expected.items() if submitted.get(item_id) == categories
    )
    correct = hits == total
    partial = bool(answer.get("partial_credit"))
    weight = _weight(evaluation.get("scoring") or {})
    return ComponentResult(
        component_type=component_type,
        mode="set",
        graded=True,
        correct=correct,
        score=(hits / total if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_matching(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    expected = {
        str(item.get("left_id")): str(item.get("right_id"))
        for item in answer.get("pairs") or ()
        if isinstance(item, dict)
    }
    submitted = {
        str(item.get("left_id")): str(item.get("right_id"))
        for item in response.get("pairs") or ()
        if isinstance(item, dict)
    }
    total = len(expected) or 1
    hits = sum(1 for left, right in expected.items() if submitted.get(left) == right)
    correct = hits == total
    partial = bool(answer.get("partial_credit"))
    weight = _weight(evaluation.get("scoring") or {})
    return ComponentResult(
        component_type=component_type,
        mode="set",
        graded=True,
        correct=correct,
        score=(hits / total if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_labeling(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    expected = {
        str(item.get("marker_id")): str(item.get("label_id"))
        for item in answer.get("labels") or ()
        if isinstance(item, dict)
    }
    submitted = {
        str(item.get("marker_id")): str(item.get("label_id"))
        for item in response.get("labels") or ()
        if isinstance(item, dict)
    }
    total = len(expected) or 1
    hits = sum(1 for marker, label in expected.items() if submitted.get(marker) == label)
    correct = hits == total
    scoring = evaluation.get("scoring") or {}
    partial = bool(scoring.get("partial_credit"))
    weight = _weight(scoring)
    return ComponentResult(
        component_type=component_type,
        mode="set",
        graded=True,
        correct=correct,
        score=(hits / total if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


# ── ordered ────────────────────────────────────────────────────────────


def _score_ordering(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    expected = [str(item) for item in answer.get("order") or ()]
    submitted = [str(item) for item in response.get("order") or ()]
    total = len(expected) or 1
    hits = sum(
        1
        for index, item in enumerate(expected)
        if index < len(submitted) and submitted[index] == item
    )
    correct = hits == total and len(submitted) == len(expected)
    scoring = evaluation.get("scoring") or {}
    partial = bool(scoring.get("partial_credit"))
    weight = _weight(scoring)
    # A submission padded with extra items cannot match more positions than
    # ``expected`` has, so dividing by ``total`` alone would let a learner buy
    # back credit lost to wrong positions just by tacking junk onto the end.
    # Dividing by the longer of the two lengths makes a padded submission
    # strictly worse, never better, than the same submission unpadded.
    denominator = max(len(expected), len(submitted)) or 1
    return ComponentResult(
        component_type=component_type,
        mode="ordered",
        graded=True,
        correct=correct,
        score=(hits / denominator if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


def _score_decision_path(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    expected = {
        str(item.get("step_id")): str(item.get("option_id"))
        for item in answer.get("decisions") or ()
        if isinstance(item, dict)
    }
    submitted = {
        str(item.get("step_id")): str(item.get("option_id"))
        for item in response.get("decisions") or ()
        if isinstance(item, dict)
    }
    total = len(expected) or 1
    hits = sum(1 for step, option in expected.items() if submitted.get(step) == option)
    correct = hits == total
    scoring = evaluation.get("scoring") or {}
    partial = bool(scoring.get("partial_credit"))
    weight = _weight(scoring)
    return ComponentResult(
        component_type=component_type,
        mode="ordered",
        graded=True,
        correct=correct,
        score=(hits / total if partial else float(correct)) * weight,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


# ── hotspot (its own geometry) ────────────────────────────────────────────


def _point_in_rectangle(x: float, y: float, points: list[float], tolerance: float) -> bool:
    if len(points) < 4:
        return False
    x1, y1, x2, y2 = points[0], points[1], points[2], points[3]
    xmin, xmax = sorted((x1, x2))
    ymin, ymax = sorted((y1, y2))
    return (xmin - tolerance) <= x <= (xmax + tolerance) and (ymin - tolerance) <= y <= (
        ymax + tolerance
    )


def _point_in_circle(x: float, y: float, points: list[float], tolerance: float) -> bool:
    if len(points) < 3:
        return False
    cx, cy, radius = points[0], points[1], points[2]
    return (x - cx) ** 2 + (y - cy) ** 2 <= (radius + max(0.0, tolerance)) ** 2


def _point_in_polygon(x: float, y: float, points: list[float]) -> bool:
    if len(points) < 6:
        return False
    vertices = list(zip(points[0::2], points[1::2], strict=False))
    inside = False
    j = len(vertices) - 1
    for i, (xi, yi) in enumerate(vertices):
        xj, yj = vertices[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_region(x: float, y: float, region: dict[str, Any], tolerance: float) -> bool:
    shape = region.get("shape")
    points = [float(value) for value in region.get("points") or ()]
    if shape == "rectangle":
        return _point_in_rectangle(x, y, points, tolerance)
    if shape == "circle":
        return _point_in_circle(x, y, points, tolerance)
    if shape == "polygon":
        return _point_in_polygon(x, y, points)
    return False


def _score_hotspot(
    component_type: str,
    mode: str,
    answer: dict[str, Any],
    evaluation: dict[str, Any],
    response: dict[str, Any],
) -> ComponentResult:
    del mode
    tolerance = float(answer.get("tolerance") or 0.0)
    submitted_points = response.get("points") or ()
    correct = False
    if submitted_points and isinstance(submitted_points[0], dict):
        point = submitted_points[0]
        x, y = float(point.get("x", -1)), float(point.get("y", -1))
        correct = any(
            _point_in_region(x, y, region, tolerance)
            for region in answer.get("regions") or ()
            if isinstance(region, dict)
        )
    weight = _weight(evaluation.get("scoring") or {})
    return ComponentResult(
        component_type=component_type,
        mode="exact",
        graded=True,
        correct=correct,
        score=weight if correct else 0.0,
        max_score=weight,
        feedback=_feedback_for(evaluation, correct=correct),
    )


# ── rubric ─────────────────────────────────────────────────────────────


def _rubric_ceiling(rubric: list[Any]) -> float:
    total = 0.0
    for criterion in rubric:
        if not isinstance(criterion, dict):
            continue
        levels = [level for level in criterion.get("levels") or () if isinstance(level, dict)]
        if levels:
            total += max(float(level.get("points", 0)) for level in levels)
    return total


def _score_rubric(
    component_type: str,
    mode: str,
    evaluation: dict[str, Any],
    rubric_levels: dict[str, str] | None,
) -> ComponentResult:
    rubric = [item for item in evaluation.get("rubric") or () if isinstance(item, dict)]
    ceiling = _rubric_ceiling(rubric) or None
    if not rubric_levels:
        return _ungraded(component_type, mode, max_score=ceiling)

    total = 0.0
    max_total = 0.0
    for criterion in rubric:
        name = str(criterion.get("criterion", ""))
        levels = {str(level.get("label")): level for level in criterion.get("levels") or ()}
        if not levels:
            continue
        max_total += max(float(level.get("points", 0)) for level in levels.values())
        chosen_label = rubric_levels.get(name)
        chosen = levels.get(str(chosen_label)) if chosen_label is not None else None
        if chosen is not None:
            total += float(chosen.get("points", 0))
    if max_total <= 0:
        return _ungraded(component_type, mode, max_score=ceiling)
    return ComponentResult(
        component_type=component_type,
        mode="rubric",
        graded=True,
        correct=None,
        score=total,
        max_score=max_total,
        feedback=None,
    )


# ── self_check ─────────────────────────────────────────────────────────

#: SM-2 quality (0-5) a flashcard's self-rating stands for. ``good``/``easy``
#: both clear the "remembered" threshold of 3 that :func:`sm2_update` uses to
#: decide whether the interval grows or resets.
SELF_RATING_QUALITY: dict[str, int] = {"again": 0, "hard": 3, "good": 4, "easy": 5}


def _score_self_check(
    component_type: str, evaluation: dict[str, Any], response: dict[str, Any]
) -> ComponentResult:
    self_report: dict[str, Any] | None = None
    if component_type == "flashcard":
        rating = response.get("self_rating")
        if isinstance(rating, str):
            self_report = {"self_rating": rating}
    elif component_type == "confidence_rating":
        rating = response.get("rating")
        if isinstance(rating, int):
            self_report = {"rating": rating}
    return ComponentResult(
        component_type=component_type,
        mode="self_check",
        graded=False,
        correct=None,
        score=None,
        max_score=None,
        feedback=None,
        self_report=self_report,
    )


# ── dispatch ───────────────────────────────────────────────────────────

_SINGLE_CHOICE_TYPES = frozenset({"multiple_choice", "image_choice", "scenario_choice"})
_TEXT_ANSWER_TYPES = frozenset({"short_answer", "translation", "typed_recall", "diagram"})


def score_component(
    *,
    component_type: str,
    hidden: dict[str, Any],
    response: dict[str, Any],
    rubric_levels: dict[str, str] | None = None,
) -> ComponentResult:
    """Score one submitted, canonical-identifier response.

    ``hidden`` is exactly what is stored in
    ``experience_component_evaluations.evaluation`` for this component: the
    ``answer`` block (absent for rubric/self-report types) and the
    ``evaluation`` block (scoring parameters, rubric, feedback — never the
    learner's business). ``response`` is the canonical-terms value already
    validated and alias-resolved by
    :func:`learning_studio.responses.validate_component_response`.

    ``rubric_levels`` is the one place a human or agent judgement enters:
    ``{criterion_name: chosen_level_label}``, for a rubric-scored type that
    has since been reviewed. Without it, a rubric type is recorded as
    attempted but not graded — see the module docstring.
    """
    answer = hidden.get("answer") or {}
    evaluation = hidden.get("evaluation") or {}
    scoring = evaluation.get("scoring") or {}
    mode = scoring.get("mode")

    if component_type in SELF_CHECK_TYPES:
        return _score_self_check(component_type, evaluation, response)

    if mode == "rubric" or component_type in RUBRIC_ONLY_TYPES:
        result = _score_rubric(component_type, mode or "rubric", evaluation, rubric_levels)
        # code_response may declare rubric as one of *several* legal modes;
        # every other rubric-capable type requires it, so this only matters
        # for that one type and is a no-op everywhere else.
        return result

    if not isinstance(mode, str):
        raise EvaluationError(f"{component_type}: no scoring mode recorded")

    if component_type in _SINGLE_CHOICE_TYPES:
        return _score_single_choice(component_type, mode, answer, evaluation, response)
    if component_type == "true_false":
        return _score_true_false(component_type, mode, answer, evaluation, response)
    if component_type in _TEXT_ANSWER_TYPES:
        return _score_text_answer(component_type, mode, answer, evaluation, response)
    if component_type == "fill_blank":
        return _score_fill_blank(component_type, mode, answer, evaluation, response)
    if component_type == "error_correction":
        return _score_error_correction(component_type, mode, answer, evaluation, response)
    if component_type == "code_response":
        return _score_code_response(component_type, mode, answer, evaluation, response)
    if component_type == "multi_select":
        return _score_multi_select(component_type, mode, answer, evaluation, response)
    if component_type == "classification":
        return _score_classification(component_type, mode, answer, evaluation, response)
    if component_type == "categorization":
        return _score_categorization(component_type, mode, answer, evaluation, response)
    if component_type == "matching":
        return _score_matching(component_type, mode, answer, evaluation, response)
    if component_type == "labeling":
        return _score_labeling(component_type, mode, answer, evaluation, response)
    if component_type in {"sentence_order", "sequence_order", "timeline", "process_flow"}:
        return _score_ordering(component_type, mode, answer, evaluation, response)
    if component_type == "decision_path":
        return _score_decision_path(component_type, mode, answer, evaluation, response)
    if component_type == "table_grid":
        return _score_table_grid(component_type, mode, answer, evaluation, response)
    if component_type == "hotspot":
        return _score_hotspot(component_type, mode, answer, evaluation, response)

    raise EvaluationError(f"{component_type}: no scorer registered")


# ── Spaced repetition (SM-2) ──────────────────────────────────────────────
#
# A standard SM-2 implementation, chosen over a bespoke scheme because it is
# small, well understood, and needs nothing this plugin does not already have:
# one quality signal per review, no per-card difficulty model to train.
#
# Quality is derived, not asked for: a graded component's quality is its score
# fraction scaled to 0-5; a flashcard's is :data:`SELF_RATING_QUALITY`. An
# objective with several components in one attempt uses the mean of every
# component's quality that could produce one.

DEFAULT_EASE_FACTOR = 2.5
MINIMUM_EASE_FACTOR = 1.3
#: SM-2's own threshold: a quality below this resets the run of successful
#: reviews, because the learner did not actually recall it.
REMEMBERED_QUALITY = 3


@dataclass(frozen=True)
class ReviewState:
    interval_days: int
    ease_factor: float
    repetitions: int


def component_quality(result: ComponentResult) -> int | None:
    """This component's contribution to a review-quality signal, or ``None``."""
    if result.self_report is not None and "self_rating" in result.self_report:
        return SELF_RATING_QUALITY.get(str(result.self_report["self_rating"]))
    fraction = result.fraction
    if fraction is None:
        return None
    return round(fraction * 5)


def objective_quality(results: list[ComponentResult]) -> int | None:
    """The mean quality signal across every component that produced one."""
    values = [q for q in (component_quality(result) for result in results) if q is not None]
    if not values:
        return None
    return max(0, min(5, round(sum(values) / len(values))))


def sm2_update(*, quality: int, state: ReviewState) -> ReviewState:
    """Apply one SM-2 review step. ``quality`` is clamped to ``0..5``."""
    quality = max(0, min(5, quality))
    ease = state.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ease = max(MINIMUM_EASE_FACTOR, ease)

    if quality < REMEMBERED_QUALITY:
        return ReviewState(interval_days=1, ease_factor=ease, repetitions=0)

    repetitions = state.repetitions + 1
    if repetitions == 1:
        interval = 1
    elif repetitions == 2:
        interval = 6
    else:
        interval = max(1, round(state.interval_days * ease))
    return ReviewState(interval_days=interval, ease_factor=ease, repetitions=repetitions)


#: A learner who has never been reviewed on this objective before.
INITIAL_REVIEW_STATE = ReviewState(interval_days=1, ease_factor=DEFAULT_EASE_FACTOR, repetitions=0)
