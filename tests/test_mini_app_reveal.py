"""Turning a flashcard over: the one authorised disclosure, and its fences.

The component contract this plugin publishes says a flashcard's reveal "must be
explicit and keyboard-operable". The first version of the Mini App had no reveal
at all, which meant a required interaction was silently missing — and the obvious
ways to add one are all wrong: shipping the back in the payload, hiding it in an
attribute, or letting any authenticated caller ask for any component's answer.

So the reveal is a route, and these tests are mostly about what it refuses. The
disclosure is one string, of one field, of one component type, after an attempt
has been committed, inside a session that already proved who it belongs to.
"""

from __future__ import annotations

import json

import pytest

from learning_studio import service
from learning_studio.config import LearningStudioConfig
from learning_studio.identity import Principal
from learning_studio.sessions import SessionStore
from learning_studio.web.app import INIT_DATA_HEADER, SESSION_HEADER, create_app
from learning_studio.web.dependencies import Dependencies
from tests.component_examples import CANARY, FLASHCARD_BACK, all_canaries, example, manifest
from tests.init_data import BOT_TOKEN, OTHER_USER_ID, USER_ID, build_init_data

NOW = 1_800_000_000


class Clock:
    def __init__(self, now: float = float(NOW)) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def config() -> LearningStudioConfig:
    return LearningStudioConfig()


@pytest.fixture
def allowlist() -> set[str]:
    return {USER_ID, OTHER_USER_ID}


@pytest.fixture
def deps(hermes_home, clock, config, allowlist) -> Dependencies:
    return Dependencies(
        config=config,
        sessions=SessionStore(
            ttl_seconds=config.mini_app_session_ttl_seconds,
            max_sessions=config.mini_app_max_sessions,
            clock=clock,
        ),
        bot_token=lambda: BOT_TOKEN,
        allowed_users=lambda: frozenset(allowlist),
        profile=lambda: "default",
        clock=clock,
        load_experience=lambda principal, experience_id: service.delivery_bundle(
            principal=principal, experience_id=experience_id, config=config
        ),
        load_asset=lambda principal, asset_id: service.read_managed_asset(
            principal=principal, asset_id=asset_id, config=config
        ),
        component_aliases=lambda principal, experience_id, component_key: service.component_aliases(
            principal=principal,
            experience_id=experience_id,
            component_key=component_key,
            config=config,
        ),
        reveal_answer=lambda principal, experience_id, component_key: (
            service.reveal_component_answer(
                principal=principal,
                experience_id=experience_id,
                component_key=component_key,
                config=config,
            )
        ),
        record_attempt=lambda principal, experience_id, responses, started_at, completed_at: (
            service.record_attempt(
                principal=principal,
                experience_id=experience_id,
                responses=responses,
                started_at=started_at,
                completed_at=completed_at,
                config=config,
            )
        ),
    )


@pytest.fixture
def client(deps):
    from fastapi.testclient import TestClient

    with TestClient(create_app(deps)) as test_client:
        yield test_client


def make_experience(principal, config, components) -> str:
    result = service.prepare_experience(
        principal=principal, manifest=manifest(components), config=config
    )
    return result["experience_id"]


@pytest.fixture
def flashcard_experience(hermes_home, principal, config) -> str:
    """A flashcard first, then a card that has nothing to reveal."""
    return make_experience(
        principal,
        config,
        [example("flashcard", id="card-one"), example("true_false", id="card-two")],
    )


def auth(user_id: str = USER_ID, age: int = 5, **kwargs) -> dict[str, str]:
    return {INIT_DATA_HEADER: build_init_data(user_id=user_id, auth_date=NOW - age, **kwargs)}


def open_session(client, experience_id: str, user_id: str = USER_ID) -> str:
    response = client.post(
        "/api/session", json={"experience_id": experience_id}, headers=auth(user_id)
    )
    assert response.status_code == 201, response.text
    return response.json()["session_token"]


def headers(token: str, user_id: str = USER_ID) -> dict[str, str]:
    return {**auth(user_id), SESSION_HEADER: token}


# ── Before the attempt, the back does not exist as far as the client knows ──


def test_the_back_is_absent_from_everything_served_before_a_reveal(client, flashcard_experience):
    """Recursively, and across every route that returns anything."""
    token = open_session(client, flashcard_experience)

    bodies = [
        client.get("/api/session/component", headers=headers(token)).text,
        client.get("/api/session/result", headers=headers(token)).text,
        client.post(
            "/api/session", json={"experience_id": flashcard_experience}, headers=auth()
        ).text,
    ]

    for body in bodies:
        assert FLASHCARD_BACK not in body
        assert CANARY not in body


def test_the_stored_learner_payload_never_held_the_back(hermes_home, principal, config):
    """The absence starts in the database, not in a projection at request time."""
    experience_id = make_experience(principal, config, [example("flashcard", id="card-one")])

    bundle = service.delivery_bundle(
        principal=principal, experience_id=experience_id, config=config
    )

    assert FLASHCARD_BACK not in json.dumps(bundle.experience)
    assert "back" not in json.dumps(bundle.experience)


# ── The reveal needs an attempt ────────────────────────────────────────────


def test_a_reveal_without_an_attempt_is_refused(client, flashcard_experience):
    token = open_session(client, flashcard_experience)

    response = client.post(
        "/api/session/reveal", json={"component_id": "card-one"}, headers=headers(token)
    )

    assert response.status_code == 400
    assert FLASHCARD_BACK not in response.text


@pytest.mark.parametrize("attempt", ["", "   ", "\n\t ", None, 42, [], {"text": "x"}])
def test_an_empty_or_malformed_attempt_buys_nothing(client, flashcard_experience, attempt):
    token = open_session(client, flashcard_experience)

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": attempt},
        headers=headers(token),
    )

    assert response.status_code == 400
    assert FLASHCARD_BACK not in response.text


def test_a_committed_attempt_reveals_exactly_one_field(client, flashcard_experience):
    token = open_session(client, flashcard_experience)

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama, I think"},
        headers=headers(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["back"] == FLASHCARD_BACK
    assert body["revealed"] is True
    assert body["attempt"] == "yama, I think"
    # Nothing else from the hidden half came with it.
    assert set(body) == {"component_id", "revealed", "back", "attempt", "notice"}
    assert CANARY not in response.text
    for canary in all_canaries():
        assert canary not in response.text


def test_the_mnemonic_and_every_other_evaluator_field_stay_hidden(client, flashcard_experience):
    """`flashcard.answer` also holds a mnemonic; only `back` is disclosable."""
    token = open_session(client, flashcard_experience)

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama"},
        headers=headers(token),
    )

    assert "mnemonic" not in response.text
    assert "rubric" not in response.text
    assert "hints" not in response.text
    assert "feedback" not in response.text


# ── The attempt is frozen ──────────────────────────────────────────────────


def test_a_repeated_reveal_is_idempotent_and_keeps_the_first_attempt(client, flashcard_experience):
    """A refresh must be safe, and must not be a second chance."""
    token = open_session(client, flashcard_experience)
    first = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "my first guess"},
        headers=headers(token),
    ).json()

    second = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "the answer, copied"},
        headers=headers(token),
    ).json()

    assert second["back"] == first["back"]
    assert second["attempt"] == "my first guess"


def test_the_recall_cannot_be_rewritten_after_the_answer_was_read(client, flashcard_experience):
    """The attack this exists to stop: peek, then submit the answer as recall."""
    token = open_session(client, flashcard_experience)
    client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "no idea"},
        headers=headers(token),
    )

    response = client.post(
        "/api/session/answer",
        json={
            "component_id": "card-one",
            "response": {"text": FLASHCARD_BACK, "self_rating": "easy"},
        },
        headers=headers(token),
    )

    assert response.status_code == 409
    assert (
        client.get("/api/session/component", headers=headers(token)).json()["component"][
            "component_id"
        ]
        == "card-one"
    ), "a refused submission must not advance the exercise"


def test_the_frozen_recall_is_what_gets_recorded(client, flashcard_experience):
    token = open_session(client, flashcard_experience)
    client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "no idea"},
        headers=headers(token),
    )

    response = client.post(
        "/api/session/answer",
        json={"component_id": "card-one", "response": {"text": "no idea", "self_rating": "again"}},
        headers=headers(token),
    )

    assert response.status_code == 200
    assert response.json()["progress"]["answered"] == 1


def test_a_flashcard_cannot_be_submitted_without_being_turned_over(client, flashcard_experience):
    token = open_session(client, flashcard_experience)

    response = client.post(
        "/api/session/answer",
        json={"component_id": "card-one", "response": {"text": "yama", "self_rating": "good"}},
        headers=headers(token),
    )

    assert response.status_code == 409
    assert FLASHCARD_BACK not in response.json()["error"]


def test_a_card_with_nothing_to_reveal_is_unaffected(client, flashcard_experience):
    """The reveal contract applies to revealable types and no others."""
    token = open_session(client, flashcard_experience)
    client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama"},
        headers=headers(token),
    )
    client.post(
        "/api/session/answer",
        json={"component_id": "card-one", "response": {"text": "yama", "self_rating": "good"}},
        headers=headers(token),
    )

    # `true_false` submits with no reveal at all.
    response = client.post(
        "/api/session/answer",
        json={"component_id": "card-two", "response": {"value": True}},
        headers=headers(token),
    )

    assert response.status_code == 200


def test_revealing_a_type_that_has_nothing_to_show_is_refused(
    client, hermes_home, principal, config
):
    experience_id = make_experience(principal, config, [example("true_false", id="only-card")])
    token = open_session(client, experience_id)

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "only-card", "attempt": "true"},
        headers=headers(token),
    )

    assert response.status_code in (404, 409)


# ── Every scope is enforced ────────────────────────────────────────────────


def test_an_unauthenticated_reveal_is_refused(client, flashcard_experience):
    response = client.post(
        "/api/session/reveal", json={"component_id": "card-one", "attempt": "yama"}
    )

    assert response.status_code == 401
    assert FLASHCARD_BACK not in response.text


def test_a_forged_payload_cannot_reveal(client, flashcard_experience):
    token = open_session(client, flashcard_experience)
    forged = {INIT_DATA_HEADER: build_init_data(auth_date=NOW - 5, signed=False)}

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama"},
        headers={**forged, SESSION_HEADER: token},
    )

    assert response.status_code == 401
    assert FLASHCARD_BACK not in response.text


def test_a_reveal_without_a_session_is_refused(client, flashcard_experience):
    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama"},
        headers=auth(),
    )

    assert response.status_code == 401
    assert FLASHCARD_BACK not in response.text


def test_another_telegram_account_cannot_use_this_session_to_reveal(client, flashcard_experience):
    token = open_session(client, flashcard_experience)

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama"},
        headers=headers(token, user_id=OTHER_USER_ID),
    )

    assert response.status_code == 401
    assert FLASHCARD_BACK not in response.text


def test_another_learner_cannot_reveal_a_card_of_this_experience(
    client, hermes_home, principal, other_principal, config
):
    """The service read is scoped in SQL, so a valid session of the *wrong*
    learner finds nothing rather than finding somebody else's answer."""
    owner_experience = make_experience(principal, config, [example("flashcard", id="card-one")])
    # The other learner opens their own session; their session cannot name the
    # first learner's experience, so the reveal is reached with their scope.
    other_experience = make_experience(
        other_principal, config, [example("flashcard", id="card-one")]
    )

    assert owner_experience != other_experience
    with pytest.raises(service.NotFoundError):
        service.reveal_component_answer(
            principal=other_principal,
            experience_id=owner_experience,
            component_key="card-one",
            config=config,
        )


def test_another_profile_cannot_reveal(hermes_home, principal, config):
    experience_id = make_experience(principal, config, [example("flashcard", id="card-one")])
    intruder = Principal(
        profile="other-profile",
        platform="telegram",
        user_id=principal.user_id,
        source="gateway_session",
    )

    with pytest.raises(service.NotFoundError):
        service.reveal_component_answer(
            principal=intruder,
            experience_id=experience_id,
            component_key="card-one",
            config=config,
        )


def test_a_component_of_another_experience_cannot_be_revealed(hermes_home, principal, config):
    first = make_experience(principal, config, [example("flashcard", id="card-one")])
    second = make_experience(principal, config, [example("flashcard", id="other-card")])

    assert service.reveal_component_answer(
        principal=principal, experience_id=first, component_key="card-one", config=config
    )
    with pytest.raises(service.NotFoundError):
        service.reveal_component_answer(
            principal=principal, experience_id=second, component_key="card-one", config=config
        )


def test_a_component_the_session_is_not_on_cannot_be_revealed(client, flashcard_experience):
    """Naming a later card must not skip ahead into its answer."""
    token = open_session(client, flashcard_experience)

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-two", "attempt": "peeking"},
        headers=headers(token),
    )

    assert response.status_code == 409


def test_an_unknown_component_id_is_refused(client, flashcard_experience):
    token = open_session(client, flashcard_experience)

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "no-such-card", "attempt": "x"},
        headers=headers(token),
    )

    assert response.status_code == 409


def test_an_expired_session_cannot_reveal(client, flashcard_experience, clock, config):
    token = open_session(client, flashcard_experience)
    clock.advance(config.mini_app_session_ttl_seconds + 1)

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama"},
        headers=headers(token),
    )

    assert response.status_code == 401
    assert FLASHCARD_BACK not in response.text


def test_a_finished_exercise_cannot_reveal(client, flashcard_experience):
    token = open_session(client, flashcard_experience)
    client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama"},
        headers=headers(token),
    )
    client.post(
        "/api/session/answer",
        json={"component_id": "card-one", "response": {"text": "yama", "self_rating": "good"}},
        headers=headers(token),
    )
    client.post(
        "/api/session/answer",
        json={"component_id": "card-two", "response": {"value": True}},
        headers=headers(token),
    )

    response = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "yama"},
        headers=headers(token),
    )

    assert response.status_code == 409


# ── The permission is a mapping, not a filter ─────────────────────────────


def test_only_one_field_of_one_type_is_ever_disclosable():
    """The permission is enumerated, so widening it is a visible diff."""
    assert service.REVEALABLE_ANSWER_FIELDS == {"flashcard": "back"}


def test_the_service_returns_a_string_not_the_hidden_record(hermes_home, principal, config):
    experience_id = make_experience(principal, config, [example("flashcard", id="card-one")])

    revealed = service.reveal_component_answer(
        principal=principal, experience_id=experience_id, component_key="card-one", config=config
    )

    assert isinstance(revealed, str)
    assert revealed == FLASHCARD_BACK


def test_the_reveal_is_rate_limited_like_every_other_route(client, flashcard_experience, config):
    token = open_session(client, flashcard_experience)
    statuses = set()

    for _attempt in range(config.mini_app_rate_limit_requests + 5):
        statuses.add(
            client.post(
                "/api/session/reveal",
                json={"component_id": "card-one", "attempt": "yama"},
                headers=headers(token),
            ).status_code
        )

    assert 429 in statuses


def test_the_reveal_writes_no_secret_to_the_logs(client, flashcard_experience, caplog):
    import logging

    token = open_session(client, flashcard_experience)
    with caplog.at_level(logging.DEBUG, logger="learning_studio.web"):
        client.post(
            "/api/session/reveal",
            json={"component_id": "card-one", "attempt": "my private recall"},
            headers=headers(token),
        )

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert FLASHCARD_BACK not in written
    assert "my private recall" not in written
    assert token not in written
    assert BOT_TOKEN not in written


# ── A failed reveal changes nothing ───────────────────────────────────────


def test_a_failed_reveal_leaves_the_session_untouched(
    client, deps, flashcard_experience, monkeypatch
):
    """The reported defect: the attempt was frozen before the reveal succeeded.

    Attempt A hits a failure; attempt B succeeds. B must be the frozen recall — if
    A had been committed, the learner would be held forever to a recall they never
    received an answer for, and no retry could correct it.
    """
    failures = {"count": 0}
    real = deps.reveal_answer

    def fail_once(*args):
        failures["count"] += 1
        if failures["count"] == 1:
            raise service.ServiceError("the store was unavailable")
        return real(*args)

    object.__setattr__(deps, "reveal_answer", fail_once)
    token = open_session(client, flashcard_experience)

    first = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "attempt A"},
        headers=headers(token),
    )
    assert first.status_code >= 400
    assert FLASHCARD_BACK not in first.text

    second = client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "attempt B"},
        headers=headers(token),
    )

    assert second.status_code == 200
    assert second.json()["attempt"] == "attempt B", "the failed attempt was committed"
    assert second.json()["back"] == FLASHCARD_BACK

    # And the submission is checked against B, not A.
    refused = client.post(
        "/api/session/answer",
        json={"component_id": "card-one", "response": {"text": "attempt A", "self_rating": "good"}},
        headers=headers(token),
    )
    assert refused.status_code == 409

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "card-one", "response": {"text": "attempt B", "self_rating": "good"}},
        headers=headers(token),
    )
    assert accepted.status_code == 200


def test_a_reveal_of_an_unrevealable_type_commits_nothing(client, hermes_home, principal, config):
    """The other way a reveal fails: the card has nothing to turn over.

    The card must remain submittable afterwards, which it would not be if the
    refused reveal had left a frozen attempt behind demanding a match.
    """
    experience_id = make_experience(principal, config, [example("true_false", id="only-card")])
    token = open_session(client, experience_id)

    refused = client.post(
        "/api/session/reveal",
        json={"component_id": "only-card", "attempt": "peeking"},
        headers=headers(token),
    )
    assert refused.status_code in (404, 409)

    answered = client.post(
        "/api/session/answer",
        json={"component_id": "only-card", "response": {"value": True}},
        headers=headers(token),
    )

    assert answered.status_code == 200


def test_a_failed_reveal_does_not_count_as_having_turned_the_card_over(
    client, deps, flashcard_experience
):
    """A refused reveal must not unlock the submission it was refusing."""

    def always_fail(*args):
        raise service.ServiceError("unavailable")

    object.__setattr__(deps, "reveal_answer", always_fail)
    token = open_session(client, flashcard_experience)

    client.post(
        "/api/session/reveal",
        json={"component_id": "card-one", "attempt": "attempt A"},
        headers=headers(token),
    )
    submitted = client.post(
        "/api/session/answer",
        json={"component_id": "card-one", "response": {"text": "attempt A", "self_rating": "good"}},
        headers=headers(token),
    )

    assert submitted.status_code == 409
