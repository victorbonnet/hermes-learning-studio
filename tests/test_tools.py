"""The registered tool surface: schemas, handler contracts, and failure modes.

Handlers are the only part of this plugin an LLM touches directly, so they
are tested the way an LLM will use them: with plausible arguments, wrong
types, extra keys, and someone else's IDs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from learning_studio import tools
from learning_studio.schemas import TOOL_SCHEMAS

LEARNER = "user-7007"


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


def test_every_handler_is_callable_and_returns_json(ctx, hermes_home: Path):
    from learning_studio import register

    register(ctx)

    for tool in ctx.tools:
        result = tool.handler({"learner_key": LEARNER})
        assert isinstance(result, str)
        assert isinstance(json.loads(result), dict)


def test_handlers_tolerate_the_kwargs_hermes_passes(ctx, hermes_home: Path):
    """The host calls ``handler(args, **kwargs)`` with context it may extend."""
    from learning_studio import register

    register(ctx)

    for tool in ctx.tools:
        result = tool.handler(
            {"learner_key": LEARNER}, session_id="abc", task_id="def", agent=object()
        )
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
def test_learner_key_is_required(name: str):
    assert TOOL_SCHEMAS[name]["parameters"]["required"] == ["learner_key"]


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


def test_get_returns_an_empty_context_for_an_unknown_learner(hermes_home: Path):
    result = _call(tools.handle_get_context, learner_key="nobody-here")

    assert result["ok"] is True
    assert result["tracks"] == []
    assert result["temporary_context"] == {}


def test_save_then_get_round_trips_through_the_handlers(hermes_home: Path):
    saved = _call(
        tools.handle_save_context,
        learner_key=LEARNER,
        track={"name": "Round trip", "confirmed": True, "context": {"goal": "a goal"}},
    )
    assert saved["outcome"]["track"]["status"] == "created"

    result = _call(tools.handle_get_context, learner_key=LEARNER)
    assert result["confirmed_context"]["goal"]["value"] == "a goal"


def test_the_save_response_always_disclaims_memory_writes(hermes_home: Path):
    result = _call(tools.handle_save_context, learner_key=LEARNER)

    assert result["hermes_memory_updated"] is False


# ── Failing closed ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"learner_key": ""},
        {"learner_key": "   "},
        {"learner_key": None},
        {"learner_key": 12345},
        {"learner_key": ["a"]},
        {"learner_key": "has spaces"},
        {"learner_key": "line\nbreak"},
        {"learner_key": "x" * 300},
    ],
)
def test_a_bad_learner_key_fails_closed(hermes_home: Path, params: dict):
    result = _call(tools.handle_get_context, **params)

    assert result["ok"] is False
    assert result["error"]


def test_an_unknown_argument_is_refused(hermes_home: Path):
    result = _call(tools.handle_get_context, learner_key=LEARNER, sneaky_extra="value")

    assert result["ok"] is False
    assert "sneaky_extra" in result["error"]


def test_a_wrong_type_is_refused_rather_than_coerced(hermes_home: Path):
    result = _call(tools.handle_save_context, learner_key=LEARNER, corrections="not a list")

    assert result["ok"] is False
    assert "corrections" in result["error"]


def test_an_unknown_context_field_is_refused(hermes_home: Path):
    result = _call(
        tools.handle_save_context,
        learner_key=LEARNER,
        temporary_context={"favourite_colour": "blue"},
    )

    assert result["ok"] is False
    assert "favourite_colour" in result["error"]


def test_non_dict_arguments_are_refused(hermes_home: Path):
    raw = tools.handle_get_context("not an object")

    assert json.loads(raw)["ok"] is False


def test_a_failed_save_writes_nothing(hermes_home: Path):
    """One bad field must not leave half a track behind."""
    result = _call(
        tools.handle_save_context,
        learner_key=LEARNER,
        track={"name": "Half written", "confirmed": True, "context": {"goal": "ok"}},
        corrections=[{"field": "not_a_field", "value": "x"}],
    )
    assert result["ok"] is False

    after = _call(tools.handle_get_context, learner_key=LEARNER)
    assert after["tracks"] == [], "the transaction did not roll back"


# ── Errors disclose nothing ───────────────────────────────────────────────


def test_an_ownership_error_does_not_reveal_another_learner(hermes_home: Path):
    created = _call(
        tools.handle_save_context,
        learner_key="owner-1",
        track={"name": "Private track", "confirmed": True},
    )
    track_id = created["outcome"]["track"]["track_id"]

    result = _call(tools.handle_get_context, learner_key="intruder-2", track_id=track_id)

    assert result["ok"] is False
    assert "Private" not in result["error"]
    assert "owner-1" not in result["error"]


def test_errors_expose_no_filesystem_path(hermes_home: Path):
    results = [
        _call(tools.handle_get_context, learner_key="!!!invalid!!!"),
        _call(tools.handle_save_context, learner_key="!!!invalid!!!"),
    ]

    for result in results:
        assert str(hermes_home) not in result["error"]
        assert "workspace/learning-studio" not in result["error"]
        assert ".sqlite3" not in result["error"]


def test_errors_expose_no_sql(hermes_home: Path):
    result = _call(
        tools.handle_save_context,
        learner_key=LEARNER,
        objectives=[{"track_id": "0" * 32, "behavior": "b", "condition": "c", "standard": "s"}],
    )

    assert result["ok"] is False
    for sql in ("SELECT", "INSERT", "UPDATE", "WHERE", "sqlite3"):
        assert sql not in result["error"]


def test_a_sql_injection_attempt_is_stored_as_data_not_executed(hermes_home: Path):
    """Parameterised throughout, so this is a track with a silly name."""
    injection = "Robert'); DROP TABLE tracks;--"
    saved = _call(
        tools.handle_save_context,
        learner_key=LEARNER,
        track={"name": injection, "confirmed": True},
    )
    assert saved["outcome"]["track"]["status"] == "created"

    result = _call(tools.handle_get_context, learner_key=LEARNER)
    assert result["tracks"][0]["name"] == injection


def test_an_internal_failure_returns_a_safe_message(hermes_home: Path, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError(f"database at {hermes_home}/secret.sqlite3 is on fire")

    monkeypatch.setattr("learning_studio.service.get_context", explode)

    result = _call(tools.handle_get_context, learner_key=LEARNER)

    assert result["ok"] is False
    assert "on fire" not in result["error"]
    assert str(hermes_home) not in result["error"]
    assert "could not complete" in result["error"]
