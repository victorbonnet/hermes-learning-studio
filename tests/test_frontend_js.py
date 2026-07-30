"""Runs the browser-free JavaScript suite, on fixtures generated from the registry.

The frontend has logic in it — dispatch, validation, response construction — and
reading a diff is not a test of any of that. This module executes it under Node
with a small DOM shim (``tests/js/dom.mjs``), which is the whole of the frontend
tooling: no package manager, no lockfile, no browser download, nothing added to
anybody's environment.

The fixtures are the part that matters most. They are built *here*, from
:mod:`tests.component_examples` through the real
:func:`learning_studio.components.build_component`, so the JavaScript is exercised
against the exact learner projection the API would return — canaries in every
hidden field included. A committed JSON fixture would have been simpler and would
have started drifting from the component registry the first time a type changed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from learning_studio.components import COMPONENT_TYPES, build_component
from tests.component_examples import CANARY, example

NODE = shutil.which("node")


def component_fixtures() -> dict[str, dict]:
    """One API-shaped component per registry type, keyed by type.

    Shaped exactly as ``GET /api/session/component`` returns it, because that is
    what ``render()`` is given: a validated component, projected for a learner,
    with the evaluator-only half absent rather than removed.
    """
    fixtures: dict[str, dict] = {}
    for position, component_type in enumerate(COMPONENT_TYPES):
        component = build_component(example(component_type), f"components[{position}]")
        payload = component.learner_payload()
        fixtures[component_type] = {
            "position": position,
            "component_id": f"component-{position}",
            "type": component_type,
            "payload": payload,
        }
    return fixtures


def test_the_fixtures_carry_no_hidden_field_before_node_is_asked_to_check():
    """The Python side of the same assertion the JavaScript makes.

    If this fails, the JavaScript suite's canary tests are vacuous — they would
    be looking for a marker that was never in the input.
    """
    fixtures = component_fixtures()
    rendered = json.dumps(fixtures)

    assert CANARY not in rendered
    assert set(fixtures) == set(COMPONENT_TYPES)
    # And the examples really do contain canaries to leak, so the check has teeth.
    assert CANARY in json.dumps({name: example(name) for name in COMPONENT_TYPES})


@pytest.mark.skipif(NODE is None, reason="node is not installed; the frontend suite needs it")
def test_the_frontend_suite_passes(tmp_path, repo_root):
    """Node's own test runner, with the generated fixtures handed over by path.

    The suite files are enumerated here rather than passed as a directory or a
    glob: ``node --test`` has changed its mind about both across releases, and
    this repository supports four Python versions on whatever Node the runner
    happens to have. An explicit list behaves the same everywhere, and a new
    suite file that nobody wired up shows as a collection difference rather than
    as silence.
    """
    fixture_path = tmp_path / "payloads.json"
    fixture_path.write_text(json.dumps(component_fixtures()), encoding="utf-8")

    suites = sorted(path.name for path in (repo_root / "tests" / "js").glob("*.test.mjs"))
    assert suites, "no frontend test suites were found"

    completed = subprocess.run(
        [NODE, "--test", *(f"tests/js/{name}" for name in suites)],
        cwd=repo_root,
        env={**os.environ, "LS_PAYLOADS": str(fixture_path), "LS_CANARY": CANARY},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_node_is_available_wherever_the_suite_is_expected_to_run():
    """A skipped frontend suite is a frontend with no tests, so say so loudly.

    Skipping is right for a contributor who has no Node installed — the Python
    package does not need it — but CI installs it, and a silent skip there would
    mean the JavaScript stopped being tested without anybody noticing.
    """
    if os.environ.get("CI"):
        assert NODE is not None, "CI must run the frontend suite, and node is missing"
