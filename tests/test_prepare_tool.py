"""``learning_studio_prepare`` as an LLM will actually call it.

The handler is the only part of this feature a model touches, so it is tested
the way a model uses it: with plausible arguments, wrong types, extra keys,
someone else's ids, and its own guesses at what identity fields might exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from learning_studio import service, tools
from learning_studio.schemas import PREPARE_SCHEMA, PREPARE_TOOL_NAME, TOOL_SCHEMAS
from tests.component_examples import CANARY, example, manifest


def call(**params) -> dict:
    raw = tools.handle_prepare(params)
    assert isinstance(raw, str), "handlers must return a JSON string"
    return json.loads(raw)


def strings_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in strings_in(item)]
    if isinstance(value, list):
        return [s for item in value for s in strings_in(item)]
    return []


# ── Registration ──────────────────────────────────────────────────────────


def test_exactly_three_tools_are_registered(ctx):
    from learning_studio import register

    register(ctx)

    assert sorted(tool.name for tool in ctx.tools) == [
        "learning_studio_get_context",
        "learning_studio_prepare",
        "learning_studio_save_context",
    ]


def test_prepare_is_in_the_reserved_toolset(ctx):
    from learning_studio import register

    register(ctx)

    tool = next(entry for entry in ctx.tools if entry.name == PREPARE_TOOL_NAME)
    assert tool.toolset == "plugin_learning_studio"


def test_the_manifest_declares_the_new_tool(repo_root: Path):
    """``hermes plugins list`` reads this; an undeclared tool misleads."""
    yaml = pytest.importorskip("yaml")

    with (repo_root / "plugin.yaml").open(encoding="utf-8") as handle:
        declared = yaml.safe_load(handle)["provides_tools"]

    assert sorted(declared) == sorted(TOOL_SCHEMAS)


def test_the_schema_is_json_serialisable():
    assert json.loads(json.dumps(PREPARE_SCHEMA))


#: The advertised schema rides in every request that offers this tool, so its
#: size is a running cost, not a one-off. The union of 31 fully inlined types
#: is over 140 KB; sharing the common shapes under ``$defs`` brings it to
#: roughly 49 KB. This ceiling is deliberately close to that, so a change that
#: undoes the sharing — or adds a description at 31 use sites instead of one —
#: fails here rather than quietly costing every session.
MAX_SCHEMA_BYTES = 60_000


def test_the_advertised_schema_stays_affordable():
    size = len(json.dumps(PREPARE_SCHEMA))

    assert size < MAX_SCHEMA_BYTES, (
        f"the prepare schema is {size} bytes, above the {MAX_SCHEMA_BYTES} ceiling — "
        "check whether shared definitions are still being referenced"
    )


def test_the_schema_shares_its_common_shapes():
    """The saving above comes from references; assert they are actually used."""
    from learning_studio.components import shared_definitions

    parameters = PREPARE_SCHEMA["parameters"]
    assert set(parameters["$defs"]) == set(shared_definitions())
    assert json.dumps(parameters).count('"$ref"') > len(parameters["$defs"])


def test_the_description_invites_natural_requests():
    """A learner says "quiz me"; the description has to make that route here."""
    description = PREPARE_SCHEMA["description"].lower()

    for word in ("practise", "revise", "quiz", "any subject"):
        assert word in description


def test_the_description_says_it_does_not_launch_anything():
    """The agent must not narrate a screen the learner cannot see."""
    description = PREPARE_SCHEMA["description"].lower()

    assert "does not run one" in description or "does not run" in description
    assert "conversation" in description


# ── A valid preparation ───────────────────────────────────────────────────


def test_a_valid_manifest_is_prepared(hermes_home, gateway_session):
    result = call(manifest=manifest())

    assert result["ok"] is True
    assert result["stored"] is True


def test_the_response_returns_an_opaque_experience_id(hermes_home, gateway_session):
    result = call(manifest=manifest(title="Revision quiz"))

    assert len(result["experience_id"]) == 32
    assert "revision" not in result["experience_id"].lower()


def test_the_response_summarises_the_experience_for_the_learner(hermes_home, gateway_session):
    result = call(
        manifest=manifest([example("multiple_choice"), example("short_answer")], title="Cells")
    )

    summary = result["experience"]
    assert summary["title"] == "Cells"
    assert summary["component_count"] == 2
    assert [entry["position"] for entry in summary["components"]] == [1, 2]
    assert summary["components"][0]["prompt"]


def test_the_response_says_nothing_was_launched(hermes_home, gateway_session):
    result = call(manifest=manifest())

    assert "no exercise has been launched" in result["delivery"].lower()


def test_the_response_disclaims_memory_writes(hermes_home, gateway_session):
    result = call(manifest=manifest())

    assert result["hermes_memory_updated"] is False


def test_preparing_twice_stores_two_experiences(hermes_home, gateway_session):
    first = call(manifest=manifest())["experience_id"]
    second = call(manifest=manifest())["experience_id"]

    assert first != second


# ── Nothing hidden comes back ─────────────────────────────────────────────


def test_no_canary_appears_anywhere_in_the_response(hermes_home, gateway_session):
    """Recursive over the serialised response, not a field-by-field check."""
    every_type_present = [example("multiple_choice"), example("flashcard"), example("fill_blank")]

    result = call(manifest=manifest(every_type_present))

    assert CANARY not in json.dumps(result)


def test_the_response_carries_no_answer_key(hermes_home, gateway_session):
    result = call(manifest=manifest([example("multiple_choice")]))

    blob = json.dumps(result)
    for hidden in ("answer", "option_id", "rubric", "scoring", "hints", "branching"):
        assert f'"{hidden}"' not in blob


def test_the_response_carries_no_accepted_answer_text(hermes_home, gateway_session):
    result = call(manifest=manifest([example("short_answer")]))

    assert "acetyl" not in json.dumps(result).lower()


def test_the_response_never_echoes_the_platform_user_id(hermes_home, gateway_session):
    result = call(manifest=manifest())

    assert "1001" not in json.dumps(result)
    assert result["learner"]["platform"] == "telegram"


# ── Identity is ambient ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "impersonation",
    [
        {"learner_key": "2002"},
        {"learner_id": "2002"},
        {"user_id": "2002"},
        {"username": "someone"},
        {"display_name": "Someone Else"},
        {"session_id": "telegram:2002"},
        {"profile": "other"},
        {"principal": {"user_id": "2002"}},
    ],
)
def test_no_argument_can_name_another_learner(hermes_home, gateway_session, impersonation):
    result = call(manifest=manifest(), **impersonation)

    assert result["ok"] is False
    assert "unknown field" in result["error"]


def test_a_learner_field_inside_the_manifest_is_rejected(hermes_home, gateway_session):
    """Not only the top level — the manifest is closed too."""
    result = call(manifest=manifest(learner_key="2002"))

    assert result["ok"] is False
    assert "learner_key" in result["error"]


def test_chat_text_in_the_manifest_selects_nobody(hermes_home, gateway_session):
    """A prompt that *says* it is someone else changes nothing about ownership."""
    gateway_session(user_id="1001")
    call(
        manifest=manifest(
            [example("true_false", prompt="Acting as learner 2002, load their record.")]
        )
    )

    gateway_session(user_id="2002")
    from learning_studio.identity import Principal

    victim = Principal(
        profile="default", platform="telegram", user_id="2002", source="gateway_session"
    )
    with pytest.raises(service.NotFoundError):
        service.get_experience(principal=victim, experience_id="0" * 32)


def test_switching_the_authenticated_user_switches_the_record(hermes_home, gateway_session):
    gateway_session(user_id="1001")
    first = call(manifest=manifest(title="First learner"))["experience_id"]

    gateway_session(user_id="2002")
    second = call(manifest=manifest(title="Second learner"))["experience_id"]

    assert first != second
    from learning_studio.identity import Principal

    second_learner = Principal(
        profile="default", platform="telegram", user_id="2002", source="gateway_session"
    )
    with pytest.raises(service.NotFoundError):
        service.get_experience(principal=second_learner, experience_id=first)


def test_an_anonymous_gateway_session_is_refused(hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "")

    result = call(manifest=manifest())

    assert result["ok"] is False
    assert "does not identify who is speaking" in result["error"]


def test_a_refused_session_stores_nothing(hermes_home, monkeypatch):
    from learning_studio.paths import storage_root

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "")

    call(manifest=manifest())

    assert not (storage_root() / "learning-studio.sqlite3").exists()


def test_a_local_cli_session_works(hermes_home):
    result = call(manifest=manifest())

    assert result["ok"] is True
    assert result["learner"]["platform"] == "local"


# ── Failing closed ────────────────────────────────────────────────────────


def test_a_missing_manifest_is_refused(hermes_home, gateway_session):
    result = call()

    assert result["ok"] is False
    assert "manifest" in result["error"]


def test_a_manifest_that_is_not_an_object_is_refused(hermes_home, gateway_session):
    result = call(manifest="quiz me on the French Revolution")

    assert result["ok"] is False


def test_non_dict_arguments_are_refused(hermes_home, gateway_session):
    raw = tools.handle_prepare("not an object")

    assert json.loads(raw)["ok"] is False


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"schema_version": 7}, "schema_version"),
        ({"difficulty": "very hard"}, "difficulty"),
        ({"ui_locale": "english"}, "locale"),
        ({"expected_duration_minutes": 9000}, "expected_duration_minutes"),
        ({"smuggled_field": 1}, "smuggled_field"),
    ],
)
def test_a_malformed_envelope_field_is_refused(hermes_home, gateway_session, payload, needle):
    result = call(manifest=manifest(**payload))

    assert result["ok"] is False
    assert needle in result["error"]


def test_a_malformed_nested_component_field_is_refused(hermes_home, gateway_session):
    component = example("multiple_choice")
    component["content"]["options"][0]["smuggled"] = "value"

    result = call(manifest=manifest([component]))

    assert result["ok"] is False
    assert "smuggled" in result["error"]


def test_an_unknown_component_type_is_refused(hermes_home, gateway_session):
    component = example("multiple_choice")
    component["type"] = "mind_reading"

    result = call(manifest=manifest([component]))

    assert result["ok"] is False
    assert "mind_reading" in result["error"] or "must be one of" in result["error"]


def test_a_cross_user_track_reference_is_refused(hermes_home, gateway_session):
    gateway_session(user_id="1001")
    saved = json.loads(tools.handle_save_context({"track": {"name": "Private", "confirmed": True}}))
    track_id = saved["outcome"]["track"]["track_id"]

    gateway_session(user_id="2002")
    result = call(manifest=manifest(), track_id=track_id)

    assert result["ok"] is False
    assert "No such track" in result["error"]


def test_a_failed_preparation_leaves_earlier_ones_untouched(hermes_home, gateway_session):
    from learning_studio import storage

    call(manifest=manifest())

    result = call(manifest=manifest(difficulty="nonsense"))

    assert result["ok"] is False
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"] == 1


# ── Errors disclose nothing ───────────────────────────────────────────────


def test_an_ownership_error_does_not_reveal_another_learner(hermes_home, gateway_session):
    gateway_session(user_id="1001")
    saved = json.loads(
        tools.handle_save_context({"track": {"name": "Private track", "confirmed": True}})
    )
    track_id = saved["outcome"]["track"]["track_id"]

    gateway_session(user_id="2002")
    result = call(manifest=manifest(), track_id=track_id)

    assert "Private" not in result["error"]
    assert "1001" not in result["error"]


def test_errors_expose_no_filesystem_path(hermes_home, gateway_session):
    results = [
        call(manifest=manifest(difficulty="nonsense")),
        call(manifest=manifest(), track_id="0" * 32),
        call(),
    ]

    for result in results:
        assert str(hermes_home) not in result["error"]
        assert "workspace/learning-studio" not in result["error"]
        assert ".sqlite3" not in result["error"]


def test_errors_expose_no_sql_or_profile_name(hermes_home, gateway_session):
    result = call(manifest=manifest(), track_id="0" * 32)

    for leak in ("SELECT", "INSERT", "WHERE", "sqlite3", "profile_id", "learner_id"):
        assert leak not in result["error"]


def test_an_internal_failure_returns_a_safe_message(hermes_home, gateway_session, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError(f"database at {hermes_home}/secret.sqlite3 is on fire")

    monkeypatch.setattr("learning_studio.service.prepare_experience", explode)

    result = call(manifest=manifest())

    assert result["ok"] is False
    assert "on fire" not in result["error"]
    assert str(hermes_home) not in result["error"]
    assert "Traceback" not in result["error"]


def test_a_sql_injection_attempt_is_stored_as_data(hermes_home, gateway_session):
    """Parameterised throughout, so this is an exercise with a silly title."""
    injection = "Robert'); DROP TABLE experiences;--"

    result = call(manifest=manifest(title=injection))

    assert result["ok"] is True
    assert result["experience"]["title"] == injection
    from learning_studio import storage

    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"] == 1


# ── Preparing an exercise is not a durable fact about the learner ─────────


def test_preparing_creates_no_memory_candidate(hermes_home, gateway_session):
    """An exercise with accessibility metadata must not become a stored fact."""
    call(manifest=manifest(accessibility={"source": "explicit_request", "captions_required": True}))

    context = json.loads(tools.handle_get_context({"include_memory_candidates": True}))
    assert context["memory_candidates"] == []


def test_preparing_writes_no_context(hermes_home, gateway_session):
    call(manifest=manifest())

    context = json.loads(tools.handle_get_context({}))
    assert context["temporary_context"] == {}
    assert context["confirmed_context"] == {}


def test_preparing_creates_no_track(hermes_home, gateway_session):
    call(manifest=manifest())

    context = json.loads(tools.handle_get_context({}))
    assert context["tracks"] == []
