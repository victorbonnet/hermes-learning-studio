"""What the built distributions contain, checked against what they promise.

A source distribution that ships a test suite is making a claim: that the suite
can be run from it. This one shipped `tests/` and the screenshot documentation
while omitting `tools/` — which those tests import and that documentation names —
so an extracted tree could neither run its own suite nor follow its own
instructions.

These tests build the real artefacts. That is slower than reading
``pyproject.toml`` and is the point: the packaging configuration is a set of glob
patterns whose behaviour is not obvious from reading them, and the only honest
check is what comes out.
"""

from __future__ import annotations

import re
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sdist_names() -> set[str]:
    """Every path inside a freshly built source distribution."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        completed = subprocess.run(
            ["uv", "build", "--sdist", "--out-dir", scratch],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(f"could not build an sdist here: {completed.stderr[-300:]}")
        archive = next(Path(scratch).glob("*.tar.gz"))
        with tarfile.open(archive) as tar:
            return {name.split("/", 1)[1] for name in tar.getnames() if "/" in name}


@pytest.fixture(scope="module")
def wheel_names() -> set[str]:
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        completed = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", scratch],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(f"could not build a wheel here: {completed.stderr[-300:]}")
        archive = next(Path(scratch).glob("*.whl"))
        with zipfile.ZipFile(archive) as wheel:
            return set(wheel.namelist())


# ── The sdist carries what its own contents need ──────────────────────────


@pytest.mark.parametrize(
    "required",
    [
        "tools/run_frontend_tests.py",
        "tools/preview_gallery.py",
        "tests/js/dom.mjs",
        "tests/js/harness.mjs",
        "tests/js/renderers.test.mjs",
        "tests/js/flow.test.mjs",
        "tests/component_examples.py",
        "tests/served_responses.py",
        "docs/screenshots/README.md",
        "README.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "plugin.yaml",
        "learning_studio/web/static/index.html",
        "learning_studio/web/static/renderers.js",
    ],
)
def test_the_sdist_includes_what_it_needs(sdist_names: set[str], required: str):
    assert required in sdist_names


def test_every_python_test_module_is_in_the_sdist(sdist_names: set[str]):
    on_disk = {
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "tests").rglob("*.py")
        if "__pycache__" not in path.parts
    }

    assert on_disk <= sdist_names, sorted(on_disk - sdist_names)


def test_every_tool_the_tests_import_is_in_the_sdist(sdist_names: set[str]):
    """`tests/test_frontend_js.py` imports from `tools/`, so it has to be there."""
    on_disk = {
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "tools").glob("*.py")
        if "__pycache__" not in path.parts
    }

    assert on_disk, "there are no tools to package"
    assert on_disk <= sdist_names, sorted(on_disk - sdist_names)


def test_every_screenshot_the_documentation_shows_is_in_the_sdist(sdist_names: set[str]):
    committed = {str(path.relative_to(REPO_ROOT)) for path in (REPO_ROOT / "docs").rglob("*.png")}

    assert committed, "the documentation references screenshots that are not committed"
    assert committed <= sdist_names, sorted(committed - sdist_names)


def test_no_relative_link_in_the_readme_points_outside_the_sdist(sdist_names: set[str]):
    """A distribution whose own README links to files it did not ship."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    links = {
        target
        for target in re.findall(r"\]\(([^)]+)\)", text)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    }

    assert links, "this check found no links to verify"
    missing = sorted(target for target in links if target.split("#")[0] not in sdist_names)
    assert missing == [], missing


def test_every_command_the_readme_documents_names_a_packaged_file(sdist_names: set[str]):
    """`uv run python tools/…` has to find something in an extracted tree."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    scripts = set(re.findall(r"uv run python (tools/[\w/.-]+)", text))

    assert scripts, "the README documents no tool commands"
    assert scripts <= sdist_names, sorted(scripts - sdist_names)


def test_the_screenshot_documentation_names_only_packaged_files(sdist_names: set[str]):
    text = (REPO_ROOT / "docs" / "screenshots" / "README.md").read_text(encoding="utf-8")
    scripts = set(re.findall(r"uv run python (tools/[\w/.-]+)", text))

    assert scripts <= sdist_names, sorted(scripts - sdist_names)


# ── The wheel carries the runtime and nothing else ────────────────────────


def test_the_wheel_ships_every_static_asset(wheel_names: set[str]):
    from learning_studio.web.static_files import STATIC_ASSETS

    for asset in STATIC_ASSETS:
        assert f"learning_studio/web/static/{asset.filename}" in wheel_names


def test_the_wheel_ships_the_skill_and_its_references(wheel_names: set[str]):
    references = {name for name in wheel_names if "skills/adaptive-learning/references/" in name}

    assert "learning_studio/skills/adaptive-learning/SKILL.md" in wheel_names
    assert len(references) >= 10


def test_the_wheel_carries_no_tests_or_tooling(wheel_names: set[str]):
    """An installed plugin has no reason to contain a test suite."""
    stray = [
        name
        for name in wheel_names
        if name.startswith(("tests/", "tools/", "docs/")) or "__pycache__" in name
    ]

    assert stray == [], stray
