"""Isolation: no data crosses a profile or a learner boundary.

These are the adversarial tests. They assume a caller who has a valid track
ID belonging to somebody else and is trying to use it — because that is the
realistic failure mode. One profile can serve a family, a classroom, or a
shared assistant, and a plugin that leaks one person's learning goals into
another's session has failed at the only thing that makes it safe to use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from learning_studio import service

ALICE = "user-1001"
BOB = "user-2002"


def _make_track(learner_key: str, name: str) -> str:
    result = service.save_context(
        learner_key=learner_key,
        track={"name": name, "confirmed": True, "context": {"goal": f"goal for {name}"}},
    )
    return result["outcome"]["track"]["track_id"]


# ── Learner isolation within one profile ──────────────────────────────────


def test_two_learners_in_one_profile_do_not_see_each_other(hermes_home: Path):
    _make_track(ALICE, "Alice track")
    _make_track(BOB, "Bob track")

    alice = service.get_context(learner_key=ALICE)
    bob = service.get_context(learner_key=BOB)

    assert [t["name"] for t in alice["tracks"]] == ["Alice track"]
    assert [t["name"] for t in bob["tracks"]] == ["Bob track"]


def test_a_known_track_id_does_not_grant_another_learner_read_access(hermes_home: Path):
    alice_track = _make_track(ALICE, "Alice track")
    _make_track(BOB, "Bob track")

    with pytest.raises(service.NotFoundError):
        service.get_context(learner_key=BOB, track_id=alice_track)


def test_a_known_track_name_does_not_grant_another_learner_read_access(hermes_home: Path):
    _make_track(ALICE, "Alice track")
    _make_track(BOB, "Bob track")

    with pytest.raises(service.NotFoundError):
        service.get_context(learner_key=BOB, track_name="Alice track")


def test_a_known_track_id_does_not_grant_another_learner_write_access(hermes_home: Path):
    alice_track = _make_track(ALICE, "Alice track")
    _make_track(BOB, "Bob track")

    with pytest.raises(service.NotFoundError):
        service.save_context(
            learner_key=BOB,
            track={"track_id": alice_track, "context": {"goal": "hijacked"}},
        )

    alice = service.get_context(learner_key=ALICE, track_id=alice_track)
    assert alice["confirmed_context"]["goal"]["value"] == "goal for Alice track"


def test_another_learner_cannot_archive_or_withdraw_a_track(hermes_home: Path):
    alice_track = _make_track(ALICE, "Alice track")
    _make_track(BOB, "Bob track")

    with pytest.raises(service.NotFoundError):
        service.save_context(
            learner_key=BOB, track={"track_id": alice_track, "status": "withdrawn"}
        )

    alice = service.get_context(learner_key=ALICE, track_id=alice_track)
    assert alice["track_selection"]["track_id"] == alice_track
    assert alice["tracks"][0]["status"] == "active"


def test_another_learner_cannot_attach_an_objective_to_a_foreign_track(hermes_home: Path):
    alice_track = _make_track(ALICE, "Alice track")
    _make_track(BOB, "Bob track")

    with pytest.raises(service.NotFoundError):
        service.save_context(
            learner_key=BOB,
            objectives=[
                {
                    "track_id": alice_track,
                    "behavior": "b",
                    "condition": "c",
                    "standard": "s",
                }
            ],
        )

    alice = service.get_context(learner_key=ALICE, track_id=alice_track)
    assert alice["objectives"] == []


def test_another_learner_cannot_correct_a_foreign_track(hermes_home: Path):
    alice_track = _make_track(ALICE, "Alice track")
    _make_track(BOB, "Bob track")

    with pytest.raises(service.NotFoundError):
        service.save_context(
            learner_key=BOB,
            corrections=[
                {"field": "goal", "value": "hijacked", "track_id": alice_track, "durable": True}
            ],
        )


def test_an_unknown_id_and_a_foreign_id_give_the_same_answer(hermes_home: Path):
    """Otherwise a track ID becomes an oracle for whether a learner exists."""
    alice_track = _make_track(ALICE, "Alice track")
    _make_track(BOB, "Bob track")

    with pytest.raises(service.NotFoundError) as foreign:
        service.get_context(learner_key=BOB, track_id=alice_track)
    with pytest.raises(service.NotFoundError) as absent:
        service.get_context(learner_key=BOB, track_id="0" * 32)

    assert str(foreign.value) == str(absent.value) == service.NOT_FOUND_MESSAGE


def test_the_error_message_names_no_learner_track_or_path(hermes_home: Path):
    alice_track = _make_track(ALICE, "Alice secret project")

    with pytest.raises(service.NotFoundError) as exc:
        service.get_context(learner_key=BOB, track_id=alice_track)

    message = str(exc.value)
    for leak in (alice_track, "Alice", "secret", ALICE, str(hermes_home)):
        assert leak not in message


# ── Profile isolation ─────────────────────────────────────────────────────


def test_a_second_profile_starts_empty(tmp_path: Path, monkeypatch):
    """Profiles are separate people or separate lives; never a shared store."""
    first = tmp_path / "profile-a"
    second = tmp_path / "profile-b"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(first))
    _make_track(ALICE, "Track in profile A")
    assert len(service.get_context(learner_key=ALICE)["tracks"]) == 1

    monkeypatch.setenv("HERMES_HOME", str(second))
    assert service.get_context(learner_key=ALICE)["tracks"] == []


def test_a_track_id_from_another_profile_is_not_readable(tmp_path: Path, monkeypatch):
    first = tmp_path / "profile-a"
    second = tmp_path / "profile-b"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(first))
    track_id = _make_track(ALICE, "Track in profile A")

    monkeypatch.setenv("HERMES_HOME", str(second))
    with pytest.raises(service.NotFoundError):
        service.get_context(learner_key=ALICE, track_id=track_id)


def test_every_learner_owned_row_carries_its_profile_scope(hermes_home: Path):
    """The scope columns are the invariant the queries rely on."""
    from learning_studio import storage

    _make_track(ALICE, "Alice track")
    service.save_context(
        learner_key=ALICE,
        temporary_context={"subject": "anything"},
        objectives=None,
    )

    scoped = (
        "tracks",
        "learning_contexts",
        "context_values",
        "context_revisions",
        "objectives",
        "memory_candidates",
        "learners",
    )
    with storage.connect() as conn:
        for table in scoped:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "profile_id" in columns, f"{table} has no profile scope column"
            unscoped = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE profile_id IS NULL OR profile_id = ''"
            ).fetchone()
            assert int(unscoped["n"]) == 0, f"{table} holds rows with no profile scope"


# ── Multiple tracks per learner ───────────────────────────────────────────


def test_a_learner_may_hold_several_tracks(hermes_home: Path):
    _make_track(ALICE, "First")
    _make_track(ALICE, "Second")
    _make_track(ALICE, "Third")

    result = service.get_context(learner_key=ALICE)

    assert sorted(t["name"] for t in result["tracks"]) == ["First", "Second", "Third"]


def test_a_second_track_never_overwrites_the_first(hermes_home: Path):
    first = _make_track(ALICE, "First")
    second = _make_track(ALICE, "Second")

    assert first != second
    one = service.get_context(learner_key=ALICE, track_id=first)
    two = service.get_context(learner_key=ALICE, track_id=second)
    assert one["confirmed_context"]["goal"]["value"] == "goal for First"
    assert two["confirmed_context"]["goal"]["value"] == "goal for Second"


def test_reusing_a_track_name_is_refused_rather_than_merged(hermes_home: Path):
    _make_track(ALICE, "Duplicate")

    with pytest.raises(service.ValidationError, match="already has a track"):
        _make_track(ALICE, "Duplicate")


def test_several_active_tracks_make_selection_ambiguous_not_arbitrary(hermes_home: Path):
    """Guessing a track is worse than asking: it silently studies the wrong thing."""
    _make_track(ALICE, "First")
    _make_track(ALICE, "Second")

    result = service.get_context(learner_key=ALICE)

    assert result["track_selection"]["mode"] == "ambiguous"
    assert result["track_selection"]["track_id"] is None
    assert result["confirmed_context"] == {}
    assert len(result["track_selection"]["candidates"]) == 2


def test_one_active_track_is_selected_without_asking(hermes_home: Path):
    track_id = _make_track(ALICE, "Only one")

    result = service.get_context(learner_key=ALICE)

    assert result["track_selection"]["mode"] == "single_active_track"
    assert result["track_selection"]["track_id"] == track_id


def test_archiving_removes_a_track_from_the_ambiguity_set(hermes_home: Path):
    first = _make_track(ALICE, "First")
    second = _make_track(ALICE, "Second")

    service.save_context(learner_key=ALICE, track={"track_id": first, "status": "archived"})

    result = service.get_context(learner_key=ALICE)
    assert result["track_selection"]["track_id"] == second
    assert len(result["tracks"]) == 2, "archiving is not deletion"
