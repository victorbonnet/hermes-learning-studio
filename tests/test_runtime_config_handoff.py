"""Parent-to-isolated-runtime configuration and authorization handoff."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from learning_studio.components import MINIMUM_REQUEST_BYTES
from learning_studio.config import LearningStudioConfig, config_to_json
from learning_studio.models import MAX_LIST_ITEMS, MAX_VALUE_CHARS
from learning_studio.runtime import environment as env
from learning_studio.runtime import server, state, supervisor
from learning_studio.runtime.errors import RuntimeUnavailable


def operator_config(**overrides) -> LearningStudioConfig:
    """A configuration in which no field is left at its default."""
    values: dict = {
        "temporary_context_ttl_hours": 12,
        "max_tracks_per_learner": 7,
        "busy_timeout_ms": 1234,
        "journal_mode": "truncate",
        "memory_candidate_min_evidence": 4,
        "allow_durable_accessibility_needs": False,
        "max_context_value_chars": 321,
        "max_asset_bytes": 2048,
        "max_asset_width": 640,
        "max_asset_height": 480,
        "max_asset_pixels": 307_200,
        "mini_app_session_ttl_seconds": 60,
        "mini_app_init_data_max_age_seconds": 45,
        "mini_app_max_request_bytes": MINIMUM_REQUEST_BYTES + 7,
        "mini_app_rate_limit_requests": 9,
        "mini_app_rate_limit_window_seconds": 11,
        "mini_app_max_sessions": 13,
        "mini_app_allowed_telegram_users": ("1001",),
        "runtime_host": "::1",
        "runtime_port": 9876,
        "runtime_readiness_timeout_seconds": 17,
        "runtime_idle_timeout_seconds": 120,
        "runtime_max_lifetime_seconds": 3600,
        "runtime_graceful_stop_seconds": 3,
        "tunnel_readiness_timeout_seconds": 19,
        "cloudflared_path": "/opt/bin/cloudflared",
        "launch_button_label": "Abrir Aula",
        "profile_context": {"explanation_language": "es", "interests": ["algebra", "geometry"]},
        "defaults": {"subject": "mathematics"},
    }
    values.update(overrides)
    return LearningStudioConfig(**values)


def record(**overrides) -> state.RuntimeRecord:
    values: dict = {
        "runtime_id": "r-1",
        "generation": 1,
        "profile": "family",
        "pid": 1,
        "host": "::1",
        "port": 1,
        "control_token": "a-control-token",
        "executable": "/x/python",
        "started_at": 0.0,
        "idle_timeout_seconds": 120,
        "max_lifetime_seconds": 3600,
    }
    values.update(overrides)
    return state.RuntimeRecord(**values)


def handed_over(config: LearningStudioConfig, **source) -> dict[str, str]:
    """The child environment the supervisor would build for this profile."""
    return supervisor.child_environment(
        record(),
        handshake=Path("/tmp/handshake.json"),
        cloudflared="",
        config=config,
        source=source,
    )


# ── The configuration survives whole ──────────────────────────────────────


def test_the_whole_operator_configuration_reaches_the_runtime():
    """Field for field, not just the two that were reported."""
    config = operator_config()

    settings = server.settings_from_environment(handed_over(config))

    assert settings.config == config
    assert settings.startup_fingerprint == supervisor.startup_fingerprint(
        config, settings.allowed_users
    )


def test_the_startup_fingerprint_covers_configuration_and_authorisation():
    config = operator_config()
    original = supervisor.startup_fingerprint(config, frozenset({"1001"}))

    assert original != supervisor.startup_fingerprint(
        replace(config, runtime_port=9877), frozenset({"1001"})
    )
    assert original != supervisor.startup_fingerprint(config, frozenset())


# ── The final allowed-user set survives whole ─────────────────────────────


def top_level(**telegram) -> dict:
    return {"platforms": {"telegram": telegram}}


@pytest.mark.parametrize(
    ("restriction", "host_config", "environment", "expected"),
    [
        # The reproduction: allow_from and the environment overlap on 1001, and
        # the plugin restriction names 1001 too.
        (("1001",), top_level(allow_from=["1001"]), "1001,2002", {"1001"}),
        # Overlap with no plugin restriction.
        ((), top_level(allow_from=["1001", "2002"]), "2002,3003", {"2002"}),
        # Disjoint gates authorise nobody.
        ((), top_level(allow_from=["1001"]), "2002", set()),
        # Configuration only: no environment allowlist at all.
        ((), top_level(allow_from=["1001", "2002"]), "", {"1001", "2002"}),
        # Present but empty `allow_from` is a deliberate lockout.
        ((), top_level(allow_from=[]), "1001,2002", set()),
        # A wildcard removes the intake bound; it grants nothing on its own.
        ((), top_level(allow_from=["*"]), "1001", {"1001"}),
        ((), top_level(allow_from=["*"]), "", set()),
        # The plugin restriction may only narrow.
        (("1001",), top_level(allow_from=["1001", "2002"]), "1001,2002", {"1001"}),
        (("9009",), top_level(allow_from=["1001"]), "1001", set()),
    ],
)
def test_the_runtime_is_given_the_final_allowed_users(
    monkeypatch, restriction, host_config, environment, expected
):
    """Every gate is applied in the parent, where the host config is readable."""
    monkeypatch.setattr("learning_studio.config.load_raw_config", lambda: host_config)
    config = operator_config(mini_app_allowed_telegram_users=restriction)

    child = handed_over(config, TELEGRAM_ALLOWED_USERS=environment)
    settings = server.settings_from_environment(child)

    assert settings.allowed_users == frozenset(expected)


def test_an_empty_allowed_user_set_is_carried_rather_than_omitted(monkeypatch):
    """ "Nobody" and "nothing was said" must not look the same to the child."""
    monkeypatch.setattr("learning_studio.config.load_raw_config", lambda: top_level(allow_from=[]))

    child = handed_over(operator_config(), TELEGRAM_ALLOWED_USERS="1001")

    assert env.ALLOWED_USERS in child
    assert server.settings_from_environment(child).allowed_users == frozenset()


def test_raw_host_allowlists_do_not_reach_the_runtime(monkeypatch):
    monkeypatch.setattr("learning_studio.config.load_raw_config", lambda: {})
    child = handed_over(
        operator_config(mini_app_allowed_telegram_users=()),
        TELEGRAM_ALLOWED_USERS="1001",
        GATEWAY_ALLOWED_USERS="2002",
        TELEGRAM_GROUP_ALLOWED_USERS="3003",
        TELEGRAM_GROUP_ALLOWED_CHATS="4004",
    )

    assert server.settings_from_environment(child).allowed_users == frozenset({"1001", "2002"})
    assert set(child).isdisjoint(
        {
            "TELEGRAM_ALLOWED_USERS",
            "GATEWAY_ALLOWED_USERS",
            "TELEGRAM_GROUP_ALLOWED_USERS",
            "TELEGRAM_GROUP_ALLOWED_CHATS",
        }
    )


# ── Both instructions fail closed ─────────────────────────────────────────


@pytest.mark.parametrize("missing", [env.CONFIG, env.ALLOWED_USERS])
def test_a_missing_instruction_refuses_to_start(missing: str):
    child = handed_over(operator_config())
    child.pop(missing)

    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(child)

    assert missing in str(caught.value)


def _config_payload(child: dict[str, str]) -> dict:
    return json.loads(child[env.CONFIG])


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda payload: "not json at all", id="not-json"),
        pytest.param(lambda payload: json.dumps([payload]), id="not-an-object"),
        pytest.param(
            lambda payload: json.dumps({**payload, "runtime_hosts": "127.0.0.1"}),
            id="unknown-setting",
        ),
        pytest.param(
            lambda payload: json.dumps(
                {k: v for k, v in payload.items() if k != "mini_app_session_ttl_seconds"}
            ),
            id="incomplete",
        ),
        pytest.param(
            lambda payload: json.dumps({**payload, "runtime_host": "10.0.0.1"}),
            id="not-loopback",
        ),
        pytest.param(
            lambda payload: json.dumps({**payload, "mini_app_session_ttl_seconds": 1}),
            id="out-of-range",
        ),
        pytest.param(
            lambda payload: json.dumps(
                {
                    **payload,
                    "runtime_idle_timeout_seconds": 86_400,
                    "runtime_max_lifetime_seconds": 300,
                }
            ),
            id="contradictory",
        ),
    ],
)
def test_a_malformed_configuration_refuses_to_start(mangle):
    child = handed_over(operator_config())
    child[env.CONFIG] = mangle(_config_payload(child))

    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(child)

    assert env.CONFIG in str(caught.value)


def test_duplicate_configuration_keys_refuse_to_start():
    child = handed_over(operator_config())
    child[env.CONFIG] = child[env.CONFIG][:-1] + ',"runtime_host":"127.0.0.2"}'

    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(child)

    assert env.CONFIG in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not json",
        "[1001]",
        '{"1001": true}',
        '["*"]',
        '["nine"]',
        '["-1"]',
        '["001"]',
        '["1001","1001"]',
    ],
)
def test_a_malformed_allowed_user_instruction_refuses_to_start(value: str):
    child = handed_over(operator_config())
    child[env.ALLOWED_USERS] = value

    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(child)

    assert env.ALLOWED_USERS in str(caught.value)


def test_a_refusal_names_the_variable_and_never_its_value():
    """These values describe an operator's profile and name their learners."""
    child = handed_over(operator_config())
    child[env.CONFIG] = json.dumps({**_config_payload(child), "runtime_host": "10.11.12.13"})
    child[env.ALLOWED_USERS] = json.dumps(["1001", "wrong"])

    with pytest.raises(server.BadEnvironment) as config_refusal:
        server.settings_from_environment(child)
    assert "10.11.12.13" not in str(config_refusal.value)

    child[env.CONFIG] = json.dumps(_config_payload(handed_over(operator_config())))
    with pytest.raises(server.BadEnvironment) as allowlist_refusal:
        server.settings_from_environment(child)
    assert "1001" not in str(allowlist_refusal.value)
    assert "wrong" not in str(allowlist_refusal.value)


def test_an_oversized_valid_configuration_is_refused_before_process_start(
    hermes_home: Path, monkeypatch, tmp_path: Path
):
    canary = "s" * MAX_VALUE_CHARS
    large_context = {"interests": [canary] * MAX_LIST_ITEMS}
    config = LearningStudioConfig.from_mapping(
        {"learning_studio": {"profile_context": large_context, "defaults": large_context}}
    )
    assert len(config_to_json(config).encode("utf-8")) > env.MAX_STARTUP_INSTRUCTION_BYTES
    monkeypatch.setattr("learning_studio.config.load_raw_config", lambda: {})
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    started = False

    def popen(*_args, **_kwargs):
        nonlocal started
        started = True
        raise AssertionError("an oversized environment reached process start")

    with pytest.raises(RuntimeUnavailable) as caught:
        supervisor.ensure_running(config, python=interpreter, popen=popen)  # type: ignore[arg-type]

    assert caught.value.reason == "runtime_spawn_failed"
    assert started is False
    assert canary not in str(caught.value)


@pytest.mark.parametrize("name", [env.CONFIG, env.ALLOWED_USERS])
def test_an_oversized_startup_instruction_is_refused_without_its_value(name: str):
    child = handed_over(operator_config())
    canary = "x" * (env.MAX_STARTUP_INSTRUCTION_BYTES + 1)
    child[name] = canary

    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(child)

    assert name in str(caught.value)
    assert canary not in str(caught.value)


@pytest.mark.parametrize("name", [env.CONFIG, env.ALLOWED_USERS])
def test_raw_instruction_size_is_checked_before_whitespace_is_removed(name: str):
    child = handed_over(operator_config())
    child[name] = " " * (env.MAX_STARTUP_INSTRUCTION_BYTES + 1) + child[name]

    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(child)

    assert name in str(caught.value)


def test_timeout_instructions_must_match_the_handed_over_config():
    child = handed_over(operator_config())
    child[env.IDLE_SECONDS] = "121"

    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(child)

    assert env.IDLE_SECONDS in str(caught.value)


def test_allowed_users_must_honor_the_handed_over_plugin_restriction():
    child = handed_over(operator_config(mini_app_allowed_telegram_users=("1001",)))
    child[env.ALLOWED_USERS] = '["2002"]'

    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(child)

    assert env.ALLOWED_USERS in str(caught.value)


# ── The runtime serves from what it was handed ────────────────────────────


class _FakeUvicornServer:
    """Records the configuration it was built with; never opens a socket."""

    def __init__(self, config) -> None:
        self.config = config
        self.started = False
        self.should_exit = False
        self.servers: list = []

    async def serve(self) -> None:
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0.005)


class _FakeTunnel:
    state = "ready"
    url = "https://x.trycloudflare.com"
    ready = True
    reason = ""
    alive = True

    async def aclose(self, **_kwargs) -> None:
        return None


@pytest.fixture
def served(monkeypatch, tmp_path: Path, hermes_home: Path):
    """Run ``serve()`` to readiness with no host configuration available at all.

    ``load_config`` and ``load_raw_config`` are made to explode rather than be
    stubbed: in the real child they would answer "no host" *quietly*, which is
    the whole defect, so a test that let them answer at all would pass whether
    or not the handoff is used.
    """
    import uvicorn

    from learning_studio import config as config_module
    from learning_studio.runtime import tunnel as tunnel_module
    from learning_studio.web import app as app_module
    from learning_studio.web import dependencies as dependencies_module

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the runtime asked a Hermes it does not have")

    for module in (config_module, dependencies_module):
        for name in ("load_config", "load_raw_config"):
            monkeypatch.setattr(module, name, forbidden, raising=False)

    servers: list[_FakeUvicornServer] = []

    def build_server(config):
        built = _FakeUvicornServer(config)
        servers.append(built)
        return built

    monkeypatch.setattr(uvicorn, "Server", build_server)

    opened: list[dict] = []

    async def open_tunnel(**kwargs):
        opened.append(kwargs)
        return _FakeTunnel()

    monkeypatch.setattr(tunnel_module, "open_tunnel", open_tunnel)

    wired: list = []
    real_create_app = app_module.create_app

    def create_app(dependencies):
        wired.append(dependencies)
        return real_create_app(dependencies)

    monkeypatch.setattr(app_module, "create_app", create_app)

    def run(config: LearningStudioConfig, allowed: frozenset[str]):
        settings = server.RuntimeSettings(
            runtime_id="r-1",
            generation=1,
            control_token="a-control-token",
            profile="family",
            handshake_path=tmp_path / "handshake.json",
            idle_timeout_seconds=60,
            max_lifetime_seconds=300,
            config=config,
            allowed_users=allowed,
        )
        ticks = iter([1000.0, 1000.0])

        def clock() -> float:
            return next(ticks, 1_000_000.0)

        code = asyncio.run(server.serve(settings, clock=clock))
        return code, servers[-1], opened[-1], wired[-1]

    return run


def test_the_runtime_binds_and_tunnels_what_the_operator_configured(served):
    config = operator_config(runtime_host="127.0.0.2", runtime_port=9876)

    code, uvicorn_server, tunnel_kwargs, _dependencies = served(config, frozenset({"1001"}))

    assert code == 0
    assert uvicorn_server.config.host == "127.0.0.2"
    assert uvicorn_server.config.port == 9876
    assert "127.0.0.2" in tunnel_kwargs["target"]
    assert tunnel_kwargs["timeout_seconds"] == config.tunnel_readiness_timeout_seconds
    assert tunnel_kwargs["executable"] == ""


def test_the_api_is_wired_from_the_handed_over_configuration_and_allowlist(served):
    config = operator_config(runtime_host="127.0.0.3")

    _code, _server, _tunnel, dependencies = served(config, frozenset({"1001"}))

    assert dependencies.config == config
    assert dependencies.allowed_users() == frozenset({"1001"})
