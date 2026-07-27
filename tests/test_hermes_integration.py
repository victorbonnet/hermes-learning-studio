"""Integration tests against a real Hermes checkout.

Hermes is not on PyPI and is not a dependency of this repository, so these
tests are opt-in: point ``HERMES_AGENT_SRC`` at a clone of
``NousResearch/hermes-agent`` and they run against the host's *actual* skill
machinery instead of this repo's fake context.

    HERMES_AGENT_SRC=/path/to/hermes-agent uv run pytest tests/test_hermes_integration.py

They exist because the reference-loading mechanism is a claim about someone
else's code. The unit tests can only check that SKILL.md *says* the right
thing; these check that what it says is true of Hermes as it actually is.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
from pathlib import Path

import pytest

HERMES_SRC_ENV = "HERMES_AGENT_SRC"


def _hermes_src() -> Path:
    raw = os.environ.get(HERMES_SRC_ENV)
    if not raw:
        pytest.skip(f"set {HERMES_SRC_ENV} to a hermes-agent checkout to run integration tests")
    path = Path(raw).expanduser()
    if not path.is_dir():
        pytest.skip(f"{HERMES_SRC_ENV}={path} is not a directory")
    return path


def _load_module(name: str, path: Path, src: Path):
    """Import a single Hermes source file without installing the package.

    Hermes modules import their siblings absolutely (``hermes_cli.…``), so the
    checkout root goes on ``sys.path`` first. A checkout whose own third-party
    dependencies are absent skips rather than fails — that is an environment
    gap, not a defect in this plugin.
    """
    if not path.is_file():
        pytest.skip(f"not found in this Hermes checkout: {path}")

    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        pytest.skip(f"Hermes checkout is missing its own dependencies: {exc}")
    return module


@pytest.fixture
def skill_preprocessing():
    src = _hermes_src()
    return _load_module(
        "_hermes_skill_preprocessing", src / "agent" / "skill_preprocessing.py", src
    )


# ── The mechanism SKILL.md tells the agent to use actually works ───────────


def test_skill_dir_token_expands_to_the_real_skill_directory(skill_preprocessing, skill_dir: Path):
    """``${HERMES_SKILL_DIR}`` must resolve, or every reference path is dead."""
    rendered = skill_preprocessing.substitute_template_vars(
        'read_file("${HERMES_SKILL_DIR}/references/selection-cards.md")',
        skill_dir,
        None,
    )

    assert "${HERMES_SKILL_DIR}" not in rendered, "token was not substituted"
    assert str(skill_dir) in rendered


def test_every_reference_resolves_after_substitution(
    skill_preprocessing, skill_dir: Path, skill_md: str
):
    """Expand the token the way Hermes does, then open every path we advertise."""
    rendered = skill_preprocessing.substitute_template_vars(skill_md, skill_dir, None)

    advertised = re.findall(r'read_file\("([^"]+)"\)', rendered)
    assert advertised, "SKILL.md advertises no read_file path"

    for target in advertised:
        assert Path(target).is_file(), f"advertised path does not resolve: {target}"


def test_template_substitution_is_on_by_default(skill_preprocessing):
    """If ``template_vars`` defaulted off, the primary idiom would ship broken."""
    source = inspect.getsource(skill_preprocessing.preprocess_skill_content)

    assert 'get("template_vars", True)' in source, (
        "Hermes no longer enables template_vars by default — SKILL.md's "
        "fallback instructions become the primary path"
    )


# ── The mechanism SKILL.md warns against is still broken ───────────────────


def test_serve_plugin_skill_still_ignores_file_path():
    """The reason we use read_file rather than skill_view(name, file_path).

    ``skill_view`` routes qualified ``plugin:skill`` names to
    ``_serve_plugin_skill()``, which has no ``file_path`` parameter — so the
    argument is silently dropped and SKILL.md is returned with
    ``success: True``. If Hermes ever adds it, this test fails and SKILL.md's
    warning can be simplified.
    """
    src = _hermes_src()
    skills_tool = (src / "tools" / "skills_tool.py").read_text(encoding="utf-8")

    signature = re.search(r"def _serve_plugin_skill\((.*?)\) -> str:", skills_tool, re.S)
    assert signature, "could not find _serve_plugin_skill in this Hermes checkout"

    assert "file_path" not in signature.group(1), (
        "_serve_plugin_skill now accepts file_path — Hermes may have fixed "
        "plugin reference loading; re-evaluate the read_file workaround"
    )


def test_plugin_dispatch_returns_before_file_path_is_handled():
    """Confirms the drop is structural, not just a missing parameter name."""
    src = _hermes_src()
    skills_tool = (src / "tools" / "skills_tool.py").read_text(encoding="utf-8")

    dispatch = skills_tool.index("return _serve_plugin_skill(")
    handling = skills_tool.index("if file_path and skill_dir:")

    assert dispatch < handling, (
        "file_path handling now precedes the plugin dispatch — re-evaluate "
        "whether skill_view can open plugin references directly"
    )
