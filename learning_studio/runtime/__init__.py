"""The on-demand runtime: one local server and one temporary tunnel, per profile.

Everything in this package exists to answer one question safely — *may this
process be started, reused, or signalled on behalf of this profile?* — and the
answer is never taken on trust.

Three rules shape the whole package:

**Nothing here is model-controlled.** No address, port, executable, argument,
process id, timeout, lock path, or destination arrives from a tool payload.
They come from the operator's ``config.yaml`` (validated in
:mod:`learning_studio.config`) or from values this package generated itself.

**A process is signalled only when ownership is proved.** A process id is a
number the operating system reuses; on its own it proves nothing. Ownership
here is a live challenge — see :mod:`learning_studio.runtime.ownership` — that
an unrelated process holding a recycled id cannot answer.

**Importing this package stays cheap.** ``register(ctx)`` reaches
:func:`learning_studio.runtime.availability.runtime_tools_supported` and
nothing else, so enabling the plugin imports no FastAPI, no Uvicorn, and
starts nothing. The modules that need the ``web`` extra are imported inside the
child process that actually serves.
"""

from __future__ import annotations

__all__: list[str] = []
