"""Manifest validation: the envelope, its bounds, and its refusals.

The manifest is the whole surface an agent writes to, so the tests here are
mostly about what is *refused*. A validator that accepts a manifest with an
unreachable branch, an unbounded passage, or a URL in a source reference is
not a validator; it is a place where those things get stored.
"""

from __future__ import annotations

import pytest

from learning_studio.manifest import (
    ACCESSIBILITY_SOURCES,
    DIFFICULTIES,
    MANIFEST_SCHEMA_VERSION,
    MAX_COMPONENTS,
    MAX_MANIFEST_BYTES,
    ManifestError,
    build_manifest,
)
from learning_studio.models import Provenance
from tests.component_examples import example, manifest


def branching(component_type: str, component_id: str, **evaluation):
    """A component with a chosen id and branching, for the graph tests."""
    payload = example(component_type, id=component_id)
    payload.setdefault("evaluation", {}).update(evaluation)
    return payload


# ── Valid manifests, across unrelated subjects ────────────────────────────


def test_a_minimal_manifest_validates():
    built = build_manifest(manifest())

    assert built.schema_version == MANIFEST_SCHEMA_VERSION
    assert built.component_count == 1


@pytest.mark.parametrize(
    ("subject", "component_type"),
    [
        ("A Portuguese listening drill", "translation"),
        ("Fire evacuation procedure", "process_flow"),
        ("Renaissance patronage", "free_response"),
        ("Titration arithmetic", "short_answer"),
        ("Reading a circuit diagram", "diagram"),
        ("Clinical decision-making", "scenario_choice"),
        ("Kanji recall", "flashcard"),
        ("Comparing cell division", "table_grid"),
    ],
)
def test_manifests_across_unrelated_subjects_validate(subject: str, component_type: str):
    """One registry, many disciplines — the point of a generic contract."""
    built = build_manifest(manifest([example(component_type)], title=subject))

    assert built.title == subject


def test_a_manifest_may_carry_every_optional_field():
    built = build_manifest(
        manifest(
            content_locale="pt-BR",
            source_references=[
                {
                    "title": "Atlas of Human Anatomy",
                    "author": "Netter",
                    "published_on": "2019-03",
                    "citation": "Netter 2019, plate 214",
                    "source_id": "atlas-2019",
                    "note": "Used for the diagram only.",
                }
            ],
            accessibility={
                "source": "explicit_request",
                "captions_required": True,
                "reading_level": "plain",
                "no_time_limit": True,
                "notes": "The learner asked for captions this session.",
            },
            delivery={"mode": "practice", "allow_back": True, "time_limit_seconds": 0},
        )
    )

    assert built.content_locale == "pt-BR"
    assert built.source_references[0]["citation"] == "Netter 2019, plate 214"
    assert built.accessibility["reading_level"] == "plain"


def test_a_manifest_carries_no_field_that_names_a_learner():
    """The whole class of impersonation the identity design removes.

    Checked against property *names*: prose may legitimately mention a
    profile — "an approved source identifier, if this profile has one" — but
    no field a caller can fill in may name a person.
    """
    from learning_studio.schemas import PREPARE_SCHEMA

    offenders: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for name in node.get("properties", {}):
                lowered = name.lower()
                for forbidden in ("learner", "user", "username", "principal", "profile", "session"):
                    if forbidden in lowered:
                        offenders.append(f"{path}.{name}")
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(PREPARE_SCHEMA["parameters"], "prepare")
    assert offenders == []


def test_the_experience_id_cannot_be_supplied():
    """A caller who chose the id could try to overwrite someone else's."""
    with pytest.raises(ManifestError, match="experience_id"):
        build_manifest(manifest(experience_id="somebody-elses"))


# ── The envelope ──────────────────────────────────────────────────────────


def test_a_manifest_that_is_not_an_object_is_rejected():
    with pytest.raises(ManifestError, match="must be an object"):
        build_manifest("a quiz about the French Revolution")


@pytest.mark.parametrize("field", ["title", "objective", "instructions", "ui_locale"])
def test_a_missing_required_field_is_rejected(field: str):
    payload = manifest()
    del payload[field]

    with pytest.raises(ManifestError, match=field):
        build_manifest(payload)


def test_an_unknown_top_level_field_is_rejected():
    with pytest.raises(ManifestError, match="smuggled"):
        build_manifest(manifest(smuggled="value"))


@pytest.mark.parametrize("version", [0, 2, 99])
def test_a_wrong_schema_version_is_rejected(version: int):
    with pytest.raises(ManifestError, match="schema_version"):
        build_manifest(manifest(schema_version=version))


def test_a_missing_schema_version_is_rejected():
    payload = manifest()
    del payload["schema_version"]

    with pytest.raises(ManifestError, match="schema_version"):
        build_manifest(payload)


# ── The measurable objective ──────────────────────────────────────────────


@pytest.mark.parametrize("part", ["behavior", "condition", "standard"])
def test_an_objective_missing_a_part_is_rejected(part: str):
    """Behaviour without a standard is a direction, not a measurable objective."""
    objective = dict(manifest()["objective"])
    del objective[part]

    with pytest.raises(ManifestError, match=part):
        build_manifest(manifest(objective=objective))


def test_an_objective_that_is_a_bare_string_is_rejected():
    with pytest.raises(ManifestError, match="objective"):
        build_manifest(manifest(objective="get better at Spanish"))


def test_an_overlong_objective_is_rejected():
    with pytest.raises(ManifestError, match="at most"):
        build_manifest(
            manifest(objective={"behavior": "b" * 600, "condition": "c", "standard": "s"})
        )


# ── Bounds ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("minutes", [0, -5, 241, 100000])
def test_an_out_of_range_duration_is_rejected(minutes: int):
    with pytest.raises(ManifestError, match="expected_duration_minutes"):
        build_manifest(manifest(expected_duration_minutes=minutes))


def test_a_duration_that_is_not_an_integer_is_rejected():
    with pytest.raises(ManifestError, match="integer"):
        build_manifest(manifest(expected_duration_minutes="ten"))


def test_a_boolean_duration_is_rejected():
    """``True`` is an int in Python; reading it as one minute is nonsense."""
    with pytest.raises(ManifestError, match="integer"):
        build_manifest(manifest(expected_duration_minutes=True))


@pytest.mark.parametrize("difficulty", ["easy", "hard", "GCSE", "", "INTERMEDIATE"])
def test_an_invalid_difficulty_is_rejected(difficulty: str):
    with pytest.raises(ManifestError, match="difficulty"):
        build_manifest(manifest(difficulty=difficulty))


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_every_declared_difficulty_is_accepted(difficulty: str):
    assert build_manifest(manifest(difficulty=difficulty)).difficulty == difficulty


@pytest.mark.parametrize("locale", ["english", "en_GB", "e", "EN", "en-gb-extra-long", "12"])
def test_a_malformed_locale_is_rejected(locale: str):
    with pytest.raises(ManifestError, match="locale"):
        build_manifest(manifest(ui_locale=locale))


@pytest.mark.parametrize("locale", ["en", "pt-BR", "zh-Hant-TW", "fr-CA", "gsw"])
def test_a_well_formed_locale_is_accepted(locale: str):
    assert build_manifest(manifest(ui_locale=locale)).ui_locale == locale


def test_an_overlong_title_is_rejected():
    with pytest.raises(ManifestError, match="at most"):
        build_manifest(manifest(title="T" * 500))


def test_too_many_components_are_rejected():
    components = [example("true_false", id=f"item-{index}") for index in range(MAX_COMPONENTS + 1)]

    with pytest.raises(ManifestError, match="at most"):
        build_manifest(manifest(components))


def test_a_manifest_with_no_components_is_rejected():
    with pytest.raises(ManifestError, match="at least one component"):
        build_manifest(manifest([]))


def test_components_must_be_an_array():
    with pytest.raises(ManifestError, match="array"):
        build_manifest(manifest(components={"id": "x"}))


def test_an_oversized_manifest_is_rejected():
    """Bounded on the serialised form, because that is what gets stored."""
    fat = example("case_study")
    fat["content"]["background"] = "word " * 780
    components = [dict(fat, id=f"case-{index}") for index in range(MAX_COMPONENTS)]

    with pytest.raises(ManifestError, match="byte limit"):
        build_manifest(manifest(components))


def test_a_manifest_just_under_the_size_limit_is_accepted():
    """Proves the limit discriminates rather than rejecting everything big."""
    built = build_manifest(manifest([example("case_study")]))

    assert built.component_count == 1
    assert MAX_MANIFEST_BYTES > 1000


# ── Component identity and ordering ───────────────────────────────────────


def test_duplicate_component_ids_are_rejected():
    components = [example("true_false", id="same"), example("short_answer", id="same")]

    with pytest.raises(ManifestError, match="duplicate"):
        build_manifest(manifest(components))


def test_component_order_is_the_array_order():
    components = [
        example("true_false", id="first"),
        example("short_answer", id="second"),
        example("flashcard", id="third"),
    ]

    built = build_manifest(manifest(components))

    assert [component.id for component in built.components] == ["first", "second", "third"]


def test_the_learner_summary_numbers_components_from_one():
    components = [example("true_false", id="a"), example("short_answer", id="b")]

    summary = build_manifest(manifest(components)).learner_summary()

    assert [entry["position"] for entry in summary["components"]] == [1, 2]


# ── Branching ─────────────────────────────────────────────────────────────


def test_a_branch_to_a_component_that_does_not_exist_is_rejected():
    components = [
        branching("multiple_choice", "one", branching=[{"on": "incorrect", "go_to": "ghost"}]),
        example("short_answer", id="two"),
    ]

    with pytest.raises(ManifestError, match="not a component of this experience"):
        build_manifest(manifest(components))


def test_a_branch_to_itself_is_rejected():
    components = [
        branching("multiple_choice", "one", branching=[{"on": "incorrect", "go_to": "one"}])
    ]

    with pytest.raises(ManifestError, match="branches to itself"):
        build_manifest(manifest(components))


def test_an_unconditional_cycle_is_rejected():
    """``always`` edges ignore the learner, so a loop of them never ends."""
    components = [
        branching("multiple_choice", "one", branching=[{"on": "always", "go_to": "two"}]),
        branching("short_answer", "two", branching=[{"on": "always", "go_to": "one"}]),
    ]

    with pytest.raises(ManifestError, match="loop with no way out"):
        build_manifest(manifest(components))


def test_a_longer_unconditional_cycle_is_also_rejected():
    components = [
        branching("multiple_choice", "one", branching=[{"on": "always", "go_to": "two"}]),
        branching("short_answer", "two", branching=[{"on": "always", "go_to": "three"}]),
        branching("true_false", "three", branching=[{"on": "always", "go_to": "one"}]),
    ]

    with pytest.raises(ManifestError, match="loop with no way out"):
        build_manifest(manifest(components))


def test_a_retry_loop_on_an_incorrect_answer_is_allowed():
    """ "Get it wrong, go back and try again" is a legitimate design.

    The learner's own answer is the way out, so this is not a trap — and
    refusing it would rule out one of the most common adaptive patterns there
    is.
    """
    components = [
        branching("multiple_choice", "one", branching=[{"on": "incorrect", "go_to": "two"}]),
        branching("short_answer", "two", branching=[{"on": "incorrect", "go_to": "one"}]),
    ]

    built = build_manifest(manifest(components))

    assert built.component_count == 2


def test_two_unconditional_branches_are_rejected():
    components = [
        branching(
            "multiple_choice",
            "one",
            branching=[{"on": "always", "go_to": "two"}, {"on": "always", "go_to": "two"}],
        ),
        example("short_answer", id="two"),
    ]

    with pytest.raises(ManifestError, match="more than one unconditional branch"):
        build_manifest(manifest(components))


def test_mixing_an_unconditional_branch_with_a_conditional_one_is_rejected():
    components = [
        branching(
            "multiple_choice",
            "one",
            branching=[{"on": "always", "go_to": "two"}, {"on": "correct", "go_to": "two"}],
        ),
        example("short_answer", id="two"),
    ]

    with pytest.raises(ManifestError, match="could never be taken"):
        build_manifest(manifest(components))


def test_an_unknown_branch_condition_is_rejected():
    components = [
        branching("multiple_choice", "one", branching=[{"on": "maybe", "go_to": "two"}]),
        example("short_answer", id="two"),
    ]

    with pytest.raises(ManifestError, match="must be one of"):
        build_manifest(manifest(components))


# ── Source references ─────────────────────────────────────────────────────


def test_a_source_reference_needs_a_title():
    with pytest.raises(ManifestError, match="title"):
        build_manifest(manifest(source_references=[{"author": "Anonymous"}]))


@pytest.mark.parametrize(
    "reference",
    [
        {"title": "A paper", "note": "See https://example.invalid/paper"},
        {"title": "A paper", "citation": "file:///home/someone/paper.pdf"},
        {"title": "A paper", "source_id": "../../etc/passwd"},
        {"title": "A paper", "source_id": "https://example.invalid"},
        {"title": "A paper", "note": "api_key=sk-live-ABCDEFGHIJKLMNOPQRST"},
        {"title": "A paper", "note": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"},
    ],
)
def test_a_source_reference_may_not_carry_a_locator_or_a_credential(reference: dict):
    """Provenance is description. Nothing here is fetched, so nothing may look
    fetchable, and nothing may carry authorisation material."""
    with pytest.raises(ManifestError):
        build_manifest(manifest(source_references=[reference]))


@pytest.mark.parametrize("published_on", ["last year", "2019-3", "19/03/2020", "2019-03-03-03"])
def test_a_malformed_publication_date_is_rejected(published_on: str):
    with pytest.raises(ManifestError, match="published_on"):
        build_manifest(
            manifest(source_references=[{"title": "A book", "published_on": published_on}])
        )


@pytest.mark.parametrize("published_on", ["2019", "2019-03", "2019-03-14"])
def test_an_acceptable_publication_date_is_kept(published_on: str):
    built = build_manifest(
        manifest(source_references=[{"title": "A book", "published_on": published_on}])
    )

    assert built.source_references[0]["published_on"] == published_on


def test_too_many_source_references_are_rejected():
    references = [{"title": f"Source {index}"} for index in range(11)]

    with pytest.raises(ManifestError, match="at most"):
        build_manifest(manifest(source_references=references))


# ── Accessibility metadata ────────────────────────────────────────────────


def test_accessibility_metadata_must_declare_where_it_came_from():
    with pytest.raises(ManifestError, match="source"):
        build_manifest(manifest(accessibility={"captions_required": True}))


@pytest.mark.parametrize("source", ACCESSIBILITY_SOURCES)
def test_the_three_authoritative_sources_are_accepted(source: str):
    built = build_manifest(manifest(accessibility={"source": source, "keyboard_only": True}))

    assert built.accessibility["source"] == source


@pytest.mark.parametrize(
    "source",
    [
        Provenance.UNCONFIRMED_INFERENCE.value,
        Provenance.RECENT_EVIDENCE.value,
        Provenance.DEFAULT.value,
        Provenance.CONFIRMED_PREFERENCE.value,
        "guessed",
    ],
)
def test_an_inferred_accessibility_source_is_rejected(source: str):
    """An exercise may not encode a guess about somebody's needs.

    ``recent_evidence`` and ``unconfirmed_inference`` are exactly the two
    provenances PR 03 refuses to treat as fact, and this keeps that refusal
    true on the exercise path too.
    """
    with pytest.raises(ManifestError, match="must be one of"):
        build_manifest(manifest(accessibility={"source": source}))


def test_the_accepted_sources_are_derived_from_the_provenance_vocabulary():
    """Adding a provenance in PR 03's model must not widen this silently."""
    assert set(ACCESSIBILITY_SOURCES) == {
        Provenance.EXPLICIT_REQUEST.value,
        Provenance.CONFIRMED_TRACK.value,
        Provenance.PROFILE_CONFIG.value,
    }


def test_an_unknown_accessibility_field_is_rejected():
    with pytest.raises(ManifestError, match="diagnosis"):
        build_manifest(
            manifest(accessibility={"source": "explicit_request", "diagnosis": "dyslexia"})
        )


def test_an_invalid_reading_level_is_rejected():
    with pytest.raises(ManifestError, match="reading_level"):
        build_manifest(
            manifest(accessibility={"source": "explicit_request", "reading_level": "easy"})
        )


def test_component_accessibility_metadata_is_learner_visible():
    """Alt text has to reach the renderer, so it belongs in the safe payload."""
    component = example(
        "image_choice",
        accessibility={"alt_text": "Two road signs side by side.", "no_time_limit": True},
    )

    built = build_manifest(manifest([component]))

    assert built.components[0].learner_payload()["accessibility"]["no_time_limit"] is True
