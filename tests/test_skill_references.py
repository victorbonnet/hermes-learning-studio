"""The skill must be one skill with a discoverable, well-formed reference set.

Hermes exposes references through ``skill_view(name, path)`` — level 2 of its
progressive-disclosure model. That only works if the paths written in SKILL.md
resolve relative to the skill directory, so the tests here treat the links as
an API surface rather than as prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Every reference this skill must ship. Names are part of the skill's
#: contract: SKILL.md tells the agent to load them by these paths.
REQUIRED_REFERENCES = (
    "learning-discovery",
    "activation-policy",
    "manifest-contract",
    "selection-cards",
    "text-input-cards",
    "ordering-and-matching",
    "flashcards-and-recall",
    "media-cards",
    "diagrams-and-hotspots",
    "timelines-and-processes",
    "tables-and-grids",
    "scenarios-and-simulations",
    "reflection-and-rubrics",
    "accessibility",
)

#: The subset that documents an interactive card type. Each one answers the
#: same fixed set of questions so the agent can compare cards at a glance.
UI_REFERENCES = (
    "selection-cards",
    "text-input-cards",
    "ordering-and-matching",
    "flashcards-and-recall",
    "media-cards",
    "diagrams-and-hotspots",
    "timelines-and-processes",
    "tables-and-grids",
    "scenarios-and-simulations",
    "reflection-and-rubrics",
)

REQUIRED_UI_SECTIONS = (
    "When to use",
    "When not to use",
    "Required fields",
    "Evaluation",
    "Accessibility",
    "Anti-patterns",
    "Combinations",
    "Examples",
)

#: Markdown inline links, e.g. ``[selection cards](references/selection-cards.md)``.
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def linked_targets(skill_md: str) -> list[str]:
    return MARKDOWN_LINK_RE.findall(skill_md)


def reference_links(skill_md: str) -> list[str]:
    return [target for target in linked_targets(skill_md) if target.startswith("references/")]


# ── The reference set exists and is complete ───────────────────────────────


def test_references_directory_exists(skill_dir: Path):
    assert (skill_dir / "references").is_dir(), (
        "the skill must ship a references/ directory for skill_view(name, path)"
    )


@pytest.mark.parametrize("name", REQUIRED_REFERENCES)
def test_required_reference_exists(references: dict[str, str], name: str):
    assert name in references, f"missing required reference: references/{name}.md"


@pytest.mark.parametrize("name", REQUIRED_REFERENCES)
def test_reference_is_not_a_stub(references: dict[str, str], name: str):
    text = references.get(name, "")
    assert text.lstrip().startswith("# "), f"references/{name}.md must open with an H1 title"
    assert len(text.split()) > 150, f"references/{name}.md looks like a stub"


# ── Links resolve, and nothing is orphaned ─────────────────────────────────


def test_every_linked_reference_resolves(skill_dir: Path, skill_md: str):
    """A broken path is a dead end: skill_view() has nothing to open."""
    for target in reference_links(skill_md):
        assert (skill_dir / target).is_file(), (
            f"SKILL.md links '{target}', which does not exist relative to the skill directory"
        )


def test_reference_links_are_relative_and_contained(skill_md: str):
    for target in reference_links(skill_md):
        assert not target.startswith("/"), f"'{target}' must be relative, not absolute"
        assert "://" not in target, f"'{target}' must be a bundled file, not a URL"
        assert ".." not in Path(target).parts, f"'{target}' must not escape the skill directory"


def test_every_reference_is_linked_from_the_skill(references: dict[str, str], skill_md: str):
    """An unlinked reference is invisible — the agent has no way to find it."""
    linked = {Path(target).stem for target in reference_links(skill_md)}
    orphans = sorted(set(references) - linked)
    assert orphans == [], f"references not linked from SKILL.md: {orphans}"


def test_skill_tells_the_agent_to_load_references_selectively(skill_md: str):
    """Loading the whole catalogue defeats the point of progressive disclosure."""
    normalized = " ".join(skill_md.lower().split())
    assert "only the references" in normalized or "load only" in normalized
    assert "do not preload" in normalized


def test_references_are_opened_with_read_file_and_the_skill_dir_token(skill_md: str):
    """The only mechanism that actually works for a plugin-namespaced skill.

    Hermes substitutes ``${HERMES_SKILL_DIR}`` for the skill's real directory
    before serving SKILL.md, so a ``read_file`` on that path resolves. This is
    the same idiom Hermes' own bundled skills use for sibling files.
    """
    assert "${HERMES_SKILL_DIR}/references/" in skill_md, (
        "SKILL.md must address references through ${HERMES_SKILL_DIR}"
    )
    assert 'read_file("${HERMES_SKILL_DIR}/references/' in skill_md, (
        "SKILL.md must show a concrete read_file call for opening a reference"
    )


def test_skill_warns_that_skill_view_cannot_open_a_reference(skill_md: str):
    """``skill_view``'s file_path is silently ignored for plugin skills.

    Hermes' ``skill_view`` dispatches qualified ``plugin:skill`` names to
    ``_serve_plugin_skill()``, which takes no ``file_path`` argument — the call
    returns SKILL.md again and *reports success*. An agent that follows the
    wrong idiom would silently re-read this file instead of the reference, so
    the warning has to be in the text.
    """
    normalized = " ".join(skill_md.lower().split())
    assert "do not try to load a reference with `skill_view`" in normalized, (
        "SKILL.md must warn against the skill_view idiom"
    )
    assert "ignored for plugin" in normalized and "returns this same skill.md" in normalized


def test_skill_gives_a_fallback_when_substitution_is_disabled(skill_md: str):
    """``template_vars`` can be switched off in a profile's skills config."""
    normalized = " ".join(skill_md.lower().split())
    assert "substitution is switched off" in normalized
    assert "search_files" in normalized


# ── One skill, not one skill per card ──────────────────────────────────────


def test_exactly_one_skill_directory_is_bundled(skill_dir: Path):
    skills_root = skill_dir.parent
    bundled = sorted(path.parent.name for path in skills_root.glob("*/SKILL.md"))
    assert bundled == ["adaptive-learning"], (
        f"expected exactly one bundled skill, found {bundled} — "
        f"the UI catalogue belongs in references/, not in extra skills"
    )


def test_no_reference_is_itself_a_skill(skill_dir: Path):
    stray = sorted(
        str(p.relative_to(skill_dir)) for p in (skill_dir / "references").rglob("SKILL.md")
    )
    assert stray == [], f"references must not contain SKILL.md files: {stray}"


def test_register_still_registers_exactly_one_skill(ctx):
    """The catalogue must not grow the registered surface."""
    from learning_studio import register

    register(ctx)

    assert ctx.qualified_skill_names == ["learning-studio:adaptive-learning"]


# ── Every UI reference answers the same questions ──────────────────────────


@pytest.mark.parametrize("name", UI_REFERENCES)
@pytest.mark.parametrize("section", REQUIRED_UI_SECTIONS)
def test_ui_reference_documents_required_section(
    references: dict[str, str], name: str, section: str
):
    text = references.get(name, "")
    headings = {
        line.lstrip("#").strip().lower() for line in text.splitlines() if line.startswith("#")
    }
    assert any(section.lower() in heading for heading in headings), (
        f"references/{name}.md is missing a '{section}' section"
    )
