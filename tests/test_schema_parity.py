"""The advertised schema and the runtime validator must agree.

Two layers look at the same arguments: the JSON Schema a provider is given,
and the checker the handler runs before touching storage. A disagreement is
not cosmetic. If the schema is *looser*, the model is told a value is fine and
then refused — a debugging trap. If it were *tighter*, a provider would reject
work the plugin would have accepted.

So the schema is checked here against a real JSON Schema implementation
(``jsonschema``, a development dependency — the plugin's own runtime
dependencies stay empty), and every case is asserted at both layers. Where a
constraint genuinely cannot be expressed in JSON Schema — an answer that
references an option, an objective that must match a stored one — the runtime
stays authoritative and the schema says so in a description.
"""

from __future__ import annotations

import json

import pytest

from learning_studio.components import COMPONENT_TYPES, SPEC_BY_TYPE
from learning_studio.manifest import ManifestError, build_manifest
from learning_studio.safety import DATE_PATTERN, IDENTIFIER_PATTERN, LOCALE_PATTERN
from learning_studio.schemas import PREPARE_SCHEMA
from learning_studio.validation import SchemaViolation, validate
from tests.component_examples import example, manifest

jsonschema = pytest.importorskip("jsonschema")

PARAMETERS = PREPARE_SCHEMA["parameters"]


@pytest.fixture(scope="module")
def library_validator():
    """A real JSON Schema validator over the advertised parameters."""
    cls = jsonschema.validators.validator_for(PARAMETERS)
    cls.check_schema(PARAMETERS)
    return cls(PARAMETERS)


def _resolve(node: dict) -> dict:
    """Follow a local ``$ref`` into ``$defs``, the way a provider would."""
    pointer = node.get("$ref")
    if pointer is None:
        return node
    return PARAMETERS["$defs"][pointer.removeprefix("#/$defs/")]


def rejected_by_library(validator, arguments) -> bool:
    return next(validator.iter_errors(arguments), None) is not None


def rejected_by_runtime(arguments) -> bool:
    try:
        validate(arguments, PARAMETERS)
    except SchemaViolation:
        return True
    return False


# ── The advertised schema is a valid schema ───────────────────────────────


def test_the_advertised_schema_is_well_formed(library_validator):
    assert library_validator is not None


def test_the_advertised_schema_is_json_serialisable():
    assert json.loads(json.dumps(PARAMETERS))


def test_a_valid_manifest_satisfies_both_layers(library_validator):
    arguments = {"manifest": manifest()}

    assert not rejected_by_library(library_validator, arguments)
    assert not rejected_by_runtime(arguments)


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_every_component_example_satisfies_both_layers(component_type, library_validator):
    arguments = {"manifest": manifest([example(component_type)])}

    assert not rejected_by_library(library_validator, arguments)
    assert not rejected_by_runtime(arguments)


# ── Representative invalid values, refused by both ────────────────────────


def component_with(**overrides):
    return {"manifest": manifest([example("multiple_choice", **overrides)])}


def manifest_with(**overrides):
    return {"manifest": manifest(**overrides)}


PARITY_CASES: list[tuple[str, dict]] = [
    ("uppercase identifier", component_with(id="UPPER")),
    ("identifier with a space", component_with(id="two words")),
    ("identifier with a slash", component_with(id="a/b")),
    ("identifier that is too long", component_with(id="a" * 80)),
    ("malformed locale", manifest_with(ui_locale="english")),
    ("locale with an underscore", manifest_with(ui_locale="en_GB")),
    (
        "malformed publication date",
        manifest_with(source_references=[{"title": "A book", "published_on": "last year"}]),
    ),
    ("unknown top-level field", manifest_with(smuggled="value")),
    (
        "unknown nested field",
        {"manifest": manifest([{**example("multiple_choice"), "smuggled": "value"}])},
    ),
    (
        "unknown component type",
        {"manifest": manifest([{**example("multiple_choice"), "type": "mind_reading"}])},
    ),
    ("invalid difficulty enum", manifest_with(difficulty="very hard")),
    (
        "invalid accommodation enum",
        manifest_with(accessibility={"source": "explicit_request", "accommodations": ["dyslexia"]}),
    ),
    (
        "invalid accessibility source",
        manifest_with(accessibility={"source": "guessed", "accommodations": ["captions"]}),
    ),
    (
        "scoring mode this type cannot use",
        {
            "manifest": manifest(
                [{**example("multiple_choice"), "evaluation": {"scoring": {"mode": "ordered"}}}]
            )
        },
    ),
    ("oversized string", manifest_with(title="T" * 5000)),
    ("duration above the bound", manifest_with(expected_duration_minutes=9000)),
    ("duration below the bound", manifest_with(expected_duration_minutes=0)),
    ("wrong type for duration", manifest_with(expected_duration_minutes="ten")),
    (
        "too many components",
        {"manifest": manifest([example("true_false", id=f"item-{index}") for index in range(41)])},
    ),
    ("empty component list", {"manifest": manifest([])}),
    ("unknown tool argument", {"manifest": manifest(), "learner_key": "2002"}),
]


@pytest.mark.parametrize(
    ("label", "arguments"), PARITY_CASES, ids=[label for label, _ in PARITY_CASES]
)
def test_an_invalid_value_is_refused_by_both_layers(label, arguments, library_validator):
    assert rejected_by_library(library_validator, arguments), (
        f"the advertised schema accepts {label}, which the runtime refuses"
    )
    assert rejected_by_runtime(arguments), (
        f"the runtime accepts {label}, which the advertised schema refuses"
    )


@pytest.mark.parametrize(
    ("label", "arguments"),
    [case for case in PARITY_CASES if set(case[1]) == {"manifest"}],
    ids=[label for label, args in PARITY_CASES if set(args) == {"manifest"}],
)
def test_an_invalid_value_never_reaches_a_built_manifest(label, arguments):
    """And the manifest builder refuses it too — the third and final layer."""
    with pytest.raises(ManifestError):
        build_manifest(arguments["manifest"])


# ── Lexical patterns are the same declaration on both sides ───────────────


def test_the_identifier_pattern_is_published_in_the_schema():
    assert PARAMETERS["$defs"]["identifier"]["pattern"] == IDENTIFIER_PATTERN


def test_the_locale_pattern_is_published_in_the_schema():
    locale = PARAMETERS["properties"]["manifest"]["properties"]["ui_locale"]

    assert locale["pattern"] == LOCALE_PATTERN


def test_the_date_pattern_is_published_in_the_schema():
    references = PARAMETERS["properties"]["manifest"]["properties"]["source_references"]

    assert references["items"]["properties"]["published_on"]["pattern"] == DATE_PATTERN


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_each_branch_advertises_the_scoring_modes_its_type_allows(component_type: str):
    """The per-type enum in the schema is the tuple the runtime checks."""
    from learning_studio.components import component_members

    spec = SPEC_BY_TYPE[component_type]
    evaluation = next(m for m in component_members(spec) if m.name == "evaluation")
    scoring = _resolve(PARAMETERS["$defs"][evaluation.ref]["properties"]["scoring"])

    assert scoring["properties"]["mode"]["enum"] == list(spec.scoring_modes)


@pytest.mark.parametrize("component_type", COMPONENT_TYPES)
def test_a_self_report_type_advertises_no_rubric_at_all(component_type: str):
    from learning_studio.components import component_members

    spec = SPEC_BY_TYPE[component_type]
    if not spec.self_report:
        pytest.skip("this type may be marked against a rubric")

    evaluation = next(m for m in component_members(spec) if m.name == "evaluation")
    assert "rubric" not in PARAMETERS["$defs"][evaluation.ref]["properties"]


# ── What JSON Schema cannot express is documented, not pretended ──────────


def test_semantic_constraints_are_described_where_the_schema_cannot_enforce_them():
    """A reader of the schema alone must not be surprised by a refusal."""
    blob = json.dumps(PARAMETERS).lower()

    for promise in ("must already be recorded", "unique where it is used"):
        assert promise in blob


def test_the_runtime_stays_stricter_where_it_has_to_be(library_validator):
    """A cross-field rule the schema cannot state: an answer naming an option.

    The advertised schema accepts this — ``option_id`` is a well-formed
    identifier — and the runtime refuses it. That is the one direction the
    parity rule allows, and it is why the runtime is the boundary.
    """
    arguments = {"manifest": manifest([example("multiple_choice", answer={"option_id": "ghost"})])}

    assert not rejected_by_library(library_validator, arguments)
    with pytest.raises(ManifestError, match="does not declare"):
        build_manifest(arguments["manifest"])
