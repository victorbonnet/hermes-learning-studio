"""The Telegram Mini App API.

Everything in this package is behind the optional ``web`` extra and is
imported by nothing else in the plugin. ``register(ctx)`` does not reach it,
so an install that never opted into FastAPI keeps a working plugin — see
``tests/test_import_isolation.py``.

The public entry point is :func:`learning_studio.web.app.create_app`. It is
deliberately *not* re-exported here: importing this package must stay free of
FastAPI so that a caller can ask whether the extra is installed without
triggering the import it is asking about.
"""

from __future__ import annotations

__all__ = ["extra_is_installed"]


def extra_is_installed() -> bool:
    """True when the ``web`` extra's dependencies are importable.

    ``find_spec`` does not merely return ``None`` for a package that cannot be
    found: a broken or shadowed installation makes it raise. Either way the
    honest answer to "can this be imported?" is no.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("fastapi") is not None
    except (ImportError, ValueError):
        return False
