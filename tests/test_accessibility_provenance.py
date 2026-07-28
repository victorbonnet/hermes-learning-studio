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


def test_confirmed_track_is_not_a_source_at_all(hermes_home: Path):
    """The reported exploit, and why the source had to go.

    One ``save_context`` call can create a confirmed track *and* the consent
    that supposedly authorises its context, because ``track.confirmed``, the
    consent statement and the need are all fields the model writes. The row
    that came out read ``provenance = confirmed_track, confirmed = 1`` and a
    later manifest could cite it. Nothing outside the model was involved at
    any point, so the source is gone rather than gated.
    """
    with pytest.raises(service.ValidationError, match="must be one of"):
        prepare(manifest=manifest(accessibility=access("confirmed_track", "captions")))


def test_the_exploit_creates_no_durable_accessibility_row(hermes_home: Path):
    """The first half of the exploit, checked at the database."""
    from learning_studio import storage

    service.save_context(
        principal=LEARNER,
        track={
            "name": "Claimed track",
            "confirmed": True,
            "context": {"accessibility_needs": ["captions"]},
        },
        accessibility_consent={"consent_statement": "remember captions", "needs": ["captions"]},
    )

    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM context_values WHERE field = 'accessibility_needs'"
        ).fetchall()

    assert rows == [], "an accessibility need was written to storage"


def test_the_exploit_creates_no_confirmed_provenance_claim(hermes_home: Path):
    outcome = service.save_context(
        principal=LEARNER,
        track={
            "name": "Claimed track",
            "confirmed": True,
            "context": {"accessibility_needs": ["captions"], "goal": "pass the exam"},
        },
        accessibility_consent={"consent_statement": "remember captions", "needs": ["captions"]},
    )["outcome"]

    assert [entry["field"] for entry in outcome["not_stored"]] == ["accessibility_needs"]
    # The non-sensitive half of the same call is unaffected.
    assert outcome["track"]["status"] == "created"
    stored = service.get_context(principal=LEARNER)["confirmed_context"]
    assert "accessibility_needs" not in stored
    assert stored["goal"]["value"] == "pass the exam"


def test_the_later_manifest_cannot_launder_the_assertion(hermes_home: Path):
    """The second half: the track exists, and still authorises nothing."""
    from learning_studio import storage

    saved = service.save_context(
        principal=LEARNER,
        track={
            "name": "Claimed track",
            "confirmed": True,
            "context": {"accessibility_needs": ["captions"]},
        },
        accessibility_consent={"consent_statement": "remember captions", "needs": ["captions"]},
    )
    track_id = saved["outcome"]["track"]["track_id"]

    with pytest.raises(service.ValidationError, match="must be one of"):
        prepare(
            manifest=manifest(accessibility=access("confirmed_track", "captions")),
            track_id=track_id,
        )

    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"] == 0


def test_profile_config_without_matching_configuration_is_refused(hermes_home: Path):
    with pytest.raises(service.ConsentError, match="nothing recorded there says so"):
        prepare(manifest=manifest(accessibility=access("profile_config", "keyboard_only")))


def test_explicit_request_is_not_a_source_either(hermes_home: Path):
    with pytest.raises(service.ValidationError, match="must be one of"):
        prepare(manifest=manifest(accessibility=access("explicit_request", "captions")))


def test_a_model_written_session_row_authorises_nothing(hermes_home: Path):
    """Writing the need first does not make a later claim true."""
    record_session_need("captions")

    with pytest.raises(service.ValidationError, match="must be one of"):
        prepare(manifest=manifest(accessibility=access("confirmed_track", "captions")))


def test_a_config_entry_for_one_accommodation_does_not_authorise_another(hermes_home: Path):
    config = LearningStudioConfig(profile_context={"accessibility_needs": ["captions"]})

    with pytest.raises(service.ConsentError):
        service.prepare_experience(
            principal=LEARNER,
            manifest=manifest(accessibility=access("profile_config", "transcript")),
            config=config,
        )


def test_a_refused_claim_stores_nothing(hermes_home: Path):
    from learning_studio import storage

    with pytest.raises(service.ConsentError):
        prepare(manifest=manifest(accessibility=access("profile_config", "captions")))

    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"] == 0


# ── Cross-learner isolation ───────────────────────────────────────────────


def test_another_learners_session_need_authorises_nothing(hermes_home: Path):
    record_session_need("captions", principal=OTHER)

    with pytest.raises(service.ValidationError, match="must be one of"):
        prepare(LEARNER, manifest=manifest(accessibility=access("confirmed_track", "captions")))


def test_a_config_accommodation_is_not_learner_specific(hermes_home: Path):
    """Operator configuration is profile-wide, and honestly labelled as such."""
    config = LearningStudioConfig(profile_context={"accessibility_needs": ["reduced_motion"]})

    for principal in (LEARNER, OTHER):
        result = service.prepare_experience(
            principal=principal,
            manifest=manifest(accessibility=access("profile_config", "reduced_motion")),
            config=config,
        )
        assert result["ok"] is True


# ── The one authoritative source works ────────────────────────────────────


def test_operator_configuration_is_accepted(hermes_home: Path):
    config = LearningStudioConfig(profile_context={"accessibility_needs": ["reduced_motion"]})

    result = service.prepare_experience(
        principal=LEARNER,
        manifest=manifest(accessibility=access("profile_config", "reduced_motion")),
        config=config,
    )

    assert result["ok"] is True
    assert result["experience"]["accessibility"]["accommodations"] == ["reduced_motion"]


def test_an_experience_may_carry_no_accessibility_metadata_at_all(hermes_home: Path):
    """The common case. Nothing claimed, nothing to authorise."""
    assert prepare()["ok"] is True


def test_matching_is_exact_on_the_canonical_form(hermes_home: Path):
    """Case and spacing are ignored; nothing else is."""
    config = LearningStudioConfig(profile_context={"accessibility_needs": ["  CAPTIONS  "]})

    assert service.prepare_experience(
        principal=LEARNER,
        manifest=manifest(accessibility=access("profile_config", "captions")),
        config=config,
    )["ok"]

    narrower = LearningStudioConfig(
        profile_context={"accessibility_needs": ["captions on all video"]}
    )
    with pytest.raises(service.ConsentError):
        service.prepare_experience(
            principal=LEARNER,
            manifest=manifest(accessibility=access("profile_config", "captions")),
            config=narrower,
        )


def test_preparing_with_accessibility_creates_no_memory_candidate(hermes_home: Path):
    """Exercise metadata must not become a durable fact about the learner."""
    config = LearningStudioConfig(profile_context={"accessibility_needs": ["captions"]})

    service.prepare_experience(
        principal=LEARNER,
        manifest=manifest(accessibility=access("profile_config", "captions")),
        config=config,
    )

    context = service.get_context(principal=LEARNER, include_memory_candidates=True)
    assert context["memory_candidates"] == []


def test_an_accessibility_need_is_never_written_to_storage(hermes_home: Path):
    """Whatever the consent argument says, and whatever the value is."""
    from learning_studio import storage

    for need in ("captions", "captions on all audio", "ADHD"):
        service.save_context(
            principal=LEARNER,
            temporary_context={"accessibility_needs": [need]},
            accessibility_consent={
                "consent_statement": f"remember I need {need}",
                "needs": [need],
            },
        )

    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM context_values WHERE field = 'accessibility_needs'"
        ).fetchone()["n"]

    assert rows == 0


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


def test_preparing_does_not_change_the_stored_context(hermes_home: Path):
    """It reads the configured accommodation; it never copies it into storage."""
    config = LearningStudioConfig(profile_context={"accessibility_needs": ["captions"]})
    track_id = confirmed_track_with("plain_language")
    before = service.get_context(principal=LEARNER, track_id=track_id)["confirmed_context"]

    service.prepare_experience(
        principal=LEARNER,
        manifest=manifest(accessibility=access("profile_config", "captions")),
        track_id=track_id,
        config=config,
    )

    after = service.get_context(principal=LEARNER, track_id=track_id)["confirmed_context"]
    assert after == before
