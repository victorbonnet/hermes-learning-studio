"""Accessibility metadata must be authorised by the source it names.

A ``source`` string written by a model is a *claim*. Treating it as
authorisation is the same defect that ``learner_key`` was: the model writes the
argument, so the argument proves nothing. Every accommodation is therefore
looked up in the source the manifest names, and an experience that cannot be
authorised is refused rather than stored with a plausible-looking label.

The second half of this module is about what may be written down at all. The
manifest has no free-text accessibility field, because a box to type in is a
box someone eventually types a diagnosis into.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from learning_studio import service
from learning_studio.config import LearningStudioConfig
from learning_studio.identity import Principal
from learning_studio.manifest import ACCOMMODATIONS, ManifestError, build_manifest
from tests.component_examples import example, manifest

LEARNER = Principal(
    profile="default", platform="telegram", user_id="7710", source="gateway_session"
)
OTHER = Principal(profile="default", platform="telegram", user_id="7720", source="gateway_session")


def access(source: str, *accommodations: str) -> dict:
    return {"source": source, "accommodations": list(accommodations)}


def prepare(principal: Principal = LEARNER, **kwargs):
    return service.prepare_experience(
        principal=principal, manifest=kwargs.pop("manifest", manifest()), **kwargs
    )


def record_session_need(need: str, principal: Principal = LEARNER) -> None:
    """Write a session need the way an agent would, consent and all."""
    service.save_context(
        principal=principal,
        temporary_context={"accessibility_needs": [need]},
        accessibility_consent={
            "consent_statement": f"please remember I need {need}",
            "needs": [need],
        },
    )


def confirmed_track_with(need: str, principal: Principal = LEARNER, name: str = "Track") -> str:
    saved = service.save_context(
        principal=principal,
        track={"name": name, "confirmed": True, "context": {"accessibility_needs": [need]}},
        accessibility_consent={
            "consent_statement": f"remember I need {need} on this track",
            "needs": [need],
        },
    )
    return saved["outcome"]["track"]["track_id"]


# ── A claim alone authorises nothing ──────────────────────────────────────


def test_confirmed_track_with_no_track_at_all_is_refused(hermes_home: Path):
    """The reported reproduction: a provenance label and no track behind it."""
    with pytest.raises(service.ConsentError, match="nothing recorded there says so"):
        prepare(manifest=manifest(accessibility=access("confirmed_track", "captions")))


def test_confirmed_track_naming_a_track_that_does_not_hold_the_need_is_refused(
    hermes_home: Path,
):
    track_id = confirmed_track_with("plain_language")

    with pytest.raises(service.ConsentError):
        prepare(
            manifest=manifest(accessibility=access("confirmed_track", "captions")),
            track_id=track_id,
        )


def test_profile_config_without_matching_configuration_is_refused(hermes_home: Path):
    with pytest.raises(service.ConsentError):
        prepare(manifest=manifest(accessibility=access("profile_config", "keyboard_only")))


def test_explicit_request_is_not_a_source_at_all(hermes_home: Path):
    """It was, and it could not stay.

    ``explicit_request`` was checked against the learner's temporary context —
    a row the model had written in an earlier call. "The learner asked for
    this" was therefore authorised by the model's own earlier assertion, which
    is not authorisation. Hermes supplies no per-request accessibility signal
    to check instead, so the source is gone rather than faked: a session-only
    need is honoured in conversation and simply not recorded on the exercise.
    """
    with pytest.raises(service.ValidationError, match="must be one of"):
        prepare(manifest=manifest(accessibility=access("explicit_request", "captions")))


def test_a_model_written_session_row_authorises_nothing(hermes_home: Path):
    """Writing the need first does not make the claim true afterwards."""
    record_session_need("captions")

    with pytest.raises(service.ValidationError, match="must be one of"):
        prepare(manifest=manifest(accessibility=access("explicit_request", "captions")))


def test_a_need_recorded_for_one_accommodation_does_not_authorise_another(hermes_home: Path):
    track_id = confirmed_track_with("plain_language")

    with pytest.raises(service.ConsentError):
        prepare(
            manifest=manifest(accessibility=access("confirmed_track", "transcript")),
            track_id=track_id,
        )


def test_a_claim_is_checked_against_the_source_it_names_not_any_source(hermes_home: Path):
    """Recorded on a track, claimed as operator configuration: still refused."""
    track_id = confirmed_track_with("reduced_motion")

    with pytest.raises(service.ConsentError):
        prepare(
            manifest=manifest(accessibility=access("profile_config", "reduced_motion")),
            track_id=track_id,
        )


def test_a_refused_claim_stores_nothing(hermes_home: Path):
    from learning_studio import storage

    track_id = confirmed_track_with("plain_language")
    with pytest.raises(service.ConsentError):
        prepare(
            manifest=manifest(accessibility=access("confirmed_track", "captions")),
            track_id=track_id,
        )

    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"] == 0


# ── Cross-learner and cross-track provenance ──────────────────────────────


def test_another_learners_confirmed_track_does_not_authorise_this_one(hermes_home: Path):
    confirmed_track_with("captions", principal=OTHER)
    mine = confirmed_track_with("plain_language", principal=LEARNER)

    with pytest.raises(service.ConsentError):
        prepare(
            LEARNER,
            manifest=manifest(accessibility=access("confirmed_track", "captions")),
            track_id=mine,
        )


def test_a_need_on_another_track_does_not_authorise_this_track(hermes_home: Path):
    with_need = confirmed_track_with("captions", name="With need")
    without = confirmed_track_with("plain_language", name="Without")

    assert with_need != without
    with pytest.raises(service.ConsentError):
        prepare(
            manifest=manifest(accessibility=access("confirmed_track", "captions")),
            track_id=without,
        )


def test_another_learners_track_cannot_be_named_for_provenance(hermes_home: Path):
    track_id = confirmed_track_with("captions", principal=OTHER)

    # Refused as an ownership failure before provenance is even considered.
    with pytest.raises(service.NotFoundError):
        prepare(
            LEARNER,
            manifest=manifest(accessibility=access("confirmed_track", "captions")),
            track_id=track_id,
        )


# ── Each genuinely authoritative source works ─────────────────────────────


def test_an_accommodation_confirmed_on_a_track_is_accepted(hermes_home: Path):
    track_id = confirmed_track_with("captions")

    result = prepare(
        manifest=manifest(accessibility=access("confirmed_track", "captions")),
        track_id=track_id,
    )

    assert result["ok"] is True
    assert result["experience"]["accessibility"]["accommodations"] == ["captions"]


def test_a_need_confirmed_on_the_named_track_is_accepted(hermes_home: Path):
    track_id = confirmed_track_with("plain_language")

    result = prepare(
        manifest=manifest(accessibility=access("confirmed_track", "plain_language")),
        track_id=track_id,
    )

    assert result["ok"] is True


def test_operator_configuration_is_accepted(hermes_home: Path):
    config = LearningStudioConfig(profile_context={"accessibility_needs": ["reduced_motion"]})

    result = service.prepare_experience(
        principal=LEARNER,
        manifest=manifest(accessibility=access("profile_config", "reduced_motion")),
        config=config,
    )

    assert result["ok"] is True


def test_an_experience_may_carry_no_accessibility_metadata_at_all(hermes_home: Path):
    """The common case. Nothing claimed, nothing to authorise."""
    assert prepare()["ok"] is True


def test_matching_is_exact_on_the_canonical_form(hermes_home: Path):
    """Case and spacing are ignored; nothing else is.

    "captions on all video" is not "captions", and it is not even storable —
    only the fixed accommodation tokens are. A looser comparison would be this
    module deciding something about a person's needs on its own.
    """
    track_id = confirmed_track_with("  CAPTIONS  ")
    assert prepare(
        manifest=manifest(accessibility=access("confirmed_track", "captions")),
        track_id=track_id,
    )["ok"]

    other_track = confirmed_track_with("captions on all video", principal=OTHER)
    with pytest.raises(service.ConsentError):
        prepare(
            OTHER,
            manifest=manifest(accessibility=access("confirmed_track", "captions")),
            track_id=other_track,
        )


# ── Nothing about a person may be written down ────────────────────────────


@pytest.mark.parametrize(
    "prose",
    [
        "The learner has ADHD and needs special treatment",
        "Student is autistic",
        "Reader has dyslexia",
        "Accommodates the learner's blindness",
        "For a user with glaucoma",
        "Following a diagnosis of dyscalculia",
        "This learner has a learning disability",
        "The learner cannot use a mouse",
        "They struggle with long passages",
    ],
)
def test_a_diagnosis_cannot_be_written_into_component_accessibility(prose: str):
    """Component accessibility describes the component, never the person."""
    component = example("image_choice", accessibility={"caption": prose})

    with pytest.raises(ManifestError):
        build_manifest(manifest([component]))


@pytest.mark.parametrize("field", ["alt_text", "caption", "long_description"])
def test_every_free_text_accessibility_field_is_guarded(field: str):
    component = example("diagram", accessibility={field: "The learner has ADHD"})

    with pytest.raises(ManifestError, match="diagnosis"):
        build_manifest(manifest([component]))


def test_an_asset_alt_text_is_guarded_too(hermes_home: Path):
    component = example("hotspot")
    component["content"]["image"]["alt_text"] = "A heart diagram for a learner with epilepsy"

    with pytest.raises(ManifestError):
        build_manifest(manifest([component]))


def test_ordinary_alt_text_is_accepted():
    """The guard must not reject the alt text people actually write."""
    component = example(
        "diagram",
        accessibility={
            "alt_text": "A circuit with a battery, a component, and a lamp in series.",
            "keyboard_alternative": "Type the component name instead of clicking it.",
        },
    )

    assert build_manifest(manifest([component])).component_count == 1


def test_a_biology_prompt_may_still_mention_a_condition():
    """The vocabulary is refused in accessibility text, not in subject matter.

    An exercise about glaucoma is an exercise about glaucoma. Refusing the
    word everywhere would make whole subjects unteachable to protect a field
    that is not involved.
    """
    component = example(
        "short_answer",
        prompt="Which structure is damaged in open-angle glaucoma?",
        answer={"accepted": ["the optic nerve head"]},
    )

    assert build_manifest(manifest([component])).component_count == 1


def test_the_accommodation_vocabulary_is_closed_and_describes_the_exercise():
    """Every token names something the *exercise* provides."""
    assert set(ACCOMMODATIONS) == {
        "captions",
        "transcript",
        "text_alternatives",
        "visual_description",
        "keyboard_only",
        "reduced_motion",
        "no_time_limit",
        "extended_time",
        "plain_language",
    }


def test_preparing_with_accessibility_creates_no_memory_candidate(hermes_home: Path):
    """Exercise metadata must not become a durable fact about the learner."""
    track_id = confirmed_track_with("captions")

    prepare(
        manifest=manifest(accessibility=access("confirmed_track", "captions")),
        track_id=track_id,
    )

    context = service.get_context(principal=LEARNER, include_memory_candidates=True)
    assert [c for c in context["memory_candidates"] if c["category"] == "accessibility"] == []


def test_preparing_does_not_change_the_stored_context(hermes_home: Path):
    """It reads the recorded accommodation; it never promotes or copies it."""
    track_id = confirmed_track_with("captions")
    before = service.get_context(principal=LEARNER, track_id=track_id)["confirmed_context"]

    prepare(
        manifest=manifest(accessibility=access("confirmed_track", "captions")),
        track_id=track_id,
    )

    after = service.get_context(principal=LEARNER, track_id=track_id)["confirmed_context"]
    assert after == before
