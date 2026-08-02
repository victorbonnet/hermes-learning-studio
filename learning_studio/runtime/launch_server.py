"""The file the runtime interpreter is pointed at, and nothing more.

The supervisor starts the runtime as ``<venv python> <this file>``. Two
properties make that worth a file of its own:

**No code is passed on a command line.** ``python -c "…"`` would put a program
into the process table, where every other user on the machine can read it, and
would make the runtime's entry point a string this package constructs rather
than a file it ships. There is nothing here for anybody to influence: the
supervisor names a path, and the path is derived from this package's own
location.

**The package is found without ``PYTHONPATH``.** Running a script by path puts
*that script's directory* on ``sys.path``, not the package root, so the import
below would fail. Appending the root — rather than inserting it — is the whole
subtlety: the runtime virtual environment's own FastAPI and Uvicorn must win
over anything of the same name sitting beside the plugin, and an entry appended
after the environment's own ``site-packages`` cannot shadow them.

**The signal mask is cleared first.** A process inherits its parent's blocked
set across ``exec``, and the supervisor deliberately blocks interrupting
signals for the length of the spawn so a Ctrl-C cannot land between ``popen``
returning and the reference being stored. Nothing resets that for us —
``restore_signals`` restores dispositions, not the mask — so a runtime started
that way would ignore the ``SIGTERM`` its own shutdown depends on, and would
hand the same deafness to every process it starts, ``cloudflared`` included.
Clearing it here, before anything else runs, is what keeps the parent's
one-instruction safety measure from becoming the child's permanent condition.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

if hasattr(signal, "pthread_sigmask"):
    signal.pthread_sigmask(signal.SIG_SETMASK, set())

#: ``<root>/learning_studio/runtime/launch_server.py`` → ``<root>``.
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])

if _PACKAGE_ROOT not in sys.path:
    sys.path.append(_PACKAGE_ROOT)

from learning_studio.runtime.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
