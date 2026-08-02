"""Proving that a process belongs to this plugin, before anything is signalled.

A process id is not identity. The operating system reuses them, quickly on a
busy machine, and the gap between "the runtime this profile started" and "the
process that now holds that number" is where a plugin that trusts a saved pid
kills somebody's database.

So nothing here trusts the saved pid. Ownership is a **challenge**, and it is
proved by the process answering it:

1. The plugin generates a secret when it starts a runtime and hands it to the
   child through its environment — never through an argument, where every other
   user on the machine could read it out of the process table.
2. The child serves one control endpoint, on loopback only, which answers
   nothing at all without that secret.
3. To prove ownership, the plugin asks that endpoint who it is. A reply is
   accepted only when the runtime id, the generation, the process id, and the
   interpreter path all match the record — so the process answering has to be
   running from where we started it, *and* know a secret nobody else was told,
   *and* claim exactly the pid we are about to signal.

An unrelated process that inherited the pid fails at step 3 because it cannot
produce the secret. A process that somehow read the secret still fails unless
it is also listening on the loopback port we recorded and reports our pid. A
runtime that has died fails because nothing answers — and a dead runtime is
never signalled, which is the whole point.

**Stopping goes through the same door.** The first move is not a signal at all:
it is an authenticated request asking the runtime to shut itself down, which
needs no inference about process identity because the runtime does the work.
Signals are the escalation, they go to the runtime's own process *group* — the
child is started in a new session, so that group contains it and its
descendants and nothing else — and they are sent only after ownership has been
proved again immediately beforehand.

**Where this cannot work, it says so.** Process groups and ``killpg`` are POSIX.
On a platform without them there is no weaker fallback here, because the weaker
fallback is "signal the pid and hope", and hoping is what this module exists to
replace.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Any

from .errors import UNPROVABLE, RuntimeUnavailable
from .state import RuntimeRecord

logger = logging.getLogger(__name__)

#: The header the control secret travels in. A header rather than a query
#: parameter: query strings are logged by every intermediary that has ever
#: existed, and this one is a capability.
CONTROL_HEADER = "X-Learning-Studio-Control"

#: The control endpoints. Under ``/internal`` so no route the Mini App serves
#: can ever collide with one, and every one of them requires the secret.
STATUS_PATH = "/internal/runtime"
SHUTDOWN_PATH = "/internal/shutdown"
GRANT_PATH = "/internal/grant"
GRANT_ACTIVATE_PATH = "/internal/grant/activate"
GRANT_REVOKE_PATH = "/internal/grant/revoke"
LAUNCH_PATH = "/internal/launch"

#: A control reply is a small fixed object. Anything larger is not one.
MAX_CONTROL_RESPONSE_BYTES = 64 * 1024

#: Loopback round trips are sub-millisecond when they work at all.
DEFAULT_CONTROL_TIMEOUT_SECONDS = 5.0


def platform_supported() -> bool:
    """True when this operating system can supply the ownership primitives.

    Checked by attribute rather than by name, so a POSIX-like platform that
    really does have process groups is not excluded for failing a string
    comparison against ``sys.platform``.
    """
    return (
        os.name == "posix"
        and hasattr(os, "killpg")
        and hasattr(os, "getpgid")
        and hasattr(os, "setsid")
    )


@dataclass(frozen=True)
class ControlReply:
    """What a runtime says about itself when properly asked."""

    runtime_id: str
    generation: int
    pid: int
    executable: str
    started_at: float
    #: Seconds since the last *authenticated learner* request. ``None`` before
    #: any learner has arrived.
    idle_seconds: float | None
    #: ``starting`` | ``ready`` | ``stopping``
    server_state: str
    #: ``pending`` | ``ready`` | ``failed``
    tunnel_state: str
    #: True once the tunnel has published a URL this plugin accepted.
    tunnel_ready: bool
    #: The public address, or ``""``. Re-validated on the way in — the runtime
    #: already checked it, and checking it again here costs nothing and means
    #: the rule lives in one module rather than in one module *and* a promise
    #: about another process. It is never included in :meth:`describe`.
    tunnel_url: str
    payload: dict[str, Any]

    def describe(self) -> dict[str, Any]:
        """Lifecycle state safe to show an agent. No address, no URL."""
        return {
            "server_state": self.server_state,
            "tunnel_state": self.tunnel_state,
            "tunnel_ready": self.tunnel_ready,
        }

    def matches(self, record: RuntimeRecord) -> bool:
        """Every identifying field agrees with the record, or this is not ours."""
        return (
            self.runtime_id == record.runtime_id
            and self.generation == record.generation
            and self.pid == record.pid
            and self.executable == record.executable
        )


class ControlError(Exception):
    """The control endpoint could not be reached, or answered unusably."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _request(
    record: RuntimeRecord,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = DEFAULT_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One control request, bounded in time and in size.

    Every failure — refused connection, timeout, wrong status, oversized body,
    unparseable JSON — raises :class:`ControlError` with a fixed reason. None of
    them carries the response text: a control endpoint that has gone wrong may
    be answering with anything at all, including another program's error page,
    and this plugin does not relay strings it did not write.
    """
    connection = http.client.HTTPConnection(record.host, record.port, timeout=timeout)
    try:
        payload = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        headers = {CONTROL_HEADER: record.control_token, "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_CONTROL_RESPONSE_BYTES + 1)
        if response.status != 200:
            raise ControlError(f"control_status_{response.status}")
        if len(raw) > MAX_CONTROL_RESPONSE_BYTES:
            raise ControlError("control_response_too_large")
    except ControlError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise ControlError(f"control_unreachable_{type(exc).__name__}") from exc
    finally:
        with contextlib.suppress(Exception):
            connection.close()

    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ControlError("control_response_not_json") from exc
    if not isinstance(parsed, dict):
        raise ControlError("control_response_not_an_object")
    return parsed


def _reply_from(payload: dict[str, Any]) -> ControlReply:
    """Validate a control payload into a reply, or refuse it."""

    def text(key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ControlError(f"control_field_{key}")
        return value

    def whole(key: str) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ControlError(f"control_field_{key}")
        return value

    idle = payload.get("idle_seconds")
    if idle is not None and (isinstance(idle, bool) or not isinstance(idle, (int, float))):
        raise ControlError("control_field_idle_seconds")
    started_at = payload.get("started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        raise ControlError("control_field_started_at")

    raw_url = payload.get("tunnel_url") or ""
    tunnel_url = ""
    if raw_url:
        from .tunnel import TunnelError, validate_quick_tunnel_url

        try:
            tunnel_url = validate_quick_tunnel_url(raw_url)
        except TunnelError as exc:
            # A runtime reporting a URL this side will not accept is a runtime
            # whose tunnel is not usable. Refused rather than passed along, and
            # the offending string is not quoted.
            raise ControlError(f"control_{exc.reason}") from exc

    return ControlReply(
        runtime_id=text("runtime_id"),
        generation=whole("generation"),
        pid=whole("pid"),
        executable=text("executable"),
        started_at=float(started_at),
        idle_seconds=None if idle is None else float(idle),
        server_state=text("server_state"),
        tunnel_state=text("tunnel_state"),
        tunnel_ready=bool(payload.get("tunnel_ready")),
        tunnel_url=tunnel_url,
        payload=payload,
    )


def query(
    record: RuntimeRecord, *, timeout: float = DEFAULT_CONTROL_TIMEOUT_SECONDS
) -> ControlReply:
    """Ask the runtime who it is, and refuse an answer that is not ours."""
    reply = _reply_from(_request(record, "GET", STATUS_PATH, timeout=timeout))
    if not reply.matches(record):
        # Something is listening on that loopback port, knows the secret, and
        # is still not the process we recorded. Whatever that is, it is not the
        # thing this plugin may act on.
        raise ControlError("control_identity_mismatch")
    return reply


def owned(record: RuntimeRecord, *, timeout: float = DEFAULT_CONTROL_TIMEOUT_SECONDS) -> bool:
    """True when the recorded runtime is alive and proved to be this plugin's."""
    try:
        query(record, timeout=timeout)
    except ControlError as exc:
        logger.debug("runtime ownership not proved: %s", exc.reason)
        return False
    return True


def call(
    record: RuntimeRecord,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Make an authenticated control call, having first proved ownership.

    The proof is not a formality that could be skipped for a request that
    "only reads": every one of these endpoints acts on a learner's session, and
    acting on the session of a runtime that is not ours is the failure this
    module exists to prevent.
    """
    query(record, timeout=timeout)
    return _request(record, "POST", path, body=body, timeout=timeout)


# ── Stopping ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StopOutcome:
    """What a stop actually did, in terms a caller may report."""

    #: ``not_running`` | ``stopped`` | ``unprovable``
    result: str
    #: ``control`` | ``sigterm`` | ``sigkill`` | ``none``
    method: str

    @property
    def stopped(self) -> bool:
        return self.result == "stopped"


def stop_owned(
    record: RuntimeRecord,
    *,
    graceful_seconds: int,
    clock=time.monotonic,
    sleep=time.sleep,
) -> StopOutcome:
    """Stop the recorded runtime, or leave it strictly alone.

    The order is the safety property, not an optimisation:

    1. **Prove ownership.** If that fails, nothing is signalled and the outcome
       is ``unprovable``. A runtime that is dead and one that is wedged look the
       same from here, and the only safe response to both is to touch neither.
    2. **Ask it to stop.** An authenticated request the runtime services itself,
       which shuts down its tunnel child before it goes.
    3. **Wait**, bounded, for the control endpoint to stop answering.
    4. **Escalate**, and only then, to the runtime's process *group* — proved
       ours again immediately before each signal, and confirmed to be a group
       whose leader is that same process, so the signal cannot travel outside
       the session this plugin created.

    Idempotent by construction: a second call finds nothing to prove and
    returns ``not_running``.
    """
    if not platform_supported():  # pragma: no cover - exercised on POSIX only
        return StopOutcome(result="unprovable", method="none")

    if not owned(record):
        return StopOutcome(result="not_running", method="none")

    with contextlib.suppress(ControlError):
        _request(record, "POST", SHUTDOWN_PATH, body={})

    deadline = clock() + max(1, int(graceful_seconds))
    while clock() < deadline:
        if not owned(record, timeout=1.0):
            return StopOutcome(result="stopped", method="control")
        sleep(0.1)

    return _escalate(record, graceful_seconds=graceful_seconds, clock=clock, sleep=sleep)


def _escalate(
    record: RuntimeRecord,
    *,
    graceful_seconds: int,
    clock,
    sleep,
) -> StopOutcome:
    """Signal, but only through an identity the kernel is holding still for us.

    This is where the previous version had a race it could not win. It proved
    ownership in userspace, threw the proof away, and then called ``getpgid``
    and ``killpg`` on a *number*. Between the proof and the signal the runtime
    could exit and the operating system could hand that number to something
    else — so the check said "ours" and the signal went to a stranger.

    A pid file descriptor closes that window. ``pidfd_open`` pins the process
    identity: while the descriptor is open the pid cannot be recycled, so the
    group id — which equals it, because the child leads its own session —
    cannot be recycled either. The sequence is therefore: pin first, re-prove
    against the pinned identity, and only then signal.

    Where no such handle exists — macOS has no ``pidfd`` — escalation **fails
    closed**. The graceful path above needs no signal at all and is what stops
    a runtime in practice; a wedged one is left to the deadline it enforces on
    itself, which is a worse outcome than killing it and a much better one than
    killing something else.
    """
    handle = acquire_handle(record)
    if handle is None:
        if not owned(record, timeout=1.0):
            # It went away while we were reaching for it.
            return StopOutcome(result="stopped", method="control")
        logger.warning(
            "the Learning Studio will not escalate on this platform: it cannot pin the "
            "runtime's identity for long enough to signal it safely"
        )
        return StopOutcome(result="unprovable", method="none")

    try:
        for sig, method in ((signal.SIGTERM, "sigterm"), (signal.SIGKILL, "sigkill")):
            if not handle.signal_group(sig, record):
                return StopOutcome(
                    result="stopped" if not owned(record) else "unprovable", method=method
                )
            escalation_deadline = clock() + max(1, int(graceful_seconds))
            while clock() < escalation_deadline:
                if not owned(record, timeout=1.0):
                    return StopOutcome(result="stopped", method=method)
                sleep(0.1)
        return StopOutcome(result="unprovable", method="sigkill")
    finally:
        handle.close()


class ProcessHandle:
    """A kernel reference that keeps one process identity from being reused.

    Only the pid *file descriptor* form is real. Its whole value is that the
    operating system will not recycle the pid while it is open, which is what
    makes "the thing I proved is the thing I am signalling" true across the gap
    between the two.
    """

    def __init__(self, pid: int, fd: int) -> None:
        self._pid = pid
        self._fd = fd

    def signal_group(self, sig: int, record: RuntimeRecord) -> bool:
        """Deliver to the runtime's process group, re-proving first.

        The group rather than the process alone, because the runtime has a
        tunnel child and stopping only the leader would leave a public address
        being served by an orphan.

        **The leader is signalled through the descriptor, and the group only
        afterwards.** That order is the whole safety argument, and the previous
        version did not have it: it checked ``getpgid`` and then called
        ``killpg`` on a *number*, so a runtime that exited in between handed
        that number — and the signal — to whatever the kernel gave it to next.
        Holding a pidfd made the race narrower without closing it, because the
        pidfd was never the thing used to signal.

        Now:

        1. ``pidfd_send_signal`` delivers to the pinned process. The descriptor
           *is* the identity, so there is no number for anything to recycle,
           and a leader that has already gone raises ``ProcessLookupError``
           rather than reaching a stranger.
        2. Only because step 1 succeeded is the group id then usable: the
           kernel does not reuse a pid while the process it names still exists,
           even as a zombie, so ``self._pid`` still names this runtime's group
           and nobody else's.

        A failure at step 1 returns False without step 2 ever running.
        """
        if not owned(record, timeout=1.0):
            return False
        try:
            if os.getpgid(self._pid) != self._pid:
                logger.warning("refusing to signal runtime: process group is not its own")
                return False
            # (1) The pinned leader, by descriptor. Race-free by construction.
            os.pidfd_send_signal(self._fd, sig)  # type: ignore[attr-defined]
        except (OSError, PermissionError) as exc:
            logger.warning("could not signal the runtime process: %s", type(exc).__name__)
            return False

        try:
            # (2) Its descendants. Reachable only by number — there is no
            # descriptor for a process group — but the number is safe to use
            # here, and only here, because the leader was alive a moment ago.
            os.killpg(self._pid, sig)
        except (OSError, PermissionError) as exc:
            # The leader has the signal either way, so this is not a failure to
            # signal the runtime; it is a failure to reach the rest of a group
            # that may already be empty.
            logger.debug("could not signal the runtime process group: %s", type(exc).__name__)
        return True

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def handle_supported() -> bool:
    """True when this platform can pin a process identity.

    Linux, through ``pidfd_open``. Reported so the runtime status and the
    documentation can say plainly that escalation is unavailable elsewhere,
    rather than leaving an operator to discover it from a stop that reports
    ``unprovable``.
    """
    return (
        hasattr(os, "pidfd_open")
        # The one that actually delivers. Without it a handle could pin an
        # identity and then have no way to use it, which is how signalling a
        # number crept back in the first time.
        and hasattr(os, "pidfd_send_signal")
        and hasattr(os, "killpg")
    )


def acquire_handle(record: RuntimeRecord) -> ProcessHandle | None:
    """Pin the recorded process, or return ``None``.

    ``None`` means one of two things and the caller treats them the same:
    the platform has no way to pin an identity, or the process is already gone.
    Neither is a reason to signal a number.
    """
    if not handle_supported():
        return None
    try:
        fd = os.pidfd_open(record.pid, 0)  # type: ignore[attr-defined]
    except (OSError, ProcessLookupError, PermissionError, AttributeError) as exc:
        logger.debug("could not pin the runtime process: %s", type(exc).__name__)
        return None
    return ProcessHandle(record.pid, fd)


def unprovable_error() -> RuntimeUnavailable:
    return RuntimeUnavailable(UNPROVABLE, reason="runtime_unprovable")
