"""The runtime process: the Mini App, a control plane, and its own deadlines.

This module runs in a *different* process and a different virtual environment
from the rest of the plugin. It is the only place FastAPI and Uvicorn are
actually served, and it is started by
:mod:`learning_studio.runtime.supervisor` and by nothing else.

Three things live here that do not live in :mod:`learning_studio.web.app`, and
each is here rather than there on purpose.

**A control plane, on loopback, behind a secret.** Five endpoints under
``/internal`` that the supervisor uses to prove it owns this process, to create
and revoke a learner's grant, to read what a launch actually did, and to ask
for a shutdown. They are not part of the Mini App API and no learner request
can reach them: every one requires a secret handed over in this process's
environment at start, *and* a loopback peer. Putting them in ``app.py`` would
have made them part of the surface every operator-started server exposes;
putting them here means they exist only in a process this plugin supervises.

**Its own deadlines.** The runtime outlives the Hermes process that started it
— that is the point of it, so a learner can keep working after a tool call
returns — which means nothing outside it can be relied upon to stop it. So it
stops itself: an idle timer driven by *authenticated learner* requests, and an
absolute lifetime that expires whether anybody is working or not. A public
entrance to somebody's learning record is not a thing to leave open because a
supervisor crashed.

**The handshake.** The supervisor cannot know an ephemeral port in advance and
will not pre-bind one to find out, so the runtime writes the port it actually
got to a file the supervisor is watching. The file proves nothing and is not
trusted to: it says where to knock, and the control challenge is what decides
whether the thing that answers is this process.

Failure behaviour is uniform: anything that goes wrong before the runtime is
serving exits non-zero having written no handshake, so the supervisor sees a
child that died rather than a runtime that half-exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import json
import logging
import os
import secrets
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from . import environment as env
from .ownership import CONTROL_HEADER

logger = logging.getLogger("learning_studio.runtime")

#: How often the deadlines are checked. A second is far finer than either
#: timeout needs and keeps a shutdown request responsive.
TICK_SECONDS = 0.25

#: Exit codes the supervisor can tell apart. Anything else is an unexpected
#: crash and is reported as one.
EXIT_BAD_ENVIRONMENT = 2
EXIT_SERVE_FAILED = 3
#: The server stopped, but the tunnel could not be confirmed gone. A distinct
#: code because the difference is the whole point: exit 0 says "the public
#: address is closed", and this says "it may still be open". Silently exiting 0
#: here let a `cloudflared` that outlived its runtime be reported as a clean
#: stop, all the way up to the operator.
EXIT_TUNNEL_INDETERMINATE = 4


@dataclass
class RuntimeSettings:
    """Everything this process was told, validated."""

    runtime_id: str
    generation: int
    control_token: str
    profile: str
    handshake_path: Path
    idle_timeout_seconds: int
    max_lifetime_seconds: int
    cloudflared_path: str = ""


class BadEnvironment(Exception):
    """The runtime was started without something it cannot work without."""


def settings_from_environment(source: dict[str, str] | None = None) -> RuntimeSettings:
    """Read and validate the supervisor's instructions.

    Strict, and loud about it. Every value here came from the supervisor one
    step ago; if one of them is missing or malformed, the interesting question
    is not "what is a sensible default" but "why is this process being started
    by something that is not the supervisor".
    """
    values = os.environ if source is None else source

    def required(name: str) -> str:
        value = str(values.get(name, "") or "").strip()
        if not value:
            raise BadEnvironment(f"missing {name}")
        return value

    def whole(name: str, *, low: int, high: int) -> int:
        raw = required(name)
        if not raw.isdigit():
            raise BadEnvironment(f"malformed {name}")
        number = int(raw)
        if not low <= number <= high:
            raise BadEnvironment(f"out of range {name}")
        return number

    handshake = Path(required(env.HANDSHAKE))
    if not handshake.is_absolute():
        raise BadEnvironment(f"malformed {env.HANDSHAKE}")

    return RuntimeSettings(
        runtime_id=required(env.RUNTIME_ID),
        generation=whole(env.GENERATION, low=1, high=2**31),
        control_token=required(env.CONTROL_TOKEN),
        profile=required(env.PROFILE),
        handshake_path=handshake,
        idle_timeout_seconds=whole(env.IDLE_SECONDS, low=1, high=86_400),
        max_lifetime_seconds=whole(env.MAX_LIFETIME_SECONDS, low=1, high=86_400),
        cloudflared_path=str(values.get(env.CLOUDFLARED, "") or "").strip(),
    )


@dataclass
class RuntimeState:
    """What the runtime knows about itself, and reports when asked."""

    settings: RuntimeSettings
    started_at: float
    #: ``starting`` | ``ready`` | ``stopping``
    server_state: str = "starting"
    #: Why it is stopping, for the supervisor's log. Never learner-facing.
    stop_reason: str = ""
    port: int = 0
    sessions: Any = None
    #: Set by the tunnel manager once one exists. Until then the runtime is
    #: reachable on loopback only, which is what the supervisor waits for
    #: before it decides anything.
    tunnel: Any = None
    grants: Any = None
    stop_event: asyncio.Event | None = field(default=None, repr=False)

    def idle_seconds(self, now: float) -> float | None:
        last = getattr(self.sessions, "last_activity_at", None)
        return None if last is None else max(0.0, now - float(last))

    def tunnel_lost(self) -> None:
        """Record that the public entrance has gone, and close what depended on it.

        Every grant is revoked, which also expires the sessions they minted. A
        learner mid-exercise loses their session — which is right: the address
        they are talking to has stopped existing, and a token that outlived it
        would be a credential for a server nobody can reach.
        """
        tunnel = self.tunnel
        if tunnel is not None:
            tunnel.state = "failed"
            tunnel.url = ""
        grants = self.grants
        if grants is not None:
            with contextlib.suppress(Exception):
                for launch_id in list(getattr(grants, "_grants", {})):
                    grants.revoke(launch_id)

    def request_stop(self, reason: str) -> None:
        """Ask for shutdown once; later reasons do not overwrite the first."""
        if self.server_state != "stopping":
            self.server_state = "stopping"
            self.stop_reason = reason
        if self.stop_event is not None:
            self.stop_event.set()

    def status(self, now: float) -> dict[str, Any]:
        """The control reply. Every field here is checked by the supervisor.

        Nothing in it identifies a learner, quotes a learner's work, or carries
        a secret — including the control token that had to be presented to read
        it. A reply that leaked the credential used to obtain it would turn one
        interception into permanent access.
        """
        tunnel = self.tunnel
        return {
            "runtime_id": self.settings.runtime_id,
            "generation": self.settings.generation,
            "pid": os.getpid(),
            "executable": sys.executable,
            "started_at": self.started_at,
            "idle_seconds": self.idle_seconds(now),
            "server_state": self.server_state,
            "tunnel_state": getattr(tunnel, "state", "absent"),
            "tunnel_ready": bool(getattr(tunnel, "url", "")),
            # The public address, over a loopback channel that already required
            # this process's secret. It travels here because the Telegram button
            # is built in the Hermes process and there is nowhere else for it to
            # come from — and it stops here: no tool result, status payload, log
            # line, or memory candidate carries it onward.
            "tunnel_url": getattr(tunnel, "url", "") or "",
            "sessions": len(self.sessions) if self.sessions is not None else 0,
            "idle_timeout_seconds": self.settings.idle_timeout_seconds,
            "max_lifetime_seconds": self.settings.max_lifetime_seconds,
            "expires_in_seconds": max(
                0.0, self.started_at + self.settings.max_lifetime_seconds - now
            ),
        }


# ── The control plane ─────────────────────────────────────────────────────


def install_control_routes(app, state: RuntimeState, *, clock=time.time) -> None:
    """Mount the supervisor's endpoints on an existing application.

    Every one of them passes through :func:`_authorised` first, and that
    function is the entire access rule: a loopback peer presenting the secret
    this process was started with. There is no second way in, no configuration
    that softens it, and no route below that checks it again more loosely.

    ``Request`` is imported at this module's scope rather than here, for the
    same reason :func:`learning_studio.web.app.create_app` does it: with
    postponed annotations, FastAPI resolves a route's ``Request`` annotation
    against *module* globals, and a function-local import leaves it
    unresolvable — which FastAPI reads as an undeclared query parameter and
    answers 422 to. The control plane then rejects the supervisor's challenge
    with a validation error, the supervisor concludes it does not own the
    runtime it just started, and the start times out for a reason that looks
    nothing like the cause.
    """

    def authorised(request: Request) -> bool:
        return _authorised(request, state.settings.control_token)

    def refused() -> JSONResponse:
        # One answer for a wrong token and for a non-loopback peer. Telling a
        # caller which of the two they failed is free reconnaissance.
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/internal/runtime", include_in_schema=False)
    async def runtime_status(request: Request):
        if not authorised(request):
            return refused()
        return JSONResponse(state.status(float(clock())))

    @app.post("/internal/shutdown", include_in_schema=False)
    async def runtime_shutdown(request: Request):
        if not authorised(request):
            return refused()
        state.request_stop("control_request")
        return JSONResponse({"ok": True})

    @app.post("/internal/grant", include_in_schema=False)
    async def create_grant(request: Request):
        if not authorised(request):
            return refused()
        if state.grants is None:
            return JSONResponse({"error": "grants unavailable"}, status_code=409)
        try:
            payload = await _control_body(request)
            granted = state.grants.create(payload)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        return JSONResponse(granted)

    @app.post("/internal/grant/activate", include_in_schema=False)
    async def activate_grant(request: Request):
        if not authorised(request):
            return refused()
        if state.grants is None:
            return JSONResponse({"error": "grants unavailable"}, status_code=409)
        try:
            payload = await _control_body(request)
            activated = state.grants.activate(str(payload.get("launch_id", "")))
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        return JSONResponse({"activated": activated})

    @app.post("/internal/grant/revoke", include_in_schema=False)
    async def revoke_grant(request: Request):
        if not authorised(request):
            return refused()
        if state.grants is None:
            return JSONResponse({"error": "grants unavailable"}, status_code=409)
        try:
            payload = await _control_body(request)
            revoked = state.grants.revoke(str(payload.get("launch_id", "")))
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        return JSONResponse({"revoked": revoked})

    @app.post("/internal/launch", include_in_schema=False)
    async def launch_progress(request: Request):
        if not authorised(request):
            return refused()
        if state.grants is None:
            return JSONResponse({"error": "grants unavailable"}, status_code=409)
        try:
            payload = await _control_body(request)
            progress = state.grants.progress(payload)
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        return JSONResponse(progress)


#: A control body is a small fixed object; nothing here needs more.
MAX_CONTROL_BODY_BYTES = 8192


async def _control_body(request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_CONTROL_BODY_BYTES:
        raise ValueError("control body too large")
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("control body is not an object")
    return parsed


def _authorised(request, control_token: str) -> bool:
    """A loopback peer holding this process's control secret, or nothing.

    Both halves matter. The secret alone would be enough if the runtime were
    only ever reachable on loopback — but it is deliberately also reachable
    through a public tunnel, which is the whole feature, so the peer check is
    what keeps the control plane off the public entrance. The secret is
    compared in constant time because the alternative leaks it one byte at a
    time to a caller who can time the answer.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") or ""
    if not _is_loopback(host):
        return False
    presented = request.headers.get(CONTROL_HEADER, "")
    if not presented or not control_token:
        return False
    return hmac.compare_digest(presented, control_token)


def _is_loopback(host: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ── Serving ───────────────────────────────────────────────────────────────


def write_handshake(path: Path, payload: dict[str, Any]) -> None:
    """Publish the port, atomically and owner-only.

    Atomic because the supervisor is polling for this file and must never read
    a half-written one; owner-only because it sits beside the record and there
    is no reason for it to be readable by anybody else.

    Created, chmod-ed and renamed relative to a descriptor for the directory,
    never by pathname. The directory is opened ``O_NOFOLLOW`` so a link
    substituted for it is refused by the kernel instead of quietly redirecting
    a file the supervisor is about to read a port out of.
    """
    directory = os.open(
        str(path.parent),
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        temporary = f".handshake-{os.getpid()}-{secrets.token_hex(8)}"
        handle = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            with contextlib.suppress(OSError, NotImplementedError):
                os.fchmod(handle, 0o600)
            os.write(handle, json.dumps(payload, sort_keys=True).encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)
        try:
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            raise
    finally:
        os.close(directory)


async def _watchdog(state: RuntimeState, *, clock=time.time, tick: float = TICK_SECONDS) -> None:
    """Enforce the two deadlines until somebody asks the runtime to stop.

    The idle clock starts at the runtime's own start time rather than at the
    first learner request. A runtime that nobody ever opened is exactly as
    unwanted as one they finished with, and waiting for a first request that
    may never come would leave it running until the absolute lifetime expired.
    """
    stop = state.stop_event
    assert stop is not None
    while not stop.is_set():
        now = float(clock())
        if now - state.started_at >= state.settings.max_lifetime_seconds:
            state.request_stop("max_lifetime")
            return
        idle = state.idle_seconds(now)
        since_quiet = now - state.started_at if idle is None else idle
        if since_quiet >= state.settings.idle_timeout_seconds:
            state.request_stop("idle_timeout")
            return
        if _tunnel_lost(state):
            # The public entrance died after it was opened. A runtime that
            # keeps serving now is one whose button goes nowhere, and whose
            # grants would still admit a learner who could never arrive — so
            # the grants go, and so does the runtime.
            state.tunnel_lost()
            state.request_stop("tunnel_lost")
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick)


def _tunnel_lost(state: RuntimeState) -> bool:
    """True when a tunnel that *was* ready is no longer running."""
    tunnel = state.tunnel
    if tunnel is None:
        return False
    return bool(getattr(tunnel, "url", "")) and not getattr(tunnel, "alive", False)


def _bound_port(server) -> int:
    """The port Uvicorn actually got, which may not be the one it was asked for."""
    for bound in getattr(server, "servers", []) or []:
        for socket in getattr(bound, "sockets", []) or []:
            with contextlib.suppress(OSError, IndexError, TypeError):
                return int(socket.getsockname()[1])
    return 0


async def serve(settings: RuntimeSettings, *, clock=time.time) -> int:
    """Run the runtime until a deadline, a signal, or a control request ends it."""
    import uvicorn

    from ..config import load_config
    from ..web.app import create_app
    from ..web.dependencies import build_dependencies
    from .grants import GrantStore

    config = load_config()
    grants = GrantStore(profile=settings.profile, generation=settings.generation, clock=clock)
    dependencies = build_dependencies(config=config, profile=lambda: settings.profile)
    dependencies = dataclasses.replace(dependencies, grants=grants)

    state = RuntimeState(
        settings=settings,
        started_at=float(clock()),
        sessions=dependencies.sessions,
        grants=grants,
    )
    state.stop_event = asyncio.Event()

    app = create_app(dependencies)
    install_control_routes(app, state, clock=clock)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.runtime_host,
            port=config.runtime_port,
            log_level="warning",
            # An access log on a server reachable from the public internet is a
            # file of URLs and timings for one identifiable person's study
            # session. Off, and there is no setting that turns it on.
            access_log=False,
            # The runtime is stopped through the control plane or a signal, both
            # of which this module handles itself.
            lifespan="on",
        )
    )

    loop = asyncio.get_running_loop()
    for received in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(received, state.request_stop, f"signal_{received.name}")

    serving = asyncio.create_task(server.serve())
    try:
        await _await_started(server, serving)
    except RuntimeError:
        return EXIT_SERVE_FAILED

    state.port = _bound_port(server)
    state.server_state = "ready"

    await _after_ready(state, settings, clock=clock)

    watchdog = asyncio.create_task(_watchdog(state, clock=clock))
    closed = False
    try:
        await state.stop_event.wait()
    finally:
        logger.info("runtime stopping: %s", state.stop_reason or "unknown")
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
        closed = await _shutdown(state, server, serving, settings)
    return 0 if closed else EXIT_TUNNEL_INDETERMINATE


async def _after_ready(state: RuntimeState, settings: RuntimeSettings, *, clock) -> None:
    """Everything between "listening" and "the supervisor may look".

    Kept as one step so the handshake is written last: the supervisor treats
    the handshake as "there is something here to talk to", and writing it
    before the tunnel had been attempted would let a launch proceed against a
    runtime that has no public entrance.
    """
    await _start_tunnel(state, settings, clock=clock)
    write_handshake(
        settings.handshake_path,
        {
            "runtime_id": settings.runtime_id,
            "generation": settings.generation,
            "pid": os.getpid(),
            "port": state.port,
        },
    )


async def _start_tunnel(state: RuntimeState, settings: RuntimeSettings, *, clock) -> None:
    """Bring up the public entrance for the server that is already listening.

    The tunnel is pointed at this runtime's *own* bound port, so a tunnel can
    only ever front the generation that started it. A failure here is recorded
    rather than raised: the supervisor reads the tunnel state from the control
    plane and decides whether a launch may proceed, which keeps that decision
    in one place instead of two.
    """
    from ..config import load_config
    from .tunnel import child_environment, loopback_target, open_tunnel

    config = load_config()
    state.tunnel = await open_tunnel(
        executable=settings.cloudflared_path,
        target=loopback_target(config.runtime_host, state.port),
        environment=child_environment(dict(os.environ)),
        timeout_seconds=config.tunnel_readiness_timeout_seconds,
    )
    if state.tunnel.ready:
        logger.info("the tunnel is open")
    else:
        logger.warning("the tunnel did not open: %s", state.tunnel.reason or "unknown")


async def _await_started(server, serving, *, timeout: float = 60.0) -> None:
    """Wait for Uvicorn to report it is listening, or for it to fall over."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if serving.done():
            raise RuntimeError("the server stopped before it started listening")
        if getattr(server, "started", False):
            return
        await asyncio.sleep(0.02)
    raise RuntimeError("the server did not start listening in time")


async def _shutdown(state: RuntimeState, server, serving, settings: RuntimeSettings) -> bool:
    """Stop the tunnel, then the server, then clean up what this process made.

    That order is not arbitrary. Closing the public entrance first means the
    last thing a learner sees is "this exercise has closed" rather than a
    half-served page, and it means no request can arrive during the window in
    which the server is tearing down.

    Returns whether the public address is *known* to be closed. False is not a
    failure to stop — the server still stops, and the handshake still goes —
    it is the difference between "closed" and "may still be open", which the
    caller turns into an exit code rather than discarding.
    """
    closed = True
    tunnel = state.tunnel
    if tunnel is not None:
        try:
            # Waits for the process to be gone, and raises rather than
            # pretending if it will not go. A stop that reported success while
            # cloudflared was still running told the operator the public
            # address was closed when it was not.
            await tunnel.aclose()
        except Exception:
            logger.warning("the tunnel could not be confirmed stopped")
            closed = False

    server.should_exit = True
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
        await asyncio.wait_for(serving, timeout=15)

    with contextlib.suppress(OSError):
        os.unlink(settings.handshake_path)

    return closed


def main(argv: list[str] | None = None) -> int:
    """Entry point. Started by the supervisor, never by anything else."""
    del argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = settings_from_environment()
    except BadEnvironment as exc:
        # The reason names a variable, never a value: one of them is a secret
        # and the rest describe an operator's profile.
        logger.error("the Learning Studio runtime was started incorrectly: %s", exc)
        return EXIT_BAD_ENVIRONMENT

    try:
        return asyncio.run(serve(settings))
    except Exception:
        # No traceback and no message: this process holds a bot token, a
        # control secret, and a learner's answers, and an unexpected failure
        # can be carrying any of them.
        logger.error("the Learning Studio runtime stopped unexpectedly")
        return EXIT_SERVE_FAILED
