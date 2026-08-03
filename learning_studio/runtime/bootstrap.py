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
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..paths import DIRECTORY_MODE
from .state import managed_path, read_managed, remove_managed, runtime_dir, write_managed

logger = logging.getLogger(__name__)

#: The pinned direct dependencies, shipped inside the package so an installed
#: wheel can bootstrap without the repository.
REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"

VENV_DIRNAME = "venv"
STAMP_FILENAME = "venv.stamp"

#: A stamp is two short strings. Anything larger is not one this module wrote.
MAX_STAMP_BYTES = 4096

#: Bounded so a pathological build log cannot become the return value.
MAX_DETAIL_CHARS = 4000

#: Creating a virtual environment is seconds; installing two wheels and their
#: dependencies is a minute on a slow link. Neither is unbounded.
VENV_TIMEOUT_SECONDS = 300
INSTALL_TIMEOUT_SECONDS = 900


def venv_dir() -> Path:
    """The plugin-local environment, refused if something has linked it away.

    A symbolic link here would put a virtual environment — and therefore an
    *interpreter this package later executes* — somewhere the profile does not
    control. That is the one path in this module where following a link would
    change what code runs.
    """
    return managed_path(VENV_DIRNAME)


def stamp_path() -> Path:
    return managed_path(STAMP_FILENAME)


def runtime_python() -> Path:
    """The interpreter that runs the Learning Studio runtime.

    A POSIX layout, because :mod:`learning_studio.runtime.ownership` already
    refuses to manage processes anywhere else.

    This is the *name*. Anything that is about to execute it calls
    :func:`verified_runtime_python`, which is the one that proves the name
    leads where it should.
    """
    return venv_dir() / "bin" / "python"


def verified_runtime_python() -> Path:
    """The interpreter, with the directories leading to it proved unredirected.

    ``runtime`` → ``venv`` → ``bin`` are opened ``O_NOFOLLOW`` relative to one
    another, so no *directory* on the way can be a symbolic link. A link at any
    of them would move the entire environment somewhere the profile does not
    control — the accident an unpacked archive or a careless sync tool actually
    produces — and this refuses it in a single resolution with no name lookup
    left to race.

    **The interpreter itself may be a link, because that is what a virtual
    environment is.** ``python -m venv`` and ``uv venv`` both create
    ``bin/python`` as a symlink — to ``python3.11``, and that to the base
    interpreter *outside* managed storage. An earlier version of this function
    opened the last component ``O_NOFOLLOW`` too, which sounds stricter and in
    fact refused every environment this plugin builds: the whole managed path
    reported ``runtime_not_bootstrapped`` and no launch could happen. Nothing
    caught it because every test wrote a plain file there.

    So the last component is followed, and what is required of it is what can
    honestly be required: that it resolves to a regular file with an execute
    bit. Where it points is not constrained, because a venv legitimately points
    out of the profile.

    **The trust boundary, stated exactly.** What is established is that at the
    moment of the check, no directory component was a link, and the final
    target was an executable regular file. Nothing is established about the
    instant after: the spawn resolves the path again, and a replacement made in
    between is executed. That gap cannot be closed here — holding the
    descriptor open pins the file but not the *name*, and executing the
    descriptor through ``/proc/self/fd/N`` pins the file but puts it in a
    directory with no ``pyvenv.cfg``, so CPython stops recognising a virtual
    environment and the runtime loses every dependency installed for it. That
    was tried; it is why this returns a path.

    Nor is anything established against somebody who can already write inside
    ``venv/bin``. They can replace the interpreter's target, its contents, or
    ``pyvenv.cfg``, and no path handling sees it. What proves the environment
    is one this plugin built is the stamp — see :func:`is_bootstrapped` — not
    this function.
    """
    from .state import ContainmentError, managed_dir, open_managed

    with managed_dir() as runtime:
        venv = open_managed(VENV_DIRNAME, os.O_RDONLY | _O_DIRECTORY, dir_fd=runtime)
        try:
            binaries = open_managed("bin", os.O_RDONLY | _O_DIRECTORY, dir_fd=venv)
        except OSError as exc:
            raise ContainmentError("the Learning Studio runtime environment is incomplete") from exc
        finally:
            os.close(venv)

    try:
        # Followed on purpose: see above. `bin` was opened no-follow, so this
        # resolves from a directory that is provably the managed one.
        interpreter = os.open("python", os.O_RDONLY | os.O_CLOEXEC, dir_fd=binaries)
    finally:
        os.close(binaries)

    try:
        info = os.fstat(interpreter)
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise ContainmentError(
                "the Learning Studio runtime interpreter is not an executable file"
            )
    finally:
        os.close(interpreter)

    return runtime_python()


def requirements_digest() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _expected_stamp() -> dict[str, str]:
    return {
        "requirements": requirements_digest(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


#: ``O_DIRECTORY`` where the platform has it, so a file where a directory
#: belongs is refused by the kernel rather than by a later, vaguer error.
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def is_bootstrapped() -> bool:
    """True when a usable environment for *these* requirements already exists.

    Both halves are checked. A stamp without an interpreter is a half-deleted
    environment; an interpreter without a matching stamp is one built for
    different requirements, and reporting it as current is how an operator ends
    up debugging a version they already upgraded.
    """
    from .state import ContainmentError

    try:
        verified_runtime_python()
    except (OSError, ContainmentError):
        return False
    raw = read_managed(STAMP_FILENAME, max_bytes=MAX_STAMP_BYTES)
    if raw is None:
        return False
    try:
        recorded = json.loads(raw)
    except ValueError:
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

    _tighten_venv_permissions()

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


def _tighten_venv_permissions() -> None:
    """Make the environment owner-only, through a descriptor. Best effort.

    ``fchmod`` on a descriptor rather than ``chmod`` on a name, so a link
    planted at ``venv`` cannot have somebody else's directory relaxed for them.

    Best effort in the sense the previous ``chmod`` was: a mode that could not
    be tightened is not a reason to fail a bootstrap that otherwise worked, and
    the environment is created inside a storage root that is already owner-only.
    Letting an error escape here would turn a cosmetic failure into "the
    Learning Studio cannot be installed".
    """
    from .state import ContainmentError, managed_dir, open_managed

    with contextlib.suppress(OSError, NotImplementedError, ContainmentError):
        with managed_dir() as runtime_fd:
            target_fd = open_managed(VENV_DIRNAME, os.O_RDONLY | _O_DIRECTORY, dir_fd=runtime_fd)
        try:
            os.fchmod(target_fd, DIRECTORY_MODE)
        finally:
            os.close(target_fd)


def _write_stamp(directory: Path) -> None:
    """Record what was built, owner-only, after the build succeeded.

    Written last on purpose: a stamp that exists before the install finishes
    would describe an environment that does not work yet, and the next launch
    would believe it.

    Through a descriptor for the runtime directory rather than by pathname, for
    the same reason as the record: a link planted at the stamp's name would
    otherwise have redirected both the write and the ``chmod`` that follows it.
    """
    del directory
    write_managed(STAMP_FILENAME, json.dumps(_expected_stamp(), sort_keys=True))


def remove() -> None:
    """Delete the runtime environment. Used by the tests, and by an operator.

    Deliberately narrow: it removes the directory this module created, under
    the profile's own storage root, and refuses anything else.
    """
    import shutil

    target = venv_dir()
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    remove_managed(STAMP_FILENAME)
