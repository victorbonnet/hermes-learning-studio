"""The eight tools, as the model and the host actually see them.

The question throughout is not "does this work" — that is tested where the
behaviour lives — but "what can a caller reach from here". A tool schema is the
plugin's boundary against a confused or adversarial model, and the four runtime
tools are the ones whose payloads could, if they carried the wrong field, name
a process, a port, or somebody else's conversation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from learning_studio.plugin import RUNTIME_TOOLS, TOOLSET_NAME
from learning_studio.schemas import TOOL_SCHEMAS

RUNTIME_TOOL_NAMES = sorted(RUNTIME_TOOLS)

ALL_TOOLS = [
    "learning_studio_get_context",
    "learning_studio_import_asset",
    "learning_studio_launch",
    "learning_studio_prepare",
    "learning_studio_results",
    "learning_studio_save_context",
    "learning_studio_status",
    "learning_studio_stop",
]


@pytest.fixture
def registered(ctx):
    from learning_studio import register

    register(ctx)
    return ctx


# ── The registered surface ────────────────────────────────────────────────


def test_exactly_these_eight_tools_are_registered(registered):
    assert sorted(tool.name for tool in registered.tools) == ALL_TOOLS


def test_every_tool_is_in_the_one_reserved_toolset(registered):
    assert {tool.toolset for tool in registered.tools} == {TOOLSET_NAME}


def test_the_manifest_declares_exactly_what_is_registered(registered, repo_root: Path):
    """A manifest that lags the code is how an operator's allowlist goes stale."""
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((repo_root / "plugin.yaml").read_text(encoding="utf-8"))

    assert sorted(manifest["provides_tools"]) == ALL_TOOLS
    assert manifest["provides_hooks"] == ["pre_gateway_dispatch"]


def test_the_manifest_requires_no_environment_variable(repo_root: Path):
    """A non-empty `requires_env` gates the *whole plugin* off when unset.

    Declaring the bot token there would take the context, manifest, and asset
    tools away from every profile that never launches anything.
    """
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((repo_root / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["requires_env"] == []


def test_registration_registers_one_observe_only_hook_and_no_commands(registered):
    from learning_studio.plugin import CONSENT_EVIDENCE_HOOK

    assert [name for name, _ in registered.hooks] == [CONSENT_EVIDENCE_HOOK]
    assert registered.commands == []
    assert registered.cli_commands == []


# ── Readiness gating ──────────────────────────────────────────────────────


def test_only_the_process_managing_tools_are_gated(registered):
    gated = sorted(tool.name for tool in registered.tools if tool.check_fn is not None)

    assert gated == RUNTIME_TOOL_NAMES


def test_the_gate_is_the_platform_and_nothing_else(registered):
    """A missing runtime environment must not make the tools disappear.

    An agent cannot explain the absence of a tool it cannot see. Everything
    that an operator could fix is reported by the handler instead.
    """
    from learning_studio.runtime.availability import runtime_tools_supported

    for tool in registered.tools:
        if tool.check_fn is not None:
            assert tool.check_fn is runtime_tools_supported


def test_the_gate_is_cheap_and_has_no_side_effects(registered, tmp_path, monkeypatch):
    """Hermes calls it repeatedly and caches the answer."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "untouched"))
    gate = next(tool.check_fn for tool in registered.tools if tool.check_fn)

    assert gate() == gate()
    assert not (tmp_path / "untouched").exists(), "the readiness check created state"


# ── What a payload may carry ──────────────────────────────────────────────


def flattened(node) -> str:
    return json.dumps(node)


@pytest.mark.parametrize("name", RUNTIME_TOOL_NAMES)
def test_a_runtime_schema_is_closed(name: str):
    parameters = TOOL_SCHEMAS[name]["parameters"]

    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False


@pytest.mark.parametrize("name", RUNTIME_TOOL_NAMES)
def test_no_runtime_schema_offers_a_machine_or_a_destination(name: str):
    """The whole security story of these tools, as one assertion.

    Every value that decides where a process listens, what it runs, how long it
    waits, or who hears about it comes from the operator's config.yaml or from
    Hermes' own session context. None of them has a property here, so the model
    cannot supply one — not by naming it, not by mistyping it, and not by
    persuading somebody to paste one into the conversation.
    """
    properties = set(TOOL_SCHEMAS[name]["parameters"].get("properties", {}))

    forbidden = {
        "host",
        "hostname",
        "address",
        "bind",
        "port",
        "url",
        "public_url",
        "tunnel_url",
        "executable",
        "command",
        "argv",
        "args",
        "path",
        "cloudflared_path",
        "pid",
        "process_id",
        "signal",
        "lock_path",
        "env",
        "environment",
        "timeout",
        "timeout_seconds",
        "idle_timeout_seconds",
        "max_lifetime_seconds",
        "chat_id",
        "telegram_user_id",
        "user_id",
        "learner_key",
        "bot_token",
        "token",
        "profile",
        "generation",
        "session_token",
    }

    assert properties & forbidden == set(), sorted(properties & forbidden)


@pytest.mark.parametrize("name", ["learning_studio_status", "learning_studio_stop"])
def test_the_no_argument_tools_accept_nothing(name: str):
    parameters = TOOL_SCHEMAS[name]["parameters"]

    assert parameters["properties"] == {}
    assert parameters["additionalProperties"] is False


def test_launch_requires_an_experience_an_initiation_and_the_learner_words():
    parameters = TOOL_SCHEMAS["learning_studio_launch"]["parameters"]

    assert sorted(parameters["required"]) == ["experience_id", "initiation", "learner_quote"]
    assert parameters["properties"]["initiation"]["enum"] == [
        "learner_request",
        "agent_suggestion",
    ]


def test_the_schema_itself_refuses_an_incomplete_suggestion():
    """Encoded in JSON Schema as well as in the handler.

    A provider that validates before dispatch must refuse the same payloads
    this code would, or the two disagree about what a valid call is.
    """
    from learning_studio.validation import SchemaViolation, validate

    parameters = TOOL_SCHEMAS["learning_studio_launch"]["parameters"]

    with pytest.raises(SchemaViolation):
        validate(
            {
                "experience_id": "e",
                "initiation": "agent_suggestion",
                "learner_quote": "go on then",
            },
            parameters,
        )

    validate(
        {
            "experience_id": "e",
            "initiation": "agent_suggestion",
            "learner_confirmed": True,
            "learner_quote": "go on then",
        },
        parameters,
    )


def test_every_runtime_string_is_bounded():
    for name in RUNTIME_TOOL_NAMES:
        for field, schema in TOOL_SCHEMAS[name]["parameters"].get("properties", {}).items():
            if schema.get("type") != "string":
                continue
            if "enum" in schema:
                # An enumeration is bounded by its own membership; a maxLength
                # beside it would be a second, weaker statement of the same rule.
                assert schema["enum"], f"{name}.{field} enumerates nothing"
                continue
            assert "maxLength" in schema, f"{name}.{field}"
            assert schema["maxLength"] <= 4096, f"{name}.{field}"


@pytest.mark.parametrize("name", RUNTIME_TOOL_NAMES)
def test_a_runtime_description_is_honest_about_scoring(name: str):
    """Every one of these is a place an agent could assume marks exist."""
    description = TOOL_SCHEMAS[name]["description"].lower()

    assert "score" in description or "performance" in description


def test_the_launch_description_forbids_claiming_success_without_one():
    description = TOOL_SCHEMAS["learning_studio_launch"]["description"].lower()

    assert "button_delivered" in description
    assert "conversation" in description


# ── The handlers ──────────────────────────────────────────────────────────


def test_every_handler_returns_a_json_object_for_an_empty_payload(registered, hermes_home):
    for tool in registered.tools:
        result = tool.handler({})

        assert isinstance(result, str)
        assert isinstance(json.loads(result), dict)


def test_every_handler_returns_a_json_object_for_no_payload_at_all(registered, hermes_home):
    for tool in registered.tools:
        assert isinstance(json.loads(tool.handler()), dict)


@pytest.mark.parametrize("name", RUNTIME_TOOL_NAMES)
def test_an_invented_field_is_refused_rather_than_ignored(registered, hermes_home, name: str):
    handler = next(tool.handler for tool in registered.tools if tool.name == name)

    result = json.loads(handler({"port": 8080, "host": "0.0.0.0"}))

    assert result["ok"] is False
    assert "port" in result["error"] or "host" in result["error"]


@pytest.mark.parametrize("name", RUNTIME_TOOL_NAMES)
def test_no_handler_raises_whatever_it_is_given(registered, hermes_home, name: str):
    """An exception escaping a handler is an opaque failure the agent cannot act on."""
    handler = next(tool.handler for tool in registered.tools if tool.name == name)

    for payload in (
        None,
        {},
        [],
        "string",
        42,
        {"experience_id": "\x00"},
        {"experience_id": "x" * 5000},
    ):
        assert isinstance(json.loads(handler(payload)), dict)


def test_status_reports_prerequisites_without_naming_a_path(registered, hermes_home):
    handler = next(
        tool.handler for tool in registered.tools if tool.name == "learning_studio_status"
    )

    result = json.loads(handler({}))

    assert result["ok"] is True
    assert result["running"] is False
    assert set(result["prerequisites"]) == {
        "platform_supported",
        "runtime_prepared",
        "tunnel_available",
    }
    assert str(hermes_home) not in json.dumps(result)


def test_status_never_reports_an_address_or_a_process(registered, hermes_home):
    handler = next(
        tool.handler for tool in registered.tools if tool.name == "learning_studio_status"
    )

    body = handler({})

    for forbidden in ("127.0.0.1", "trycloudflare", "control_token", "pid", "executable"):
        assert forbidden not in body


def test_status_says_nothing_is_scored(registered, hermes_home):
    handler = next(
        tool.handler for tool in registered.tools if tool.name == "learning_studio_status"
    )

    result = json.loads(handler({}))

    assert result["scored"] is False
    assert result["attempts_stored"] is False


def test_stopping_nothing_is_a_stated_no_op(registered, hermes_home):
    handler = next(tool.handler for tool in registered.tools if tool.name == "learning_studio_stop")

    first = json.loads(handler({}))
    second = json.loads(handler({}))

    assert first == second
    assert first["ok"] is True
    assert first["stopped"] is False
    assert first["state"] == "not_running"


def test_results_for_an_unknown_experience_says_no_such_exercise(registered, hermes_home):
    handler = next(
        tool.handler for tool in registered.tools if tool.name == "learning_studio_results"
    )

    result = json.loads(handler({"experience_id": "not-a-real-id"}))

    assert result["ok"] is False
    assert "No such prepared exercise" in result["error"]


def test_launching_an_unknown_experience_starts_nothing(registered, hermes_home):
    from learning_studio.runtime import state

    handler = next(
        tool.handler for tool in registered.tools if tool.name == "learning_studio_launch"
    )

    result = json.loads(
        handler({"experience_id": "not-a-real-id", "initiation": "learner_request"})
    )

    assert result["ok"] is False
    assert state.read_record() is None


# ── Registration remains cheap ────────────────────────────────────────────


@pytest.fixture
def reimportable_package():
    """Forget this package, then put every module back exactly as it was.

    The forgetting is what gives the test below any force: with the whole
    package already imported, "registration did not reach the supervisor" is
    unfalsifiable.

    The restoring is not tidiness. Other test modules hold references to
    ``learning_studio.service`` and ``learning_studio.tools`` bound at *their*
    import time, and a fresh import replaces the objects those names point at.
    ``monkeypatch.setattr("learning_studio.service.get_context", ...)`` then
    patches a module the tool layer is no longer using, and a test asserting
    that an internal failure is reported safely quietly stops exercising the
    failure at all.
    """
    import sys

    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.split(".", 1)[0] == "learning_studio"
    }
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [n for n in sys.modules if n.split(".", 1)[0] == "learning_studio"]:
            del sys.modules[name]
        sys.modules.update(saved)


def test_registration_imports_no_runtime_machinery(ctx, reimportable_package):
    """Enabling the plugin must not import a process manager or a web server."""
    import sys

    from learning_studio import register

    register(ctx)

    reached = {name for name in sys.modules if name.startswith("learning_studio.runtime")}

    # Three modules, all standard library and all free of side effects: the
    # package marker, the readiness check, and the message constants the tool
    # layer maps failures onto.
    assert reached <= {
        "learning_studio.runtime",
        "learning_studio.runtime.availability",
        "learning_studio.runtime.errors",
    }, reached
    assert "learning_studio.runtime.server" not in sys.modules
    assert "learning_studio.runtime.supervisor" not in sys.modules
