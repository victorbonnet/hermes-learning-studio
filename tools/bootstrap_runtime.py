"""Prepare the Learning Studio runtime environment for the active profile.

    uv run python tools/bootstrap_runtime.py

One command, run once by an operator, with the output in front of them. It
creates a virtual environment under the active profile's Learning Studio
storage and installs the pinned dependencies the Mini App runtime serves with.

It does not touch Hermes' own environment, does not install an operating-system
package, does not ask for privilege, and does not download ``cloudflared`` —
that binary is an operator prerequisite, installed however you install software
and pointed at from ``config.yaml`` if it is not on ``PATH``.

Running it twice is safe: an environment that already matches the pinned
requirements is reported as current and rebuilt only with ``--force``.

The profile it prepares is the active one, which means ``HERMES_HOME`` (or the
profile Hermes has selected) decides where it lands. Two profiles get two
environments; preparing one says nothing about the other.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning_studio.runtime import bootstrap  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild even when the environment already matches the pinned requirements",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="delete this profile's runtime environment and stop",
    )
    arguments = parser.parse_args(argv)

    if arguments.remove:
        bootstrap.remove()
        print("Removed this profile's Learning Studio runtime environment.")
        return 0

    result = bootstrap.bootstrap(force=arguments.force)
    print(result.message)
    if result.detail:
        # An operator's own terminal is the one audience for this: they already
        # have the filesystem it describes. Nothing on the agent-facing path
        # ever sees it.
        print("\n--- output from the failing command ---", file=sys.stderr)
        print(result.detail, file=sys.stderr)
    if result.ok:
        print(f"Interpreter: {bootstrap.runtime_python()}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
