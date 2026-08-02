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
    #: True once the tunnel has published a URL this plugin accepted. The URL
    #: itself deliberately does not travel in the shape used for status.
    tunnel_ready: bool
    payload: dict[str, Any]

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

    for sig, method in ((signal.SIGTERM, "sigterm"), (signal.SIGKILL, "sigkill")):
        if not _signal_group(record, sig):
            # Ownership could not be re-proved. Either it has just gone (in
            # which case there is nothing to do) or it cannot be verified (in
            # which case there is nothing this module is willing to do).
            return StopOutcome(
                result="stopped" if not owned(record) else "unprovable", method=method
            )
        escalation_deadline = clock() + max(1, int(graceful_seconds))
        while clock() < escalation_deadline:
            if not owned(record, timeout=1.0):
                return StopOutcome(result="stopped", method=method)
            sleep(0.1)

    return StopOutcome(result="unprovable", method="sigkill")


def _signal_group(record: RuntimeRecord, sig: int) -> bool:
    """Signal the runtime's process group, having proved it is ours.

    Two checks, and neither is redundant. The control challenge proves the
    process at that pid is the runtime this plugin started. ``getpgid`` then
    proves that pid is its own group leader — which it is because the child was
    started with ``start_new_session=True`` — so the group about to be signalled
    contains that process and its descendants and nothing else. Without the
    second check, a runtime that had somehow ended up in another process's group
    would take that group down with it.
    """
    if not owned(record, timeout=1.0):
        return False
    try:
        if os.getpgid(record.pid) != record.pid:
            logger.warning("refusing to signal runtime: process group is not its own")
            return False
        os.killpg(record.pid, sig)
    except (OSError, PermissionError) as exc:
        logger.warning("could not signal the runtime process group: %s", type(exc).__name__)
        return False
    return True


def unprovable_error() -> RuntimeUnavailable:
    return RuntimeUnavailable(UNPROVABLE, reason="runtime_unprovable")
