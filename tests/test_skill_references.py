"""The skill must be one skill with a discoverable, well-formed reference set.

The agent reaches a reference by naming this skill and the file:
``skill_view(name="learning-studio:adaptive-learning", file_path="references/…")``.
Hermes resolves that inside the skill's own directory and refuses anything that
leaves it (see ``tests/test_hermes_integration.py``). A ``read_file`` on the
substituted ``${HERMES_SKILL_DIR}`` is kept as the fallback for an older host
that ignored ``file_path`` for plugin-namespaced skills. Either way every
advertised path has to resolve relative to the skill directory, so the tests
here treat the links as an API surface rather than as prose.
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
        "the skill must ship a references/ directory under ${HERMES_SKILL_DIR}"
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
    """A broken path is a dead end: read_file() has nothing to open."""
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
    """The compatibility route still has to be spelled out concretely.

    Hermes substitutes ``${HERMES_SKILL_DIR}`` for the skill's real directory
    before serving SKILL.md, so a ``read_file`` on that path resolves. This is
    the same idiom Hermes' own bundled skills use for sibling files, and it is
    what an older host without ``file_path`` support needs — see
    ``test_skill_frames_the_read_file_route_as_an_older_host_fallback`` for the
    framing that keeps it secondary.
    """
    assert "${HERMES_SKILL_DIR}/references/" in skill_md, (
        "SKILL.md must address references through ${HERMES_SKILL_DIR}"
    )
    assert 'read_file("${HERMES_SKILL_DIR}/references/' in skill_md, (
        "SKILL.md must show a concrete read_file call for opening a reference"
    )


def test_skill_prefers_the_qualified_skill_view_file_path_form(skill_md: str):
    """The supported route, offered first.

    Hermes' ``skill_view`` dispatches a qualified ``plugin:skill`` name to
    ``_serve_plugin_skill()``, which takes ``file_path``, refuses a ``..``
    component, confirms the resolved target is still inside the skill
    directory, and returns that file. So SKILL.md must name the skill and the
    file rather than sending the agent somewhere else first —
    ``tests/test_hermes_integration.py`` pins that host contract.
    """
    assert (
        'skill_view(name="learning-studio:adaptive-learning", file_path="references/' in skill_md
    ), "SKILL.md must show the qualified skill_view call for opening a reference"

    normalized = " ".join(skill_md.lower().split())
    # The old text said current Hermes ignores file_path. It does not, and an
    # agent told otherwise would skip the route that actually works.
    assert "do not try to load a reference with `skill_view`" not in normalized, (
        "SKILL.md still warns against skill_view, which current Hermes supports"
    )


def test_skill_frames_the_read_file_route_as_an_older_host_fallback(skill_md: str):
    """``read_file`` stays documented, but only as compatibility.

    A host predating the ``file_path`` parameter returned SKILL.md again
    reporting success, so the token route is still worth having. What it must
    not be is the headline instruction: that would send every agent down the
    substitution-dependent path on a host that does not need it.
    """
    normalized = " ".join(skill_md.lower().split())

    assert "fallback" in normalized, "the read_file route must be marked as a fallback"
    assert "older hermes" in normalized, (
        "the fallback must say which hosts need it, rather than describing current Hermes"
    )
    # Offered in that order, so the agent reads the supported route first.
    assert skill_md.index("skill_view(name=") < skill_md.index(
        'read_file("${HERMES_SKILL_DIR}/references/'
    ), "SKILL.md offers the read_file fallback before the supported skill_view route"


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
