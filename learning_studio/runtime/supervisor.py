"""Starting, reusing, and stopping the runtime, from the Hermes side.

This is the only module that creates a runtime process, and it is written so
that every failure lands in the same place: **nothing running, nothing
recorded, nothing signalled that was not proved ours.**

The start sequence
------------------

1. Take the profile lock, so "is there a runtime?" and "start one" are one
   step. Two concurrent launches cannot both decide there is no runtime.
2. Read the record. If one exists and its process answers the ownership
   challenge, that runtime is reused and the sequence stops here.
3. If a record exists and its process does *not* answer, the record is cleared
   and **nothing is signalled**. The process is either gone or unverifiable,
   and those are indistinguishable from outside. An unverifiable runtime is
   left to its own maximum lifetime, which it enforces itself.
4. Check the plugin's runtime environment exists. Missing is an actionable
   refusal, never an invitation to build one mid-session.
5. Start the child: an argument array, a new session so its process group
   contains it and its descendants and nothing else, a closed environment, and
   a control secret passed through that environment rather than an argument.
6. Wait — bounded — for the handshake file, then for the ownership challenge.
   A child that exits during that wait is detected as an exit, not as a
   timeout, so a crash is reported promptly instead of at the deadline.
7. Write the record, atomically.

Anything that fails between 5 and 7 rolls back: the child is stopped through
the same proved-ownership path a deliberate stop uses, the handshake file is
removed, and no record is written. A caller therefore never sees a record
pointing at a runtime that never became usable.

Why the child outlives us
-------------------------

It has to. A tool call returns in a second; a learner works for twenty minutes.
So the runtime is not a subprocess in the ordinary sense — a Hermes restart
leaves it running, and the ownership challenge is how the next Hermes process
recognises it. What bounds it is not this module's lifetime but the runtime's
own idle timer and absolute lifetime, which is why those are enforced inside
the child rather than here.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import LearningStudioConfig
from . import bootstrap, ownership
from . import environment as env
from .errors import (
    NOT_BOOTSTRAPPED,
    START_FAILED,
    UNSUPPORTED_PLATFORM,
    RuntimeUnavailable,
)
from .state import (
    RuntimeRecord,
    clear_record,
    new_control_token,
    next_generation,
    read_record,
    runtime_dir,
    write_record,
)

logger = logging.getLogger(__name__)

#: The file the supervisor executes. Derived from this package's own location,
#: so nothing outside the package can choose what gets run.
LAUNCHER = Path(__file__).resolve().parent / "launch_server.py"

#: How often the handshake and the challenge are re-checked while starting.
POLL_SECONDS = 0.1


@dataclass(frozen=True)
class RuntimeHandle:
    """A runtime that is up and proved to be ours."""

    record: RuntimeRecord
    reply: ownership.ControlReply
    #: True when this call started it, False when it found one already running.
    started: bool

    @property
    def public_url(self) -> str:
        """The tunnel address, for the one caller that builds a button with it.

        A property rather than a field of :meth:`describe` so that the two
        audiences are visibly different: this is read by the launch
        orchestration and by nothing that renders a tool result.
        """
        return self.reply.tunnel_url

    def describe(self) -> dict[str, object]:
        """Safe lifecycle state. No address, no port, no secret, no URL."""
        return {**self.record.describe(), **self.reply.describe()}


def _unavailable(message: str, reason: str) -> RuntimeUnavailable:
    return RuntimeUnavailable(message, reason=reason)


def resolve_cloudflared(config: LearningStudioConfig) -> str:
    """The operator's tunnel binary, resolved to an absolute path, or ``""``.

    Resolution happens *here*, in the process that has the operator's
    ``PATH``, and the result is handed to the child as an absolute path. The
    child therefore needs no ``PATH`` at all, which removes an entire class of
    question about which executable a process this plugin started actually ran.

    A configured path is used as configured and is never searched for: an
    operator who names a binary has made a decision, and quietly falling back
    to a different one on ``PATH`` when theirs is missing would undo it.
    """
    configured = config.cloudflared_path
    candidate = configured or shutil.which("cloudflared") or ""
    if not candidate:
        return ""
    path = Path(candidate)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        return ""
    return str(path)


def child_environment(
    record: RuntimeRecord,
    *,
    handshake: Path,
    cloudflared: str,
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    """The complete environment the runtime is given — nothing inherited by default.

    Built by naming every variable rather than by copying ``os.environ`` and
    removing things. The difference matters the first time somebody exports a
    credential for an unrelated tool: a copy-and-delete list would pass it
    straight into a process that answers a public URL, and nobody would notice
    until it appeared in a crash report.
    """
    child: dict[str, str] = {
        env.RUNTIME_ID: record.runtime_id,
        env.GENERATION: str(record.generation),
        env.CONTROL_TOKEN: record.control_token,
        env.PROFILE: record.profile,
        env.HANDSHAKE: str(handshake),
        env.IDLE_SECONDS: str(record.idle_timeout_seconds),
        env.MAX_LIFETIME_SECONDS: str(record.max_lifetime_seconds),
    }
    if cloudflared:
        child[env.CLOUDFLARED] = cloudflared
    for name in env.INHERITED:
        value = _inherited(name, source)
        if value:
            child[name] = value
    return child


def _inherited(name: str, source: dict[str, str] | None) -> str:
    """One value to pass on, resolved for the profile actually being served.

    The runtime this starts verifies Telegram signatures and computes an
    allowlist. Both depend on credentials that, in a multiplexed Hermes, are
    *not* the ones in the process environment — so they are resolved through
    the host's secret scope here, in the parent, where the active profile is
    known. The child then reads its own environment, which is correct because
    the environment it has is the one this function built.
    """
    if source is not None:
        return str(source.get(name, "") or "").strip()

    from ..secrets import get_secret

    return get_secret(name)


def handshake_path(runtime_id: str) -> Path:
    """Where this particular start publishes its port.

    Named after the runtime id so two starts — one abandoned, one in progress —
    can never read each other's file. A single fixed name would let a start
    that timed out and a start that succeeded write the same path.
    """
    return runtime_dir() / f"handshake-{runtime_id}.json"


def read_handshake(path: Path, runtime_id: str) -> int | None:
    """The port this runtime bound, or ``None`` if it has not said yet.

    The file is checked, not believed: a payload naming a different runtime is
    ignored. Even a correct one only decides *where to knock* — the ownership
    challenge that follows is what decides whether the thing answering is ours.
    """
    import json

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("runtime_id") != runtime_id:
        return None
    port = parsed.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    return port


def current(config: LearningStudioConfig) -> RuntimeHandle | None:
    """The runtime this profile is running, if it is running one we can prove.

    Reads only. A record whose process cannot be proved ours is reported as no
    runtime — and left exactly as it is, because clearing state is a decision
    for a caller holding the lock.
    """
    del config
    record = read_record()
    if record is None:
        return None
    try:
        reply = ownership.query(record)
    except ownership.ControlError:
        return None
    return RuntimeHandle(record=record, reply=reply, started=False)


def ensure_running(
    config: LearningStudioConfig,
    *,
    popen=subprocess.Popen,
    clock=time.monotonic,
    sleep=time.sleep,
    python: Path | None = None,
) -> RuntimeHandle:
    """Return a proved-owned runtime, starting one if there is not one already.

    The caller must hold the profile lock. That is not enforced with a runtime
    check because there is nothing useful to do when it is violated; it is
    stated here and honoured by the one caller, in
    :mod:`learning_studio.runtime.manager`.
    """
    if not ownership.platform_supported():
        raise _unavailable(UNSUPPORTED_PLATFORM, "platform_unsupported")

    previous = read_record()
    if previous is not None:
        try:
            reply = ownership.query(previous)
        except ownership.ControlError as exc:
            logger.info("clearing an unprovable runtime record: %s", exc.reason)
            # Not signalled. See the module docstring: an unverifiable process
            # is left to the deadline it enforces on itself.
            clear_record()
        else:
            return RuntimeHandle(record=previous, reply=reply, started=False)

    interpreter = Path(python) if python is not None else bootstrap.runtime_python()
    if not interpreter.is_file():
        raise _unavailable(NOT_BOOTSTRAPPED, "runtime_not_bootstrapped")

    return _start(
        config,
        interpreter=interpreter,
        previous=previous,
        popen=popen,
        clock=clock,
        sleep=sleep,
    )


def _start(
    config: LearningStudioConfig,
    *,
    interpreter: Path,
    previous: RuntimeRecord | None,
    popen,
    clock,
    sleep,
) -> RuntimeHandle:
    """Start one runtime, or leave the profile exactly as it was found."""
    from ..paths import profile_id

    runtime_id = uuid.uuid4().hex
    handshake = handshake_path(runtime_id)
    with contextlib.suppress(OSError):
        handshake.unlink()

    record_without_port = RuntimeRecord(
        runtime_id=runtime_id,
        generation=next_generation(previous),
        profile=profile_id(),
        pid=0,
        host=config.runtime_host,
        # Replaced by the port the child reports. Never used to reach anything:
        # `_await_handshake` is what produces the record this function returns.
        port=1,
        control_token=new_control_token(),
        executable=str(interpreter),
        started_at=time.time(),
        idle_timeout_seconds=config.runtime_idle_timeout_seconds,
        max_lifetime_seconds=config.runtime_max_lifetime_seconds,
    )
    child_env = child_environment(
        record_without_port,
        handshake=handshake,
        cloudflared=resolve_cloudflared(config),
    )

    child = None
    try:
        child = popen(
            # An argument array. There is no shell anywhere in this package and
            # no string for one to reinterpret; both entries are paths this
            # package computed, neither is reachable from a tool payload.
            [str(interpreter), str(LAUNCHER)],
            env=child_env,
            # Its own session, so `pid == pgid` and the group this plugin may
            # ever signal contains this child and its descendants alone.
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            # The runtime logs through Python's logging, which goes to stderr.
            # Neither stream is read by this process, so neither may be a pipe:
            # an unread pipe fills and blocks the child forever.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            cwd=str(runtime_dir()),
        )
        record = dataclasses.replace(record_without_port, pid=child.pid)
        return _await_ready(
            config,
            child=child,
            record=record,
            handshake=handshake,
            clock=clock,
            sleep=sleep,
        )
    except (OSError, ValueError) as exc:
        # The spawn itself failed, so there is nothing running to clean up
        # unless `popen` returned before raising — which it does not, but the
        # rollback below is cheap and unconditional for exactly that reason.
        _roll_back(child, record_without_port, handshake, config)
        logger.warning("the runtime process could not be started: %s", type(exc).__name__)
        raise _unavailable(START_FAILED, "runtime_spawn_failed") from exc
    except BaseException:
        # `BaseException`, so a KeyboardInterrupt or a cancellation between the
        # spawn and the record cannot leave a runtime nobody is holding. The
        # window used to be real: `popen` returned, and an interruption during
        # the very next statement leaked a process with no record of it.
        #
        # Cleanup only. The exception is re-raised immediately, because
        # swallowing a cancellation is how a shutdown hangs.
        _roll_back(child, record_without_port, handshake, config)
        raise


def _await_ready(
    config: LearningStudioConfig,
    *,
    child,
    record: RuntimeRecord,
    handshake: Path,
    clock,
    sleep,
) -> RuntimeHandle:
    """Wait for a port and then for proof, or give up in bounded time.

    The child is polled on every pass. A runtime that fell over on its way up
    is a different failure from one that is merely slow, and waiting out the
    full readiness timeout for a process that has already exited wastes the
    learner's patience for no information.
    """
    deadline = clock() + config.runtime_readiness_timeout_seconds
    port: int | None = None

    while clock() < deadline:
        if child.poll() is not None:
            logger.warning("the runtime exited while starting (code %s)", child.returncode)
            raise _unavailable(START_FAILED, "runtime_exited_while_starting")

        if port is None:
            port = read_handshake(handshake, record.runtime_id)
            if port is None:
                sleep(POLL_SECONDS)
                continue
            record = dataclasses.replace(record, port=port)

        try:
            reply = ownership.query(record, timeout=2.0)
        except ownership.ControlError:
            sleep(POLL_SECONDS)
            continue

        write_record(record)
        with contextlib.suppress(OSError):
            handshake.unlink()
        return RuntimeHandle(record=record, reply=reply, started=True)

    logger.warning("the runtime did not become ready within the configured timeout")
    raise _unavailable(START_FAILED, "runtime_readiness_timeout")


def _roll_back(child, record: RuntimeRecord, handshake: Path, config) -> None:
    """Undo a start that did not finish, touching only what this call created.

    Two paths, and the distinction is the whole point:

    - If the runtime got far enough to answer the ownership challenge, it is
      stopped through the ordinary proved-ownership route.
    - If it did not, the only thing this process knows about it is a handle it
      is holding *right now*, from a ``Popen`` it created moments ago. That
      handle is a stronger claim than any recorded process id, so it is used
      directly — and it is the only case in which this package signals a
      process without the control challenge, because here the operating system
      itself is the one saying which process this is.
    """
    with contextlib.suppress(Exception):
        if record.port > 1 and ownership.owned(record, timeout=1.0):
            ownership.stop_owned(record, graceful_seconds=config.runtime_graceful_stop_seconds)
        elif child is not None and child.poll() is None:
            _terminate_held_child(child, config.runtime_graceful_stop_seconds)

    clear_record()
    with contextlib.suppress(OSError):
        handshake.unlink()


def _terminate_held_child(child, graceful_seconds: int) -> None:
    """Stop a child this process is still holding, group and all."""
    pid = child.pid
    with contextlib.suppress(OSError, ProcessLookupError):
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:  # pragma: no cover - the child is always its own group leader
            child.terminate()
    try:
        child.wait(timeout=max(1, int(graceful_seconds)))
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(OSError, ProcessLookupError):
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:  # pragma: no cover
            child.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5)


def stop(config: LearningStudioConfig) -> dict[str, object]:
    """Stop this profile's runtime if it is proved ours. Idempotent.

    The caller must hold the profile lock.
    """
    record = read_record()
    if record is None:
        return {"stopped": False, "state": "not_running"}

    if not ownership.platform_supported():
        clear_record()
        return {"stopped": False, "state": "unprovable"}

    outcome = ownership.stop_owned(record, graceful_seconds=config.runtime_graceful_stop_seconds)
    if outcome.result != "unprovable":
        clear_record()
    return {"stopped": outcome.stopped, "state": outcome.result}
