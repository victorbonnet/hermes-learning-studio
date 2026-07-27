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
