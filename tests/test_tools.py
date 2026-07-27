"""The registered tool surface: schemas, handler contracts, and failure modes.

Handlers are the only part of this plugin an LLM touches directly, so they
are tested the way an LLM will use them: with plausible arguments, wrong
types, extra keys, and someone else's IDs.
"""

from __future__ import annotations

import json

import pytest

from learning_studio import tools
from learning_studio.schemas import TOOL_SCHEMAS


def _call(handler, **params) -> dict:
    raw = handler(params)
    assert isinstance(raw, str), "handlers must return a JSON string"
    return json.loads(raw)


# ── Registration ──────────────────────────────────────────────────────────


def test_exactly_two_tools_are_registered(ctx):
    from learning_studio import register

    register(ctx)

    assert len(ctx.tools) == 2


def test_the_tool_names_are_the_agreed_ones(ctx):
    from learning_studio import register

    register(ctx)

    assert sorted(tool.name for tool in ctx.tools) == [
        "learning_studio_get_context",
        "learning_studio_save_context",
    ]


def test_every_tool_is_in_the_reserved_toolset(ctx):
    from learning_studio import register

    register(ctx)

    assert {tool.toolset for tool in ctx.tools} == {"plugin_learning_studio"}


def test_every_handler_is_callable_and_returns_json(ctx, hermes_home, gateway_session):
    from learning_studio import register

    register(ctx)

    for tool in ctx.tools:
        result = tool.handler({})
        assert isinstance(result, str)
        assert isinstance(json.loads(result), dict)


def test_handlers_tolerate_the_kwargs_hermes_passes(ctx, hermes_home, gateway_session):
    """The host calls ``handler(args, **kwargs)`` with context it may extend."""
    from learning_studio import register

    register(ctx)

    for tool in ctx.tools:
        result = tool.handler({}, session_id="abc", task_id="def", agent=object())
        assert json.loads(result)["ok"] is True


# ── Schemas ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(TOOL_SCHEMAS))
def test_schema_has_the_fields_the_host_requires(name: str):
    schema = TOOL_SCHEMAS[name]

    assert schema["name"] == name
    assert schema["description"].strip()
    assert schema["parameters"]["type"] == "object"


@pytest.mark.parametrize("name", sorted(TOOL_SCHEMAS))
def test_schema_is_json_serialisable(name: str):
    """A schema that cannot be serialised cannot reach the model."""
    assert json.loads(json.dumps(TOOL_SCHEMAS[name]))


@pytest.mark.parametrize("name", sorted(TOOL_SCHEMAS))
def test_every_object_refuses_additional_properties(name: str):
    """A typo must be an error, not a silently dropped field."""
    lax: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            if (
                node.get("type") == "object"
                and "properties" in node
                and node.get("additionalProperties") is not False
            ):
                lax.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(TOOL_SCHEMAS[name]["parameters"], name)
    assert lax == []


@pytest.mark.parametrize("name", sorted(TOOL_SCHEMAS))
def test_no_parameter_names_a_learner(name: str):
    """Identity is ambient. An argument naming a learner is an impersonation vector."""
    blob = json.dumps(TOOL_SCHEMAS[name]).lower()

    for forbidden in ("learner_key", "learner_id", "user_id", "username", "principal"):
        assert forbidden not in blob, f"{name} exposes '{forbidden}' as an argument"


@pytest.mark.parametrize("name", sorted(TOOL_SCHEMAS))
def test_every_string_and_array_is_bounded(name: str):
    """Unbounded input is a denial-of-service vector and a storage problem."""
    unbounded: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and "maxLength" not in node and "enum" not in node:
                unbounded.append(f"{path} (string)")
            if node.get("type") == "array" and "maxItems" not in node:
                unbounded.append(f"{path} (array)")
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(TOOL_SCHEMAS[name]["parameters"], name)
    assert unbounded == []


#: Parameter names that would mean the tool accepts a path, code, or SQL.
#: Matched against property *names* only — "description" legitimately
#: contains "script", and prose about not accepting SQL is a feature.
FORBIDDEN_PARAM_WORDS = (
    "path",
    "file",
    "dir",
    "sql",
    "query",
    "script",
    "command",
    "cmd",
    "exec",
    "eval",
    "code",
    "url",
    "token",
)


@pytest.mark.parametrize("name", sorted(TOOL_SCHEMAS))
def test_no_parameter_accepts_a_path_code_or_sql(name: str):
    """There must be no parameter through which a caller could reach outside."""
    offenders: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for prop in node.get("properties", {}):
                lowered = prop.lower()
                for word in FORBIDDEN_PARAM_WORDS:
                    if word in lowered:
                        offenders.append(f"{path}.{prop} (matches '{word}')")
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(TOOL_SCHEMAS[name]["parameters"], name)
    assert offenders == []


def test_the_parameter_name_guard_would_catch_a_real_offender():
    """Proves the check above discriminates rather than passing vacuously."""
    offenders = [
        prop
        for prop in {"source_file_path": {}, "learner_key": {}}
        if any(word in prop.lower() for word in FORBIDDEN_PARAM_WORDS)
    ]

    assert offenders == ["source_file_path"]


@pytest.mark.parametrize("name", sorted(TOOL_SCHEMAS))
def test_schemas_stay_subject_neutral(name: str):
    blob = json.dumps(TOOL_SCHEMAS[name]).lower()

    for subject in (
        "spanish",
        "japanese",
        "python",
        "chemistry",
        "history",
        "vocabulary",
        "verb",
        "grammar",
    ):
        assert subject not in blob, f"{name} presumes the subject '{subject}'"


# ── Handler behaviour ─────────────────────────────────────────────────────


def test_get_returns_an_empty_context_for_a_first_time_learner(hermes_home, gateway_session):
    result = _call(tools.handle_get_context)

    assert result["ok"] is True
    assert result["tracks"] == []
    assert result["temporary_context"] == {}


def test_save_then_get_round_trips_through_the_handlers(hermes_home, gateway_session):
    saved = _call(
        tools.handle_save_context,
        track={"name": "Round trip", "confirmed": True, "context": {"goal": "a goal"}},
    )
    assert saved["outcome"]["track"]["status"] == "created"

    result = _call(tools.handle_get_context)
    assert result["confirmed_context"]["goal"]["value"] == "a goal"


def test_the_save_response_always_disclaims_memory_writes(hermes_home, gateway_session):
    result = _call(tools.handle_save_context)

    assert result["hermes_memory_updated"] is False


def test_the_response_never_echoes_the_platform_user_id(hermes_home, gateway_session):
    """Tool results become conversation history; identifiers must not ride along."""
    result = _call(tools.handle_get_context)

    assert "1001" not in json.dumps(result)
    assert result["learner"]["platform"] == "telegram"


# ── Identity cannot be supplied or overridden ─────────────────────────────


@pytest.mark.parametrize(
    "impersonation",
    [
        {"learner_key": "2002"},
        {"learner_id": "2002"},
        {"user_id": "2002"},
        {"principal": {"user_id": "2002"}},
        {"profile": "other"},
    ],
)
def test_no_argument_can_name_another_learner(hermes_home, gateway_session, impersonation):
    """The whole class of attack the previous design was open to."""
    result = _call(tools.handle_get_context, **impersonation)

    assert result["ok"] is False
    assert "unknown field" in result["error"]


def test_a_supplied_identity_cannot_reach_another_learners_data(hermes_home, gateway_session):
    gateway_session(user_id="1001")
    _call(
        tools.handle_save_context,
        track={"name": "Private", "confirmed": True, "context": {"goal": "secret goal"}},
    )

    gateway_session(user_id="2002")
    # Even naming the victim exactly as the old API would have accepted.
    stolen = _call(tools.handle_get_context, learner_key="1001")
    assert stolen["ok"] is False

    own = _call(tools.handle_get_context)
    assert own["tracks"] == [], "the second principal saw the first principal's track"


def test_switching_the_authenticated_user_switches_the_record(hermes_home, gateway_session):
    gateway_session(user_id="1001")
    _call(tools.handle_save_context, temporary_context={"goal": "first learner"})

    gateway_session(user_id="2002")
    _call(tools.handle_save_context, temporary_context={"goal": "second learner"})

    assert _call(tools.handle_get_context)["temporary_context"]["goal"]["value"] == (
        "second learner"
    )

    gateway_session(user_id="1001")
    assert _call(tools.handle_get_context)["temporary_context"]["goal"]["value"] == (
        "first learner"
    )


def test_an_anonymous_gateway_session_is_refused(hermes_home, monkeypatch):
    """A multi-user host that will not say who is speaking gets nothing stored."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "")

    result = _call(tools.handle_save_context, temporary_context={"goal": "whose goal?"})

    assert result["ok"] is False
    assert "does not identify who is speaking" in result["error"]


def test_a_local_cli_session_works_without_a_platform_user(hermes_home):
    """No gateway means one local operator; the profile is the principal."""
    result = _call(tools.handle_save_context, temporary_context={"goal": "local goal"})

    assert result["ok"] is True
    assert result["learner"]["platform"] == "local"


# ── Failing closed ────────────────────────────────────────────────────────


def test_an_unknown_argument_is_refused(hermes_home, gateway_session):
    result = _call(tools.handle_get_context, sneaky_extra="value")

    assert result["ok"] is False
    assert "sneaky_extra" in result["error"]


def test_an_unknown_nested_property_is_refused(hermes_home, gateway_session):
    """``additionalProperties: false`` has to mean something at runtime."""
    result = _call(
        tools.handle_save_context,
        track={"name": "T", "confirmed": True, "smuggled": "value"},
    )

    assert result["ok"] is False
    assert "smuggled" in result["error"]


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"corrections": [{"field": "goal", "value": "x", "extra": 1}]}, "extra"),
        ({"objectives": [{"behavior": "b", "condition": "c", "standard": "s", "x": 1}]}, "x"),
        (
            {
                "memory_candidates": [
                    {
                        "category": "durable_preference",
                        "statement": "s",
                        "evidence_summary": "e",
                        "origin": "explicit_durable_preference",
                        "bogus": True,
                    }
                ]
            },
            "bogus",
        ),
        ({"temporary_context": {"not_a_field": "x"}}, "not_a_field"),
        ({"accessibility_consent": {"consent_statement": "s", "needs": ["n"], "z": 1}}, "z"),
    ],
)
def test_unknown_nested_properties_are_refused_everywhere(
    hermes_home, gateway_session, payload, needle
):
    result = _call(tools.handle_save_context, **payload)

    assert result["ok"] is False
    assert needle in result["error"]


def test_an_oversized_array_is_refused(hermes_home, gateway_session):
    """26 items where the schema advertises 25."""
    result = _call(
        tools.handle_save_context,
        corrections=[{"field": "goal", "value": f"v{i}"} for i in range(26)],
    )

    assert result["ok"] is False
    assert "at most 25" in result["error"]


def test_an_oversized_context_list_is_refused(hermes_home, gateway_session):
    result = _call(
        tools.handle_save_context,
        temporary_context={"interests": [f"i{n}" for n in range(30)]},
    )

    assert result["ok"] is False
    assert "at most" in result["error"]


def test_an_overlong_objective_string_is_refused(hermes_home, gateway_session):
    result = _call(
        tools.handle_save_context,
        track={"name": "T", "confirmed": True},
        objectives=[{"behavior": "b" * 600, "condition": "c", "standard": "s"}],
    )

    assert result["ok"] is False
    assert "at most 500" in result["error"]


def test_an_overlong_context_value_is_refused(hermes_home, gateway_session):
    result = _call(tools.handle_save_context, temporary_context={"goal": "g" * 5000})

    assert result["ok"] is False
    assert "at most 2000" in result["error"]


def test_a_boolean_where_an_integer_belongs_is_refused(hermes_home, gateway_session):
    """``True`` is an int in Python; reading it as 1 would satisfy a threshold."""
    result = _call(
        tools.handle_save_context,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "s",
                "evidence_summary": "e",
                "origin": "repeated_evidence",
                "evidence_count": True,
            }
        ],
    )

    assert result["ok"] is False
    assert "integer" in result["error"]


def test_an_invalid_enum_value_is_refused(hermes_home, gateway_session):
    result = _call(tools.handle_save_context, track={"name": "T", "status": "deleted"})

    assert result["ok"] is False
    assert "must be one of" in result["error"]


def test_a_wrong_type_is_refused_rather_than_coerced(hermes_home, gateway_session):
    result = _call(tools.handle_save_context, corrections="not a list")

    assert result["ok"] is False
    assert "corrections" in result["error"]


def test_non_dict_arguments_are_refused(hermes_home, gateway_session):
    raw = tools.handle_get_context("not an object")

    assert json.loads(raw)["ok"] is False


def test_a_failed_save_writes_nothing(hermes_home, gateway_session):
    """One bad field must not leave half a track behind."""
    result = _call(
        tools.handle_save_context,
        track={"name": "Half written", "confirmed": True, "context": {"goal": "ok"}},
        corrections=[{"field": "not_a_field", "value": "x"}],
    )
    assert result["ok"] is False

    after = _call(tools.handle_get_context)
    assert after["tracks"] == [], "the transaction did not roll back"


def test_validation_happens_before_the_database_is_touched(hermes_home, gateway_session):
    """A malformed request must not even create the storage root."""
    from learning_studio.paths import storage_root

    result = _call(tools.handle_save_context, bogus_argument=1)

    assert result["ok"] is False
    assert not (storage_root() / "learning-studio.sqlite3").exists()


# ── Errors disclose nothing ───────────────────────────────────────────────


def test_an_ownership_error_does_not_reveal_another_learner(hermes_home, gateway_session):
    gateway_session(user_id="1001")
    created = _call(tools.handle_save_context, track={"name": "Private track", "confirmed": True})
    track_id = created["outcome"]["track"]["track_id"]

    gateway_session(user_id="2002")
    result = _call(tools.handle_get_context, track_id=track_id)

    assert result["ok"] is False
    assert "Private" not in result["error"]
    assert "1001" not in result["error"]


def test_errors_expose_no_filesystem_path(hermes_home, gateway_session):
    results = [
        _call(tools.handle_get_context, track_id="0" * 32),
        _call(tools.handle_save_context, temporary_context={"goal": ""}),
    ]

    for result in results:
        assert str(hermes_home) not in result["error"]
        assert "workspace/learning-studio" not in result["error"]
        assert ".sqlite3" not in result["error"]


def test_errors_expose_no_sql(hermes_home, gateway_session):
    result = _call(
        tools.handle_save_context,
        objectives=[{"track_id": "0" * 32, "behavior": "b", "condition": "c", "standard": "s"}],
    )

    assert result["ok"] is False
    for sql in ("SELECT", "INSERT", "UPDATE", "WHERE", "sqlite3"):
        assert sql not in result["error"]


def test_a_sql_injection_attempt_is_stored_as_data_not_executed(hermes_home, gateway_session):
    """Parameterised throughout, so this is a track with a silly name."""
    injection = "Robert'); DROP TABLE tracks;--"
    saved = _call(tools.handle_save_context, track={"name": injection, "confirmed": True})
    assert saved["outcome"]["track"]["status"] == "created"

    result = _call(tools.handle_get_context)
    assert result["tracks"][0]["name"] == injection


def test_an_internal_failure_returns_a_safe_message(hermes_home, gateway_session, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError(f"database at {hermes_home}/secret.sqlite3 is on fire")

    monkeypatch.setattr("learning_studio.service.get_context", explode)

    result = _call(tools.handle_get_context)

    assert result["ok"] is False
    assert "on fire" not in result["error"]
    assert str(hermes_home) not in result["error"]
    assert "could not complete" in result["error"]
