"""Nothing this plugin starts outlives the call that started it.

Every test here reproduces a window in which a process could be left running
with nobody holding it: an interruption between spawn and record, a
cancellation mid-readiness, a tunnel that ignores SIGTERM, a tunnel that
publishes an address and exits, and a tunnel that dies after the runtime has
told an agent the exercise is open.

No real ``cloudflared`` runs here. The processes are local fakes with the
awkward behaviours spelled out.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from learning_studio.config import LearningStudioConfig
from learning_studio.runtime import bootstrap, ownership, server, state, supervisor, tunnel
from learning_studio.runtime.errors import RuntimeUnavailable

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the runtime owns processes through POSIX primitives only"
)

VALID = "https://calm-forest-1234.trycloudflare.com"


def config(**overrides) -> LearningStudioConfig:
    settings = {
        "runtime_readiness_timeout_seconds": 5,
        "runtime_graceful_stop_seconds": 1,
        "runtime_idle_timeout_seconds": 60,
        "runtime_max_lifetime_seconds": 300,
    }
    settings.update(overrides)
    return LearningStudioConfig(**settings)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ── The supervisor: nothing survives an interruption ──────────────────────


class TrackedChild:
    """A child that knows whether anybody ever stopped it."""

    def __init__(self, pid: int = 31337) -> None:
        self.pid = pid
        self.returncode = None
        self.signalled: list[int] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("child", timeout or 0)
        return self.returncode

    def terminate(self):
        self.signalled.append(15)
        self.returncode = -15

    def kill(self):
        self.signalled.append(9)
        self.returncode = -9

    @property
    def alive(self) -> bool:
        return self.returncode is None


@pytest.fixture
def interpreter(hermes_home) -> Path:
    path = bootstrap.runtime_python()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def group_signals(monkeypatch):
    """Route group signals onto the fake child, so cleanup is observable."""
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: seen.append((pid, sig)))
    return seen


def test_an_interruption_between_spawn_and_record_leaves_no_child(
    hermes_home, interpreter, monkeypatch, group_signals
):
    """The window: ``popen`` returned, and the very next statement was interrupted.

    Reproduced by making that statement raise. Before the fix the child was
    alive with no record of it — nothing in the profile knew it existed, so
    nothing would ever stop it, and its own deadlines were the only thing left.
    """
    child = TrackedChild()
    monkeypatch.setattr(ownership, "owned", lambda *a, **k: False)

    def explode(*_args, **_kwargs):
        raise KeyboardInterrupt("interrupted right after the spawn")

    monkeypatch.setattr(supervisor.dataclasses, "replace", explode)

    with pytest.raises(KeyboardInterrupt):
        supervisor.ensure_running(config(), popen=lambda *a, **k: child, python=interpreter)

    # Cleanup goes to the child's process *group*, which is how its tunnel
    # descendant is reached too. Asserted on the signal rather than on the fake
    # child's own state, because the group is what the real code signals.
    assert group_signals, "a runtime was left running with no record of it"
    assert group_signals[0][0] == child.pid
    assert state.read_record() is None


def test_a_spawn_failure_leaves_no_record_and_no_child(hermes_home, interpreter):
    with pytest.raises(RuntimeUnavailable) as caught:
        supervisor.ensure_running(
            config(),
            popen=lambda *a, **k: (_ for _ in ()).throw(OSError("no such file")),
            python=interpreter,
        )

    assert caught.value.reason == "runtime_spawn_failed"
    assert state.read_record() is None


def test_a_readiness_timeout_stops_the_child_it_is_holding(
    hermes_home, interpreter, monkeypatch, group_signals
):
    child = TrackedChild()
    monkeypatch.setattr(ownership, "owned", lambda *a, **k: False)
    clock = Clock()

    with pytest.raises(RuntimeUnavailable):
        supervisor.ensure_running(
            config(runtime_readiness_timeout_seconds=5),
            popen=lambda *a, **k: child,
            clock=clock,
            sleep=clock.sleep,
            python=interpreter,
        )

    assert group_signals, "a timed-out start left its child running"
    assert group_signals[0][0] == child.pid


def test_a_child_that_exits_while_starting_needs_no_cleanup(
    hermes_home, interpreter, monkeypatch, group_signals
):
    child = TrackedChild()
    child.returncode = 3
    monkeypatch.setattr(ownership, "owned", lambda *a, **k: False)
    clock = Clock()

    with pytest.raises(RuntimeUnavailable) as caught:
        supervisor.ensure_running(
            config(),
            popen=lambda *a, **k: child,
            clock=clock,
            sleep=clock.sleep,
            python=interpreter,
        )

    assert caught.value.reason == "runtime_exited_while_starting"
    assert state.read_record() is None


# ── The tunnel: every exit path stops the process ─────────────────────────


class FakeStream:
    def __init__(self, lines: list[bytes], *, hang: bool = False) -> None:
        self.lines = list(lines)
        self.hang = hang

    async def readline(self) -> bytes:
        if self.hang:
            await asyncio.sleep(3600)
        return self.lines.pop(0) if self.lines else b""


class FakeTunnelProcess:
    """A cloudflared stand-in, with the awkward behaviours spelled out."""

    def __init__(
        self,
        lines: list[bytes],
        *,
        hang: bool = False,
        ignores_sigterm: bool = False,
    ) -> None:
        self.stdout = FakeStream(lines, hang=hang)
        self.returncode = None
        self.ignores_sigterm = ignores_sigterm
        self.signalled: list[str] = []
        self._gone = asyncio.Event()

    def terminate(self) -> None:
        self.signalled.append("terminate")
        if not self.ignores_sigterm:
            self.returncode = -15
            self._gone.set()

    def kill(self) -> None:
        self.signalled.append("kill")
        self.returncode = -9
        self._gone.set()

    async def wait(self) -> int:
        await self._gone.wait()
        return self.returncode


def open_tunnel(process, *, timeout: float = 5.0):
    async def spawn(argv, environment):
        return process

    return asyncio.run(
        tunnel.open_tunnel(
            executable="/usr/bin/cloudflared",
            target="http://127.0.0.1:45678",
            environment={},
            timeout_seconds=timeout,
            spawn=spawn,
        )
    )


def test_a_tunnel_that_publishes_and_exits_is_not_ready():
    """A hostname nobody is serving is not a public entrance."""
    process = FakeTunnelProcess([f"|  {VALID}  |\n".encode()])
    process.returncode = 0  # it printed the address and left

    opened = open_tunnel(process)

    assert opened.state == "failed"
    assert opened.reason == "tunnel_exited_after_publishing"
    assert opened.url == ""


def test_a_stubborn_tunnel_is_killed_rather_than_reported_stopped():
    """SIGTERM ignored. ``stop`` used not to wait, so it lied."""
    process = FakeTunnelProcess([f"{VALID}\n".encode()], ignores_sigterm=True)
    opened = open_tunnel(process)
    assert opened.state == "ready"

    asyncio.run(opened.aclose(grace_seconds=0.05))

    assert process.signalled == ["terminate", "kill"]
    assert process.returncode == -9


def test_closing_waits_for_the_process_to_actually_be_gone():
    process = FakeTunnelProcess([f"{VALID}\n".encode()])
    opened = open_tunnel(process)

    asyncio.run(opened.aclose(grace_seconds=0.5))

    assert process.returncode is not None
    assert opened.alive is False


def test_a_tunnel_that_will_not_die_is_reported_rather_than_claimed_stopped():
    class Immortal(FakeTunnelProcess):
        def terminate(self):
            self.signalled.append("terminate")

        def kill(self):
            self.signalled.append("kill")

    process = Immortal([f"{VALID}\n".encode()])
    opened = open_tunnel(process)

    with pytest.raises(tunnel.TunnelError) as caught:
        asyncio.run(opened.aclose(grace_seconds=0.05))

    assert caught.value.reason == "tunnel_cleanup_indeterminate"


def test_cancelling_the_open_stops_the_process_it_created():
    """The runtime shuts down while the tunnel is still starting."""
    process = FakeTunnelProcess([], hang=True)

    async def scenario():
        async def spawn(argv, environment):
            return process

        task = asyncio.create_task(
            tunnel.open_tunnel(
                executable="/usr/bin/cloudflared",
                target="http://127.0.0.1:45678",
                environment={},
                timeout_seconds=60,
                spawn=spawn,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert process.signalled == ["terminate"], "a cancelled open left cloudflared running"
    assert process.returncode is not None


def test_a_readiness_timeout_stops_the_tunnel():
    process = FakeTunnelProcess([], hang=True)

    opened = open_tunnel(process, timeout=0.05)

    assert opened.state == "failed"
    assert opened.reason == "tunnel_readiness_timeout"
    assert process.returncode is not None


def test_output_that_never_yields_a_url_still_stops_the_tunnel():
    process = FakeTunnelProcess([b"ERR could not reach the edge\n"])

    opened = open_tunnel(process)

    assert opened.state == "failed"
    assert process.returncode is not None


# ── The runtime: a tunnel that dies is not survived ───────────────────────


class Tunnel:
    def __init__(self, url: str = VALID, alive: bool = True) -> None:
        self.url = url
        self.state = "ready"
        self._alive = alive

    @property
    def alive(self) -> bool:
        return self._alive


class Grants:
    def __init__(self) -> None:
        self._grants = {"one": object(), "two": object()}
        self.revoked: list[str] = []

    def revoke(self, launch_id):
        self.revoked.append(launch_id)
        self._grants.pop(launch_id, None)
        return True


class Sessions:
    last_activity_at = 0.0

    def __len__(self) -> int:
        return 0


def runtime_state(tunnel_obj, grants=None):
    settings = server.RuntimeSettings(
        runtime_id="r",
        generation=1,
        control_token="t",
        profile="default",
        handshake_path=Path("/tmp/h.json"),
        idle_timeout_seconds=600,
        max_lifetime_seconds=3600,
    )
    state_ = server.RuntimeState(settings=settings, started_at=0.0, server_state="ready")
    state_.tunnel = tunnel_obj
    state_.grants = grants
    state_.sessions = Sessions()
    return state_


def test_a_tunnel_that_dies_after_readiness_stops_the_runtime():
    """Otherwise the button goes nowhere and the agent was told it was open."""
    grants = Grants()
    state_ = runtime_state(Tunnel(alive=False), grants)

    async def scenario():
        state_.stop_event = asyncio.Event()
        await server._watchdog(state_, clock=lambda: 1.0, tick=0.001)

    asyncio.run(scenario())

    assert state_.stop_reason == "tunnel_lost"
    assert sorted(grants.revoked) == ["one", "two"]
    assert state_.tunnel.state == "failed"


def test_losing_the_tunnel_ends_every_session_that_depended_on_it():
    """A token for a server nobody can reach is not a session worth keeping."""
    grants = Grants()
    state_ = runtime_state(Tunnel(alive=False), grants)

    state_.tunnel_lost()

    assert grants._grants == {}
    assert state_.tunnel.url == ""


def test_a_live_tunnel_is_left_alone():
    state_ = runtime_state(Tunnel(alive=True), Grants())

    async def scenario():
        state_.stop_event = asyncio.Event()
        waiting = asyncio.create_task(server._watchdog(state_, clock=lambda: 1.0, tick=0.001))
        await asyncio.sleep(0.03)
        state_.stop_event.set()
        await waiting

    asyncio.run(scenario())

    assert state_.stop_reason == ""


def test_a_runtime_with_no_tunnel_yet_is_not_treated_as_having_lost_one():
    state_ = runtime_state(None)

    async def scenario():
        state_.stop_event = asyncio.Event()
        waiting = asyncio.create_task(server._watchdog(state_, clock=lambda: 1.0, tick=0.001))
        await asyncio.sleep(0.03)
        state_.stop_event.set()
        await waiting

    asyncio.run(scenario())

    assert state_.stop_reason == ""
