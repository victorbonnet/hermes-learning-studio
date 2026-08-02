"""What the plugin remembers about a runtime it started, and how it is guarded.

Two files, both under the active profile's storage root, both owner-only:

``runtime/runtime.json``
    The record of the one runtime this profile started: where it listens, which
    process it is, which generation it belongs to, and the secret that proves a
    caller is entitled to ask it anything. Written by atomic replace so a reader
    never sees half a record, and never sees the previous record's port beside
    the new record's process id.

``runtime/runtime.lock``
    An advisory lock held only for the length of a start, stop, or reuse
    decision. It is what makes "check whether a runtime exists, then start one"
    a single step rather than two, which is the difference between one runtime
    per profile and as many as there are concurrent tool calls.

**The record is not the runtime.** A file cannot tell you whether a process is
alive, and it certainly cannot tell you whether the process now holding that
process id is the one you started. The record says what to *ask*;
:mod:`learning_studio.runtime.ownership` does the asking. Nothing in this
module signals anything.

**The record holds one secret**, ``control_token``, and it is the reason the
file is ``0600`` and the reason :meth:`RuntimeRecord.describe` exists. That
token is a local capability: presenting it to the runtime's control endpoint on
loopback is what distinguishes this plugin from any other process on the
machine that happens to guess the port. It is never logged, never returned to a
tool, never placed in a process argument, and never written anywhere but this
file.

**Generations are monotonic per profile.** A session, a grant, and a tunnel are
all bound to one generation, so a reply from a runtime that was replaced while
a request was in flight is recognised as stale rather than merged into the new
one.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import DIRECTORY_MODE, FILE_MODE, ensure_storage_root
from .errors import BUSY, RuntimeUnavailable

#: Subdirectory of the profile's Learning Studio storage root.
RUNTIME_SUBDIR = "runtime"

RECORD_FILENAME = "runtime.json"
LOCK_FILENAME = "runtime.lock"

#: Bumped when the on-disk shape changes. A record written by a different
#: version is not migrated: it is discarded, because the only thing it is used
#: for is deciding whether to signal a process, and a half-understood record is
#: exactly the input that must not reach that decision.
RECORD_SCHEMA = 1

#: 32 bytes of entropy, URL-safe, so it can sit in an HTTP header.
_CONTROL_TOKEN_BYTES = 32

#: A record any larger than this is not one this plugin wrote.
MAX_RECORD_BYTES = 8192


def runtime_dir() -> Path:
    """Create and return ``<storage root>/runtime``, owner-only."""
    root = ensure_storage_root() / RUNTIME_SUBDIR
    root.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    with contextlib.suppress(OSError, NotImplementedError):
        root.chmod(DIRECTORY_MODE)
    return root


def record_path() -> Path:
    return runtime_dir() / RECORD_FILENAME


def lock_path() -> Path:
    return runtime_dir() / LOCK_FILENAME


def new_control_token() -> str:
    return secrets.token_urlsafe(_CONTROL_TOKEN_BYTES)


@dataclass(frozen=True)
class RuntimeRecord:
    """One runtime this profile started, as far as the filesystem knows.

    Frozen because a record is a statement about a past event. Changing the
    port on a record and writing it back would describe a runtime that never
    existed; a new runtime gets a new record and a new generation.
    """

    #: Unique to this start. Two runtimes never share one, so a control reply
    #: quoting it could only have come from the process we started.
    runtime_id: str
    #: Monotonic within the profile. Sessions and grants carry it.
    generation: int
    profile: str
    pid: int
    host: str
    port: int
    #: Loopback capability for the control endpoint. Secret — see the module
    #: docstring, and never put it in ``describe()``.
    control_token: str
    #: Interpreter that was executed. Compared against the control reply, so a
    #: process pretending to be the runtime has to be running from the same
    #: place as well as knowing the token.
    executable: str
    started_at: float
    idle_timeout_seconds: int
    max_lifetime_seconds: int

    @property
    def control_url(self) -> str:
        """The loopback URL of the control endpoint.

        Built from validated configuration and an integer, never from anything
        a caller supplied. IPv6 literals are bracketed so the authority parses.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": RECORD_SCHEMA,
            "runtime_id": self.runtime_id,
            "generation": self.generation,
            "profile": self.profile,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "control_token": self.control_token,
            "executable": self.executable,
            "started_at": self.started_at,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "max_lifetime_seconds": self.max_lifetime_seconds,
        }

    def describe(self) -> dict[str, Any]:
        """The half of this record that may be shown to anybody.

        Absent, deliberately: the control token, the interpreter path, the
        listen address, and the port. The first is a credential; the rest are
        an operator's filesystem layout and a local attack surface, and none of
        them is a thing an agent can act on.
        """
        return {
            "generation": self.generation,
            "started_at": self.started_at,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "max_lifetime_seconds": self.max_lifetime_seconds,
        }


class CorruptRecord(ValueError):
    """The record on disk is not one this plugin can act on."""


def parse_record(raw: Any) -> RuntimeRecord:
    """Build a record from parsed JSON, or refuse.

    Every field is checked for type *and* range. This is the input to a
    decision about signalling a process, so "close enough" is not a standard
    that applies: a record whose port is a string, whose pid is negative, or
    whose schema is from a future release is discarded rather than coerced.
    """
    if not isinstance(raw, dict):
        raise CorruptRecord("record is not an object")
    if raw.get("schema") != RECORD_SCHEMA:
        raise CorruptRecord("record schema is not the one this release writes")

    def text(key: str, *, max_chars: int) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
            raise CorruptRecord(f"{key} is not a usable string")
        return value

    def whole(key: str, *, low: int, high: int) -> int:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise CorruptRecord(f"{key} is not a usable integer")
        return value

    started_at = raw.get("started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        raise CorruptRecord("started_at is not a timestamp")

    return RuntimeRecord(
        runtime_id=text("runtime_id", max_chars=128),
        generation=whole("generation", low=1, high=2**31),
        profile=text("profile", max_chars=256),
        pid=whole("pid", low=1, high=2**31),
        host=text("host", max_chars=64),
        port=whole("port", low=1, high=65535),
        control_token=text("control_token", max_chars=256),
        executable=text("executable", max_chars=4096),
        started_at=float(started_at),
        idle_timeout_seconds=whole("idle_timeout_seconds", low=1, high=86_400),
        max_lifetime_seconds=whole("max_lifetime_seconds", low=1, high=86_400),
    )


def read_record() -> RuntimeRecord | None:
    """The current record, or ``None`` when there is none this plugin can use.

    A missing file, an unreadable one, an oversized one, one that is not JSON,
    and one whose fields do not check out all give the same answer, because
    they call for the same behaviour: treat the profile as having no runtime,
    and — critically — signal nothing. A corrupt record is the one input that
    must never be half-believed, since the half a reader might keep is a
    process id.
    """
    path = record_path()
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None

    try:
        return parse_record(json.loads(raw))
    except (ValueError, CorruptRecord):
        return None


def write_record(record: RuntimeRecord) -> None:
    """Replace the record atomically, owner-only from the moment it exists.

    The temporary file is created in the same directory — a rename is only
    atomic within one filesystem — and its mode is set *before* the rename, so
    the record never exists at its final name with default permissions, not
    even for the width of one syscall. That matters more here than in most
    places: for that instant the file would be a world-readable control token.
    """
    directory = runtime_dir()
    payload = json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True)

    handle, temporary = tempfile.mkstemp(dir=str(directory), prefix=".runtime-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        with contextlib.suppress(OSError, NotImplementedError):
            os.chmod(temporary, FILE_MODE)
        os.replace(temporary, record_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def clear_record() -> None:
    """Forget the runtime record. Idempotent, and signals nothing."""
    with contextlib.suppress(OSError):
        record_path().unlink()


def next_generation(previous: RuntimeRecord | None) -> int:
    """The generation a new runtime should carry.

    Derived from the record being replaced rather than from a counter file, so
    there is one fewer thing to keep consistent. A profile that has never run
    one starts at 1.
    """
    if previous is None:
        return 1
    return min(previous.generation + 1, 2**31 - 1)


class ProfileLock:
    """Exclusive, advisory, released by the kernel when this process ends.

    ``flock`` rather than a lock *file whose existence* means "locked": a
    process that is killed between creating such a file and removing it leaves
    a lock nobody can release, and the usual repair — "delete it if it looks
    old" — is a race with a fresh start. An advisory lock has no stale state to
    reason about, because the operating system drops it when the holder dies.

    Held only across a decision, never for the life of a runtime. The runtime
    outlives the Hermes process that started it; the lock must not, or a Hermes
    restart would be indistinguishable from a crash mid-start.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> ProfileLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def acquire(self) -> None:
        """Take the lock, or raise :class:`RuntimeUnavailable` immediately.

        Non-blocking on purpose. A tool call that waits on a lock held by a
        start that is itself waiting on a readiness timeout would appear to the
        learner as a session that has simply stopped responding; telling the
        agent "busy, try again" is both truthful and actionable.
        """
        import fcntl

        path = self._path or lock_path()
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeUnavailable(BUSY, reason="runtime_locked") from exc
        self._fd = fd

    def release(self) -> None:
        """Drop the lock. Idempotent, and safe to call after a failed acquire."""
        import fcntl

        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
