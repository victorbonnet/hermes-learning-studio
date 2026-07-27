"""Shared fixtures and path helpers for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make ``learning_studio`` and ``tests.fake_hermes`` importable when pytest is
# invoked from anywhere.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def skill_dir() -> Path:
    """The bundled skill's directory, derived from the plugin's own helper.

    Going through ``skill_path()`` rather than hardcoding the layout means a
    move of the skill directory fails registration tests, not these.
    """
    from learning_studio.plugin import skill_path

    return skill_path().parent


@pytest.fixture
def skill_md(skill_dir: Path) -> str:
    """Full text of SKILL.md, frontmatter included."""
    return (skill_dir / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture
def references(skill_dir: Path) -> dict[str, str]:
    """Every bundled reference, keyed by filename stem."""
    reference_dir = skill_dir / "references"
    if not reference_dir.is_dir():
        return {}
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(reference_dir.glob("*.md"))
    }


@pytest.fixture
def corpus(skill_md: str, references: dict[str, str]) -> str:
    """SKILL.md and every reference concatenated — the full agent-facing text."""
    return "\n".join([skill_md, *references.values()])


@pytest.fixture
def ctx():
    """A fake Hermes plugin context bound to this plugin's manifest name."""
    from tests.fake_hermes import FakePluginContext

    return FakePluginContext(plugin_name="learning-studio")


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch) -> Path:
    """An isolated ``HERMES_HOME`` for anything that touches the filesystem.

    Persistence tests must never reach the developer's real profile: it would
    leak their learning data into the suite and couple tests to each other.
    Every storage-facing test takes this fixture.
    """
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def principal():
    """A trusted principal, as Hermes' session context would supply one.

    Identity is never a tool argument, so tests construct it the way the
    host does rather than passing a learner key around.
    """
    from learning_studio.identity import Principal

    return Principal(
        profile="default", platform="telegram", user_id="1001", source="gateway_session"
    )


@pytest.fixture
def other_principal():
    """A second authenticated learner in the same profile."""
    from learning_studio.identity import Principal

    return Principal(
        profile="default", platform="telegram", user_id="2002", source="gateway_session"
    )


#: Every session variable the real host defines, plus the two this plugin
#: actually reads. Listed explicitly rather than by prefix so that clearing
#: is total even if a test sets one by hand.
SESSION_ENV_VARS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_NAME",
    "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_USER_NAME",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
    "HERMES_UI_SESSION_ID",
    "HERMES_SESSION_MESSAGE_ID",
    "HERMES_SESSION_PROFILE",
    "HERMES_GATEWAY_SESSION",
)


@pytest.fixture(autouse=True)
def isolated_identity():
    """Start every test with no inherited learner identity.

    Autouse and unconditional, because the failure it prevents is silent and
    environment-dependent: run the suite from inside a live Telegram-hosted
    Hermes session and `HERMES_SESSION_PLATFORM=telegram` is already in the
    environment, so a test expecting the local-CLI principal quietly asserts
    against somebody's real account instead.

    Both halves of the identity path are cleared — the environment *and* the
    host's ContextVars when the real module happens to be loaded — since
    either alone would leave the other leaking. Everything is restored
    afterwards, including variables a test set itself.
    """
    import contextlib
    import os
    import sys

    saved_env = {name: os.environ.get(name) for name in SESSION_ENV_VARS}
    for name in SESSION_ENV_VARS:
        os.environ.pop(name, None)

    session_context = sys.modules.get("gateway.session_context")
    saved_vars = {}
    if session_context is not None:
        # Snapshot and blank the real ContextVars; `get_session_env` treats a
        # bound "" as authoritative, which is exactly the "no session" answer.
        for var_name, var in getattr(session_context, "_VAR_MAP", {}).items():
            saved_vars[var_name] = var.set("")

    try:
        yield
    finally:
        if session_context is not None:
            for var_name, token in saved_vars.items():
                # A token created in another context cannot be reset here;
                # that is survivable, the environment restore below is not.
                with contextlib.suppress(ValueError, KeyError):
                    session_context._VAR_MAP[var_name].reset(token)
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def gateway_session(monkeypatch):
    """Bind Hermes session vars the way a platform adapter does.

    Yields a setter so a test can switch which authenticated user is
    speaking, which is the only supported way to change learner identity.
    """
    import os

    def bind(platform: str = "telegram", user_id: str = "1001", chat_id: str = "chat-1") -> None:
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
        monkeypatch.setenv("HERMES_SESSION_USER_ID", user_id)
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)

    bind()
    yield bind
    for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_USER_ID", "HERMES_SESSION_CHAT_ID"):
        os.environ.pop(name, None)
