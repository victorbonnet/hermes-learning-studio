"""The bundled skill file must be well-formed and truthful about its scope."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

SKILL_RELPATH = Path("learning_studio") / "skills" / "adaptive-learning" / "SKILL.md"


@pytest.fixture
def skill_bytes(repo_root: Path) -> bytes:
    path = repo_root / SKILL_RELPATH
    assert path.is_file(), f"missing bundled skill: {SKILL_RELPATH}"
    return path.read_bytes()


@pytest.fixture
def skill_text(skill_bytes: bytes) -> str:
    return skill_bytes.decode("utf-8")


def test_frontmatter_starts_at_byte_zero(skill_bytes: bytes):
    """A BOM or leading blank line stops the frontmatter parser cold."""
    assert skill_bytes.startswith(b"---\n"), (
        "SKILL.md must open with '---' at byte 0 (no BOM, no leading whitespace)"
    )


def test_frontmatter_has_name_and_description(skill_text: str):
    _, _, rest = skill_text.partition("---\n")
    raw_frontmatter, delimiter, _ = rest.partition("\n---\n")
    assert delimiter, "SKILL.md frontmatter is not terminated by a '---' line"

    frontmatter = yaml.safe_load(raw_frontmatter)
    assert isinstance(frontmatter, dict)
    assert frontmatter["name"] == "adaptive-learning"
    assert frontmatter["description"].strip()


def test_body_is_non_empty(skill_text: str):
    _, _, rest = skill_text.partition("---\n")
    _, _, body = rest.partition("\n---\n")
    assert body.strip(), "SKILL.md must have a body after the frontmatter"


def test_frontmatter_name_matches_registered_skill_name(skill_text: str, ctx):
    from learning_studio import register

    register(ctx)

    _, _, rest = skill_text.partition("---\n")
    raw_frontmatter, _, _ = rest.partition("\n---\n")
    frontmatter = yaml.safe_load(raw_frontmatter)

    assert frontmatter["name"] == ctx.skills[0].name


def test_skill_does_not_advertise_unimplemented_tools(skill_text: str, ctx):
    """Every Studio tool the skill names must actually be registered.

    The failure this prevents is an aspirational skill: guidance that sends
    the agent after ``learning_studio_start_exercise`` because a future PR
    was written about before it shipped. The check runs in both directions —
    a tool named but not registered fails here, and the registered set is
    read from ``register()`` rather than hardcoded so it cannot go stale.
    """
    import re

    from learning_studio import register

    register(ctx)
    registered = {tool.name for tool in ctx.tools}
    assert registered, "this guard assumes the plugin registers at least one tool"

    body = skill_text.partition("---\n")[2].partition("\n---\n")[2]
    named = set(re.findall(r"\blearning_studio_[a-z0-9_]+", body))

    unknown = sorted(named - registered)
    assert unknown == [], (
        f"SKILL.md names {unknown}, which register() does not register — the agent "
        f"would be sent after a tool that does not exist"
    )
