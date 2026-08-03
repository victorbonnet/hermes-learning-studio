"""Credentials and allowlists belong to the profile being served, not the process.

Hermes can multiplex several profiles through one process. When it does, the
Telegram token and allowlists in ``os.environ`` may belong to a *different*
profile than the turn currently running — so reading them directly is not a
shortcut, it is a cross-profile credential leak with a plausible-looking
implementation.
"""

from __future__ import annotations

import sys
import types

import pytest

from learning_studio import authorization, secrets


@pytest.fixture
def hermes_secret_scope(monkeypatch):
    """A stand-in for ``agent.secret_scope`` with a switchable active profile.

    Faithful to the contract that matters: under multiplexing the scope is
    authoritative and a miss does *not* fall through to ``os.environ``.
    """
    state = {"scope": None, "multiplex": True}

    def get_secret(name, default=None):
        if name in ("HERMES_HOME", "HOME", "PATH", "TMPDIR", "LANG"):
            import os

            return os.environ.get(name, default)
        scope = state["scope"]
        if scope is not None:
            value = scope.get(name)
            if value is not None:
                return value
            if state["multiplex"]:
                return default
        import os

        return os.environ.get(name, default)

    module = types.ModuleType("agent.secret_scope")
    module.get_secret = get_secret
    package = types.ModuleType("agent")
    package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    return state


PROFILE_A = {"TELEGRAM_BOT_TOKEN": "111:AAAprofileA", "TELEGRAM_ALLOWED_USERS": "1001"}
PROFILE_B = {"TELEGRAM_BOT_TOKEN": "222:BBBprofileB", "TELEGRAM_ALLOWED_USERS": "2002"}


def test_the_token_comes_from_the_active_profile_not_the_process(hermes_secret_scope, monkeypatch):
    """The headline case: globals say A, the turn is serving B."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PROFILE_A["TELEGRAM_BOT_TOKEN"])
    hermes_secret_scope["scope"] = PROFILE_B

    assert secrets.telegram_bot_token() == "222:BBBprofileB"


def test_a_missing_credential_in_the_active_profile_fails_closed(hermes_secret_scope, monkeypatch):
    """Falling back to A's token would deliver B's exercise from A's bot."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PROFILE_A["TELEGRAM_BOT_TOKEN"])
    hermes_secret_scope["scope"] = {"OTHER": "x"}

    assert secrets.telegram_bot_token() == ""


def test_the_allowlist_comes_from_the_active_profile(hermes_secret_scope, monkeypatch):
    """Otherwise B authorises A's learners, and denies its own."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", PROFILE_A["TELEGRAM_ALLOWED_USERS"])
    hermes_secret_scope["scope"] = PROFILE_B

    assert authorization.env_allowed_users() == frozenset({"2002"})


def test_two_profiles_resolve_independently(hermes_secret_scope, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "9999")

    hermes_secret_scope["scope"] = PROFILE_A
    first = authorization.env_allowed_users()
    hermes_secret_scope["scope"] = PROFILE_B
    second = authorization.env_allowed_users()

    assert first == frozenset({"1001"})
    assert second == frozenset({"2002"})


def test_a_child_runtime_is_given_the_active_profile_values(
    hermes_secret_scope, monkeypatch, hermes_home
):
    """The runtime verifies signatures with whatever it is handed."""
    from pathlib import Path

    from learning_studio.runtime import state, supervisor

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PROFILE_A["TELEGRAM_BOT_TOKEN"])
    hermes_secret_scope["scope"] = PROFILE_B

    child = supervisor.child_environment(
        state.RuntimeRecord(
            runtime_id="r",
            generation=1,
            profile="b",
            pid=1,
            host="127.0.0.1",
            port=1,
            control_token="t",
            executable="/x/python",
            started_at=0.0,
            idle_timeout_seconds=60,
            max_lifetime_seconds=300,
        ),
        handshake=Path("/tmp/h.json"),
        cloudflared="",
    )

    assert child["TELEGRAM_BOT_TOKEN"] == "222:BBBprofileB"
    assert PROFILE_A["TELEGRAM_BOT_TOKEN"] not in child.values()


def test_an_unscoped_read_under_multiplexing_is_treated_as_absent(monkeypatch):
    """Hermes raises to make the mistake loud; a learning plugin refuses instead."""

    def explode(name, default=None):
        raise RuntimeError("get_secret called with no profile secret scope active")

    module = types.ModuleType("agent.secret_scope")
    module.get_secret = explode
    package = types.ModuleType("agent")
    package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)

    assert secrets.telegram_bot_token() == ""


def test_without_hermes_the_environment_is_the_only_source(monkeypatch):
    """The test and build path: no host, no multiplexing, nothing to leak from."""
    monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    monkeypatch.setattr(
        "builtins.__import__",
        _blocking_import(monkeypatch, "agent.secret_scope"),
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "333:CCClocal")

    assert secrets.telegram_bot_token() == "333:CCClocal"


def _blocking_import(monkeypatch, blocked: str):
    real = __import__

    def guard(name, *args, **kwargs):
        if name == blocked:
            raise ImportError(name)
        return real(name, *args, **kwargs)

    return guard


def test_no_module_reads_the_telegram_token_from_the_process_environment():
    """Parsed by name: the only reader is the profile-scoped wrapper."""
    from pathlib import Path

    package = Path(secrets.__file__).parent
    offenders = []
    for path in package.rglob("*.py"):
        relative = path.relative_to(package).as_posix()
        if relative in ("secrets.py", "runtime/environment.py"):
            continue
        body = path.read_text(encoding="utf-8")
        if "TELEGRAM_BOT_TOKEN" not in body:
            continue
        # The web layer runs inside the child process, whose whole environment
        # the supervisor built from the active profile's scope.
        if relative.startswith("web/"):
            continue
        offenders.append(relative)

    assert offenders == [], offenders
