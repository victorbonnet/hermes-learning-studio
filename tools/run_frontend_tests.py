"""Run the frontend test suite: generate fixtures, then invoke Node.

    uv run python tools/run_frontend_tests.py

This is *the* command. It is one command on purpose.

The suite is driven by fixtures built from the real component registry through
the real ``build_component``, so the JavaScript is exercised against exactly the
learner projection the API returns — canaries in every hidden field included. A
committed JSON fixture would have started drifting from the registry the first
time a component type changed, and nobody would have noticed until the canary
tests were checking for a marker that no longer existed.

Which is why ``node --test tests/js/*.test.mjs`` on its own does not work and
should not be documented as though it does: the fixtures have to be generated
first, by Python, and handed over. The suite says so if you try — it fails with a
message naming this script rather than with an unexplained ``undefined``.

``tests/test_frontend_js.py`` calls the same function this script does, so the
documented command and the pytest run cannot diverge.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning_studio.components import COMPONENT_TYPES, build_component  # noqa: E402
from tests.component_examples import CANARY, example  # noqa: E402

SUITE_DIR = REPO_ROOT / "tests" / "js"


def component_fixtures() -> dict[str, dict]:
    """One API-shaped component per registry type, keyed by type.

    Shaped exactly as ``GET /api/session/component`` returns it, because that is
    what ``render()`` is given: a validated component, projected for a learner,
    with the evaluator-only half absent rather than removed.
    """
    fixtures: dict[str, dict] = {}
    for position, component_type in enumerate(COMPONENT_TYPES):
        component = build_component(example(component_type), f"components[{position}]")
        fixtures[component_type] = {
            "position": position,
            "component_id": f"component-{position}",
            "type": component_type,
            "payload": component.learner_payload(),
        }
    return fixtures


def suite_files() -> list[str]:
    """The suites, named explicitly.

    ``node --test`` has changed its mind about directories and globs across
    releases, and this repository runs on whatever Node a contributor happens to
    have. An explicit list behaves the same everywhere.
    """
    return sorted(f"tests/js/{path.name}" for path in SUITE_DIR.glob("*.test.mjs"))


def run(node: str, extra_args: list[str] | None = None) -> int:
    """Generate the fixtures into a temporary file and run the suites."""
    suites = suite_files()
    if not suites:
        print("no frontend test suites were found in tests/js/", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as scratch:
        fixture_path = Path(scratch) / "payloads.json"
        fixture_path.write_text(json.dumps(component_fixtures()), encoding="utf-8")

        completed = subprocess.run(
            [node, "--test", *suites, *(extra_args or [])],
            cwd=REPO_ROOT,
            env={**os.environ, "LS_PAYLOADS": str(fixture_path), "LS_CANARY": CANARY},
            check=False,
        )
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Mini App frontend test suite.")
    parser.add_argument("node_args", nargs="*", help="extra arguments passed through to node")
    args = parser.parse_args()

    node = shutil.which("node")
    if node is None:
        print(
            "node is not installed. The Python package does not need it; the frontend\n"
            "suite does. Install Node 18 or newer and run this again.",
            file=sys.stderr,
        )
        return 1

    return run(node, args.node_args)


if __name__ == "__main__":
    raise SystemExit(main())
