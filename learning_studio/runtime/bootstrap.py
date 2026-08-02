"""Preparing the plugin's own runtime environment, once, on purpose.

The Mini App needs FastAPI and Uvicorn. Hermes' environment is not this
plugin's to modify, so they are installed into a virtual environment that
belongs to this profile's Learning Studio storage and to nothing else. Nothing
in the plugin's normal operation runs this: it is an explicit step an operator
takes, from a terminal, with the output in front of them.

What this deliberately does not do
----------------------------------

- **Install anything into Hermes' environment.** The venv is created *from* the
  running interpreter and is otherwise unrelated to it.
- **Install an operating-system package, or ask for privilege.** If a build
  needs a compiler that is not there, that is reported, not fixed.
- **Download ``cloudflared``.** Fetching and executing a binary on a user's
  machine is not a thing a learning plugin does quietly. The operator installs
  it however they install software, and points this plugin at it.
- **Run itself automatically.** A launch that found no environment reports that
  an operator needs to prepare one; it does not go and build one mid-session.
- **Run a shell.** Every command here is an argument array.

Repeatability
-------------

A stamp file records the requirements digest and the interpreter version the
environment was built for. A second bootstrap with both unchanged does nothing
and says so; a change to either rebuilds. That makes the step safe to put in a
setup script and safe to run twice.

Error reporting
---------------

:class:`BootstrapResult` carries a bounded tail of the failing command's
output, and the *operator-facing* CLI prints it — that reader already has the
filesystem this text would describe. The agent-facing path never sees it: a
launch that finds no environment reports
:data:`learning_studio.runtime.errors.NOT_BOOTSTRAPPED` and nothing else.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..paths import DIRECTORY_MODE, FILE_MODE
from .state import runtime_dir

logger = logging.getLogger(__name__)

#: The pinned direct dependencies, shipped inside the package so an installed
#: wheel can bootstrap without the repository.
REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"

VENV_DIRNAME = "venv"
STAMP_FILENAME = "venv.stamp"

#: Bounded so a pathological build log cannot become the return value.
MAX_DETAIL_CHARS = 4000

#: Creating a virtual environment is seconds; installing two wheels and their
#: dependencies is a minute on a slow link. Neither is unbounded.
VENV_TIMEOUT_SECONDS = 300
INSTALL_TIMEOUT_SECONDS = 900


def venv_dir() -> Path:
    return runtime_dir() / VENV_DIRNAME


def stamp_path() -> Path:
    return runtime_dir() / STAMP_FILENAME


def runtime_python() -> Path:
    """The interpreter that runs the Learning Studio runtime.

    A POSIX layout, because :mod:`learning_studio.runtime.ownership` already
    refuses to manage processes anywhere else.
    """
    return venv_dir() / "bin" / "python"


def requirements_digest() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _expected_stamp() -> dict[str, str]:
    return {
        "requirements": requirements_digest(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def is_bootstrapped() -> bool:
    """True when a usable environment for *these* requirements already exists.

    Both halves are checked. A stamp without an interpreter is a half-deleted
    environment; an interpreter without a matching stamp is one built for
    different requirements, and reporting it as current is how an operator ends
    up debugging a version they already upgraded.
    """
    if not runtime_python().is_file():
        return False
    try:
        recorded = json.loads(stamp_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(recorded, dict) and recorded == _expected_stamp()


@dataclass(frozen=True)
class BootstrapResult:
    """What a bootstrap did. ``detail`` is for an operator's terminal only."""

    #: ``created`` | ``current`` | ``failed``
    outcome: str
    message: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("created", "current")


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-MAX_DETAIL_CHARS:]


def _run(command: list[str], *, timeout: int, runner=subprocess.run) -> subprocess.CompletedProcess:
    """One bounded child process, built as an argument array.

    ``shell=False`` is the default and is never overridden anywhere in this
    package. There is no string here for a shell to reinterpret, so there is
    nothing to quote and nothing to get wrong.
    """
    logger.debug("runtime bootstrap running %s", command[0])
    return runner(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def bootstrap(*, runner=subprocess.run, force: bool = False) -> BootstrapResult:
    """Create or refresh the plugin-local runtime environment.

    ``runner`` is injected so the tests exercise every branch — success,
    failure, timeout, idempotence — without building a virtual environment or
    reaching a package index.
    """
    if not force and is_bootstrapped():
        return BootstrapResult(
            outcome="current",
            message="The Learning Studio runtime environment is already up to date.",
        )

    directory = runtime_dir()
    target = venv_dir()

    # Built from the interpreter that is running, which is the one whose
    # standard library the runtime will use. `--clear` makes a rebuild a
    # replacement rather than an upgrade of whatever was there.
    creation = _run(
        [sys.executable, "-m", "venv", "--clear", str(target)],
        timeout=VENV_TIMEOUT_SECONDS,
        runner=runner,
    )
    if creation.returncode != 0:
        return BootstrapResult(
            outcome="failed",
            message=(
                "The Learning Studio runtime environment could not be created. Check that "
                "this Python installation can create virtual environments."
            ),
            detail=_tail(creation.stderr or creation.stdout),
        )

    with contextlib.suppress(OSError, NotImplementedError):
        target.chmod(DIRECTORY_MODE)

    installation = _run(
        [
            str(runtime_python()),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--require-virtualenv",
            "--requirement",
            str(REQUIREMENTS),
        ],
        timeout=INSTALL_TIMEOUT_SECONDS,
        runner=runner,
    )
    if installation.returncode != 0:
        return BootstrapResult(
            outcome="failed",
            message=(
                "The Learning Studio runtime dependencies could not be installed. The "
                "environment was left in place so the output below can be read."
            ),
            detail=_tail(installation.stderr or installation.stdout),
        )

    _write_stamp(directory)
    return BootstrapResult(
        outcome="created",
        message="The Learning Studio runtime environment is ready.",
    )


def _write_stamp(directory: Path) -> None:
    """Record what was built, owner-only, after the build succeeded.

    Written last on purpose: a stamp that exists before the install finishes
    would describe an environment that does not work yet, and the next launch
    would believe it.
    """
    path = directory / STAMP_FILENAME
    path.write_text(json.dumps(_expected_stamp(), sort_keys=True), encoding="utf-8")
    with contextlib.suppress(OSError, NotImplementedError):
        path.chmod(FILE_MODE)


def remove() -> None:
    """Delete the runtime environment. Used by the tests, and by an operator.

    Deliberately narrow: it removes the directory this module created, under
    the profile's own storage root, and refuses anything else.
    """
    import shutil

    target = venv_dir()
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    with contextlib.suppress(OSError):
        os.unlink(stamp_path())
