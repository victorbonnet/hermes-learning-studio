"""Profile-safe path resolution.

Hermes supports multiple profiles by pointing ``HERMES_HOME`` at different
directories, so every path this plugin touches has to be derived from the
host's own resolver rather than from ``~/.hermes`` or the process CWD. Getting
this wrong does not fail loudly — it silently mixes one profile's learners
with another's, which is exactly the isolation failure the storage layer is
built to prevent.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

#: Everything this plugin persists lives under this subtree of ``HERMES_HOME``.
STORAGE_SUBPATH = ("workspace", "learning-studio")

#: Owner-only. Learning data is personal, and a shared machine should not be
#: able to read one user's goals out of another's Hermes home.
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


def hermes_home() -> Path:
    """Return the active Hermes home directory.

    Delegates to the host's ``get_hermes_home()``, which resolves the
    context-local profile override before the ``HERMES_HOME`` env var — a
    distinction that matters inside gateway and kanban worker sessions, where
    the env var alone reflects the wrong profile.

    The import is deliberately lazy and deliberately caught: this package must
    stay importable outside a Hermes process (the test suite, ``uv build``,
    a bare ``import learning_studio``). The fallback reads the same env var
    the host reads, so the two agree in every context that sets it.
    """
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return _hermes_home_from_env()

    try:
        return Path(get_hermes_home())
    except Exception:  # pragma: no cover - defensive: never fail path resolution
        return _hermes_home_from_env()


def _hermes_home_from_env() -> Path:
    """Mirror of the host's own env-var resolution, used when it is absent."""
    value = os.environ.get("HERMES_HOME", "").strip()
    if value:
        return Path(value)
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def profile_id() -> str:
    """Return a stable identifier for the active Hermes profile.

    Used as an explicit scope column on every profile-owned row. The database
    is already physically profile-isolated (it lives under ``HERMES_HOME``),
    so this is defence in depth: if a future release ever supports a shared
    database, the queries are already scoped and do not need revisiting.

    Falls back to ``"default"`` rather than raising — an unknown profile name
    must not take down the plugin, and the physical isolation still holds.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name

        name = str(get_active_profile_name()).strip()
        return name or "default"
    except Exception:
        return "default"


def storage_root() -> Path:
    """Return ``$HERMES_HOME/workspace/learning-studio``."""
    return hermes_home().joinpath(*STORAGE_SUBPATH)


def ensure_storage_root() -> Path:
    """Create the storage root if needed and return it.

    ``mode=`` on ``mkdir`` is masked by the process umask, so the permissions
    are re-applied explicitly afterwards. Failure to tighten them is not fatal
    — some filesystems (Windows, FAT, network mounts) do not implement POSIX
    modes — but on a POSIX filesystem it must take effect.
    """
    root = storage_root()
    root.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    _restrict(root, DIRECTORY_MODE)
    return root


def _restrict(path: Path, mode: int) -> None:
    # Platform dependent: FAT, Windows, and some network mounts have no POSIX
    # modes. Failing to tighten them there is expected, not an error.
    with contextlib.suppress(OSError, NotImplementedError):
        path.chmod(mode)


def resolve_within_storage(*parts: str) -> Path:
    """Resolve a path under the storage root, refusing to escape it.

    Callers supply names that originate from tool arguments, so they are
    untrusted: ``..`` segments, absolute paths, and symlinks that point
    outside the root are all rejected rather than normalised.
    """
    root = storage_root()
    candidate = root.joinpath(*parts)

    resolved_root = _resolve_existing(root)
    resolved = _resolve_existing(candidate)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("path escapes the Learning Studio storage root")
    return candidate


def _resolve_existing(path: Path) -> Path:
    """``Path.resolve()`` that tolerates the path not existing yet."""
    return path.resolve() if path.exists() else Path(os.path.normpath(str(path.absolute())))
