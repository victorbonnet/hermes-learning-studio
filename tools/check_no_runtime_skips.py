"""Fail if any test in the runtime launch set was skipped.

A skipped test looks exactly like a passing one in a summary line, and the
tests most likely to skip are the ones that matter most here: they need
FastAPI and Uvicorn, and they are the only ones that start a real process.

CI installed only the ``dev`` extra for a while, so those tests skipped and a
green run said nothing whatever about whether an exercise could be launched.
Test-order pollution can do the same thing on a machine where the dependency
*is* installed — one module removing ``fastapi`` from ``sys.modules`` is
enough. So this asserts the outcome rather than the cause: these files run, and
nothing in them is skipped.

    uv run python tools/check_no_runtime_skips.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The suites that prove an exercise can actually be opened. Every one of them
#: must run in full.
REQUIRED = (
    "tests/test_runtime_state.py",
    "tests/test_runtime_bootstrap.py",
    "tests/test_runtime_supervisor.py",
    "tests/test_runtime_server.py",
    "tests/test_runtime_tunnel.py",
    "tests/test_runtime_cleanup.py",
    "tests/test_runtime_security.py",
    "tests/test_runtime_tools.py",
    "tests/test_launch.py",
    "tests/test_launch_sessions.py",
    "tests/test_consent_evidence.py",
    "tests/test_telegram_launch.py",
    "tests/test_hardening.py",
    "tests/test_end_to_end.py",
    "tests/test_mini_app_api.py",
)


def main() -> int:
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "report.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--no-header",
                f"--junit-xml={report.with_suffix('.xml')}",
                *REQUIRED,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        print(completed.stdout[-4000:])
        if completed.stderr.strip():
            print(completed.stderr[-2000:], file=sys.stderr)

        skipped = _skipped(report.with_suffix(".xml"))

    if completed.returncode != 0:
        print("the runtime launch suites did not pass", file=sys.stderr)
        return completed.returncode

    if skipped:
        print(
            "these runtime tests were skipped, so this run proves nothing about "
            "launching:\n  " + "\n  ".join(skipped),
            file=sys.stderr,
        )
        return 1

    print(f"every test in {len(REQUIRED)} runtime suites ran; none skipped.")
    return 0


def _skipped(report: Path) -> list[str]:
    """Every skipped case, by name, from the JUnit report."""
    import xml.etree.ElementTree as ElementTree

    if not report.is_file():
        return ["<no report was written>"]

    tree = ElementTree.parse(report)
    names = []
    for case in tree.iter("testcase"):
        if case.find("skipped") is not None:
            names.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return names


if __name__ == "__main__":
    raise SystemExit(main())
