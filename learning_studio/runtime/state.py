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
import errno
import json
import os
import secrets
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

#: A handshake is four small fields. Same reasoning, smaller bound.
MAX_HANDSHAKE_BYTES = 4096


class ContainmentError(RuntimeError):
    """A managed path is not where this profile's storage says it should be."""


def runtime_dir() -> Path:
    """Create and return ``<storage root>/runtime``, owner-only and contained.

    The containment check is not decoration. ``mkdir(exist_ok=True)`` follows an
    existing symlink, so a link planted at this path — by another profile, an
    unpacked archive, a careless sync tool — silently redirected the runtime
    record, the lock, the handshake, the bootstrap environment *and* the
    permission changes made to all of them somewhere else entirely. The record
    holds a control secret, so "somewhere else" is not an abstract problem.

    So: refuse a symlink at this component outright rather than resolving it,
    and then verify that what exists really does sit under the profile's own
    storage root. Both, because the first catches the plant and the second
    catches a link further up the path that this component knows nothing about.
    """
    root = ensure_storage_root()
    target = root / RUNTIME_SUBDIR

    with contextlib.suppress(FileExistsError):
        os.mkdir(str(target), DIRECTORY_MODE)

    # The check *is* the open. `O_NOFOLLOW` makes the kernel refuse a symbolic
    # link at the final component, so there is no window between "it is not a
    # link" and "use it" for one to be planted in — which a preceding
    # `is_symlink()` call, however careful, always left open.
    fd = _open_directory(target)
    try:
        _require_contained(root, target)
        _require_same_file(fd, target)
        # Through the descriptor, so this cannot be the moment somebody
        # substitutes a link and has another file's mode changed for them.
        with contextlib.suppress(OSError, NotImplementedError):
            os.fchmod(fd, DIRECTORY_MODE)
    finally:
        os.close(fd)
    return target


def _open_directory(target: Path) -> int:
    """A descriptor for ``target``, refusing a symbolic link at that name."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(str(target), flags)
    except OSError as exc:
        # ELOOP is what `O_NOFOLLOW` reports for a link; ENOTDIR is what
        # `O_DIRECTORY` reports for a file. Both mean the same thing here.
        raise ContainmentError(
            "the Learning Studio runtime directory is not a directory this "
            "plugin can use and was refused"
        ) from exc


def _require_same_file(fd: int, target: Path) -> None:
    """The descriptor and the verified pathname must be the same object.

    ``_require_contained`` resolves names; this compares inodes. Together they
    close the gap between "the path checks out" and "the descriptor I am about
    to write through is that path", which a rename between the two would
    otherwise open.
    """
    try:
        opened = os.fstat(fd)
        named = os.stat(str(target))
    except OSError as exc:  # pragma: no cover - unreadable path
        raise ContainmentError(
            "the Learning Studio runtime directory could not be verified"
        ) from exc
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise ContainmentError(
            "the Learning Studio runtime directory changed while it was being opened"
        )


@contextlib.contextmanager
def managed_dir():
    """A descriptor for the runtime directory, for the length of one operation.

    Every sensitive file this package writes is created, opened, chmod-ed,
    renamed and removed **relative to this descriptor**, never by pathname.

    That is the whole fix for a class of bug this module had throughout: a
    pathname check followed by a pathname operation is two lookups, and
    anything at all may happen between them. A link planted in that window
    redirected a write, or made ``chmod`` loosen the mode of a file belonging
    to somebody else. Relative operations resolve once, against a directory
    that has already been proved to be the right one.
    """
    fd = _open_directory(runtime_dir())
    try:
        yield fd
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


#: Flags for opening a managed file: never follow a link, never leak to a child.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def open_managed(name: str, flags: int, mode: int = FILE_MODE, *, dir_fd: int | None = None) -> int:
    """Open one file in the runtime directory, refusing a link at its name.

    A link is refused by the *kernel*, through ``O_NOFOLLOW``, rather than by
    an ``is_symlink()`` call a moment beforehand — so there is no window
    between the check and the open for one to appear in.

    The kernel reports that refusal as ``ELOOP``, which is translated back to
    :class:`ContainmentError` here. The distinction is worth keeping: "somebody
    put a link where this plugin's file goes" is a different event from "the
    file is not there", and only one of them is worth a person's attention.
    """
    _require_plain_name(name)
    opened = flags | _NOFOLLOW | os.O_CLOEXEC
    try:
        if dir_fd is not None:
            return os.open(name, opened, mode, dir_fd=dir_fd)
        with managed_dir() as directory:
            return os.open(name, opened, mode, dir_fd=directory)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ContainmentError(
                f"the Learning Studio {name} is a symbolic link and was refused"
            ) from exc
        raise


def read_managed(name: str, *, max_bytes: int) -> str | None:
    """The contents of a managed file, or ``None`` if there is nothing usable.

    Oversized is ``None`` rather than an error, and so is missing, unreadable,
    and "a symbolic link is sitting there": every one of them means the same
    thing to every caller here — there is no value to be had — and they are
    deliberately not distinguished, because a caller that branched on the
    difference would be branching on something an attacker chooses.
    """
    try:
        fd = open_managed(name, os.O_RDONLY)
    except (OSError, ContainmentError):
        return None
    try:
        if os.fstat(fd).st_size > max_bytes:
            return None
        return os.read(fd, max_bytes).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def write_managed(name: str, text: str) -> None:
    """Replace a managed file atomically, owner-only from the moment it exists.

    The temporary is created in the same directory — a rename is only atomic
    within one filesystem — and its mode is set through its own descriptor
    before the rename, so the file never exists at its final name with default
    permissions, not even for the width of one syscall.
    """
    _require_plain_name(name)
    payload = text.encode("utf-8")
    with managed_dir() as directory:
        temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            FILE_MODE,
            dir_fd=directory,
        )
        try:
            with contextlib.suppress(OSError, NotImplementedError):
                os.fchmod(fd, FILE_MODE)
            os.write(fd, payload)
            os.fsync(fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            raise
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
        try:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            raise


def remove_managed(name: str) -> None:
    """Delete a managed file. Idempotent."""
    _require_plain_name(name)
    with contextlib.suppress(OSError, ContainmentError), managed_dir() as directory:
        os.unlink(name, dir_fd=directory)


def _require_plain_name(name: str) -> None:
    """One path component, and not a traversal.

    Nothing here takes a name from a request — every caller passes a constant
    or a runtime id this package generated — so this is a guard against a
    future caller, not against today's input.
    """
    if not name or "/" in name or name in (".", ".."):
        raise ContainmentError("the Learning Studio was asked for a managed file by a bad name")


def _require_contained(root: Path, path: Path) -> None:
    """Refuse a path that does not physically live under the storage root.

    Resolved on both sides, so a link anywhere along the way is caught — not
    only one at the last component. A mismatch is a refusal rather than a
    correction: this plugin does not know why the path moved, and guessing is
    how a profile's data ends up in another profile's directory.
    """
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except OSError as exc:  # pragma: no cover - unreadable path
        raise ContainmentError(
            "the Learning Studio runtime directory could not be verified"
        ) from exc

    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ContainmentError(
            "the Learning Studio runtime directory resolves outside this profile's "
            "storage and was refused"
        )


def managed_path(name: str) -> Path:
    """One file inside the runtime directory, refused if it is a link.

    Every sensitive file this package writes goes through here: the record, the
    lock, the handshake, the bootstrap stamp. A symbolic link at any of them
    would redirect a write, a ``chmod``, or both — and ``chmod`` follows links,
    so loosening the mode of somebody else's file is one of the things that
    could happen.
    """
    path = runtime_dir() / name
    if path.is_symlink():
        raise ContainmentError(f"the Learning Studio {name} is a symbolic link and was refused")
    return path


def record_path() -> Path:
    return managed_path(RECORD_FILENAME)


def lock_path() -> Path:
    return managed_path(LOCK_FILENAME)


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
    raw = read_managed(RECORD_FILENAME, max_bytes=MAX_RECORD_BYTES)
    if raw is None:
        return None

    try:
        return parse_record(json.loads(raw))
    except (ValueError, CorruptRecord):
        return None


def write_record(record: RuntimeRecord) -> None:
    """Replace the record atomically, owner-only from the moment it exists.

    Written through a descriptor for the runtime directory rather than by
    pathname — see :func:`write_managed`. That matters more here than anywhere
    else in this package: the record holds a control secret, so a link that
    redirected this write, or a ``chmod`` that landed on the wrong file, would
    be handing that secret away.
    """
    write_managed(RECORD_FILENAME, json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True))


def clear_record() -> None:
    """Forget the runtime record. Idempotent, and signals nothing."""
    remove_managed(RECORD_FILENAME)


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

        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | _NOFOLLOW
        if self._path is not None:
            # A caller-supplied path, used only by tests that want a lock of
            # their own. Still no-follow; there is no directory descriptor to
            # be had for an arbitrary location.
            fd = os.open(str(self._path), flags, FILE_MODE)
        else:
            # Relative to a descriptor for the runtime directory, so a link
            # planted at the lock's name is refused by the kernel rather than
            # opened — and there is no pathname lookup left to race with.
            fd = open_managed(LOCK_FILENAME, os.O_RDWR | os.O_CREAT)
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
