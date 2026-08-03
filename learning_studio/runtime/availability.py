"""Whether the runtime tools should be offered at all, decided cheaply.

Hermes calls a tool's ``check_fn`` to decide whether to put it in front of the
model, caches the answer, and calls it again later. So it has to be fast, it has
to be free of side effects, and it must not import anything optional — a check
that imported FastAPI in order to report that FastAPI is missing would be the
thing it is checking for.

The gate here is deliberately *only* the platform. It is the one condition that
cannot be fixed from inside a conversation: without process groups this package
cannot prove which process it owns, and it will not signal one it cannot prove.
Everything else that can go wrong — the runtime environment not prepared,
``cloudflared`` not installed, no Telegram destination — is reported by the
handler as an explanation the agent can act on and pass to the learner. Gating
on those instead would make the tools silently vanish, and an agent cannot
explain the absence of a tool it cannot see.
"""

from __future__ import annotations

import os


def runtime_tools_supported() -> bool:
    """True when this operating system can own a process safely.

    Mirrors :func:`learning_studio.runtime.ownership.platform_supported`, which
    is the function that actually guards the signalling, and is kept here as a
    separate two-line implementation so that ``check_fn`` costs one attribute
    lookup rather than an import of the control-plane module.
    """
    return (
        os.name == "posix"
        and hasattr(os, "killpg")
        and hasattr(os, "getpgid")
        and hasattr(os, "setsid")
    )
