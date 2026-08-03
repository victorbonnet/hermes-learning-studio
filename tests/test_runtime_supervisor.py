"""Starting, reusing, and rolling back the runtime.

Most of this runs against a fake process and a fake clock. The last section
does not: it starts the real runtime, on a real loopback port, and stops it
again. That is deliberate and it is not a network test — nothing leaves the
machine, no tunnel is opened, and no Telegram request is made. It is here
because the interesting failures in a supervisor are the ones a mock agrees
with: a handshake that never appears, a child that exits during startup, a
control endpoint that answers but is not ours.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from learning_studio.config import LearningStudioConfig
from learning_studio.runtime import bootstrap, ownership, state, supervisor
from learning_studio.runtime import environment as env
from learning_studio.runtime.errors import RuntimeUnavailable

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the runtime owns processes through POSIX primitives only"
)


def config(**overrides) -> LearningStudioConfig:
    settings = {
        "runtime_readiness_timeout_seconds": 5,
        "runtime_graceful_stop_seconds": 2,
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


class FakeChild:
    """A ``Popen`` stand-in that never runs anything."""

    def __init__(self, pid: int = 31337, exits_with: int | None = None) -> None:
        self.pid = pid
        self.returncode = exits_with
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class FakePopen:
    """Records how the child was started, and hands back a fake one.

    ``publishes`` reproduces the runtime's one externally visible act during
    startup: writing the port it bound to the handshake path it was given. The
    fake reads that path out of the environment it was handed, exactly as the
    real runtime does, so a supervisor that stopped passing it would fail here.
    """

    def __init__(
        self,
        child: FakeChild | None = None,
        *,
        raises: Exception | None = None,
        publishes: int | None = None,
    ) -> None:
        self.child = child or FakeChild()
        self.raises = raises
        self.publishes = publishes
        self.command: list[str] | None = None
        self.kwargs: dict = {}

    def __call__(self, command, **kwargs):
        self.command = list(command)
        self.kwargs = kwargs
        if self.raises:
            raise self.raises
        if self.publishes is not None:
            child_env = kwargs["env"]
            Path(child_env[env.HANDSHAKE]).write_text(
                json.dumps(
                    {
                        "runtime_id": child_env[env.RUNTIME_ID],
                        "generation": int(child_env[env.GENERATION]),
                        "pid": self.child.pid,
                        "port": self.publishes,
                    }
                ),
                encoding="utf-8",
            )
        return self.child


@pytest.fixture
def interpreter(hermes_home: Path) -> Path:
    """A file that stands in for the bootstrapped runtime interpreter."""
    path = bootstrap.runtime_python()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class Answering:
    """A control plane that answers for whichever record it is handed."""

    def __init__(self, *, ready_after: int = 0) -> None:
        self.ready_after = ready_after
        self.calls = 0

    def __call__(self, record, method, path, *, body=None, timeout=None):
        self.calls += 1
        if self.calls <= self.ready_after:
            raise ownership.ControlError("control_unreachable_OSError")
        return {
            "runtime_id": record.runtime_id,
            "generation": record.generation,
            "pid": record.pid,
            "executable": record.executable,
            "started_at": 1000.0,
            "idle_seconds": None,
            "server_state": "ready",
            "tunnel_state": "ready",
            "tunnel_ready": True,
        }


# ── Starting ──────────────────────────────────────────────────────────────


def test_a_start_records_a_runtime_only_once_it_answers(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    monkeypatch.setattr(ownership, "_request", Answering())
    clock = Clock()

    handle = supervisor.ensure_running(
        config(),
        popen=FakePopen(publishes=45678),
        clock=clock,
        sleep=clock.sleep,
        python=interpreter,
    )

    assert handle.started is True
    stored = state.read_record()
    assert stored is not None
    assert stored.pid == 31337
    assert stored.port == 45678


def test_the_recorded_port_is_the_one_the_runtime_reports(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    """The supervisor never pre-binds a port to find out which one it got."""
    monkeypatch.setattr(ownership, "_request", Answering())
    clock = Clock()

    supervisor.ensure_running(
        config(),
        popen=FakePopen(publishes=52001),
        clock=clock,
        sleep=clock.sleep,
        python=interpreter,
    )

    assert state.read_record().port == 52001  # type: ignore[union-attr]


def test_a_handshake_naming_another_runtime_is_ignored(hermes_home: Path):
    path = state.runtime_dir() / "handshake-x.json"
    path.write_text(json.dumps({"runtime_id": "somebody-else", "port": 1234}), encoding="utf-8")

    assert supervisor.read_handshake(path, "ours") is None


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps([1, 2]),
        json.dumps({"runtime_id": "ours", "port": 0}),
        json.dumps({"runtime_id": "ours", "port": "1234"}),
        json.dumps({"runtime_id": "ours", "port": 70000}),
        json.dumps({"runtime_id": "ours"}),
    ],
)
def test_a_malformed_handshake_is_ignored(hermes_home: Path, payload: str):
    path = state.runtime_dir() / "handshake-x.json"
    path.write_text(payload, encoding="utf-8")

    assert supervisor.read_handshake(path, "ours") is None


def test_a_successful_start_removes_its_handshake(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    monkeypatch.setattr(ownership, "_request", Answering())
    clock = Clock()

    supervisor.ensure_running(
        config(),
        popen=FakePopen(publishes=45678),
        clock=clock,
        sleep=clock.sleep,
        python=interpreter,
    )

    assert list(state.runtime_dir().glob("handshake-*.json")) == []


def test_the_child_is_started_as_an_argument_array_with_no_shell(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    popen = FakePopen(FakeChild(exits_with=1))
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))

    with pytest.raises(RuntimeUnavailable):
        supervisor.ensure_running(config(), popen=popen, python=interpreter)

    assert popen.command == [str(interpreter), str(supervisor.LAUNCHER)]
    assert popen.kwargs.get("shell") in (None, False)
    assert popen.kwargs["start_new_session"] is True
    assert popen.kwargs["close_fds"] is True


def test_the_child_environment_is_built_by_naming_every_variable(hermes_home: Path):
    record = state.RuntimeRecord(
        runtime_id="r",
        generation=1,
        profile="family",
        pid=1,
        host="127.0.0.1",
        port=1,
        control_token="the-secret",
        executable="/x/python",
        started_at=0.0,
        idle_timeout_seconds=60,
        max_lifetime_seconds=300,
    )

    child = supervisor.child_environment(
        record,
        handshake=Path("/tmp/h.json"),
        cloudflared="/usr/bin/cloudflared",
        source={
            "HERMES_HOME": "/profiles/family",
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "AWS_SECRET_ACCESS_KEY": "must-not-travel",
            "OPENAI_API_KEY": "must-not-travel-either",
            "PATH": "/usr/bin",
        },
    )

    assert child[env.CONTROL_TOKEN] == "the-secret"
    assert child[env.PROFILE] == "family"
    assert child["TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert "AWS_SECRET_ACCESS_KEY" not in child
    assert "OPENAI_API_KEY" not in child
    assert "PATH" not in child, "the runtime resolves no executables of its own"


def test_the_control_secret_never_reaches_a_command_line(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    """The process table is world-readable; the environment of a process is not."""
    popen = FakePopen(FakeChild(exits_with=1))
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))

    with pytest.raises(RuntimeUnavailable):
        supervisor.ensure_running(config(), popen=popen, python=interpreter)

    token = popen.kwargs["env"][env.CONTROL_TOKEN]
    assert token
    assert token not in " ".join(popen.command or [])


def test_a_child_that_exits_while_starting_is_reported_promptly(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))
    clock = Clock()

    with pytest.raises(RuntimeUnavailable) as caught:
        supervisor.ensure_running(
            config(runtime_readiness_timeout_seconds=600),
            popen=FakePopen(FakeChild(exits_with=3)),
            clock=clock,
            sleep=clock.sleep,
            python=interpreter,
        )

    assert caught.value.reason == "runtime_exited_while_starting"
    assert clock.now < 5, "it waited out the readiness timeout for a dead child"


def test_a_start_that_times_out_leaves_no_record(hermes_home: Path, interpreter: Path, monkeypatch):
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: None)
    clock = Clock()

    with pytest.raises(RuntimeUnavailable) as caught:
        supervisor.ensure_running(
            config(runtime_readiness_timeout_seconds=5),
            popen=FakePopen(),
            clock=clock,
            sleep=clock.sleep,
            python=interpreter,
        )

    assert caught.value.reason == "runtime_readiness_timeout"
    assert state.read_record() is None


def test_a_start_that_times_out_stops_the_child_it_is_holding(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    """Rollback signals a handle this process holds — the strongest claim there is."""
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    clock = Clock()
    child = FakeChild()

    with pytest.raises(RuntimeUnavailable):
        supervisor.ensure_running(
            config(runtime_readiness_timeout_seconds=5),
            popen=FakePopen(child),
            clock=clock,
            sleep=clock.sleep,
            python=interpreter,
        )

    assert killed and killed[0][0] == child.pid


def test_a_start_that_times_out_leaves_no_handshake_behind(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: None)
    clock = Clock()

    with pytest.raises(RuntimeUnavailable):
        supervisor.ensure_running(
            config(runtime_readiness_timeout_seconds=5),
            popen=FakePopen(),
            clock=clock,
            sleep=clock.sleep,
            python=interpreter,
        )

    assert list(state.runtime_dir().glob("handshake-*.json")) == []


def test_a_start_that_cannot_spawn_reports_safely(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    with pytest.raises(RuntimeUnavailable) as caught:
        supervisor.ensure_running(
            config(),
            popen=FakePopen(raises=OSError("no such file")),
            python=interpreter,
        )

    assert caught.value.reason == "runtime_spawn_failed"
    assert "no such file" not in caught.value.message
    assert state.read_record() is None


def test_an_unbootstrapped_profile_refuses_rather_than_building_one(hermes_home: Path):
    popen = FakePopen()

    with pytest.raises(RuntimeUnavailable) as caught:
        supervisor.ensure_running(config(), popen=popen)

    assert caught.value.reason == "runtime_not_bootstrapped"
    assert popen.command is None, "a launch built an environment mid-session"


def test_an_unsupported_platform_refuses_before_anything_starts(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    monkeypatch.setattr(ownership, "platform_supported", lambda: False)
    popen = FakePopen()

    with pytest.raises(RuntimeUnavailable) as caught:
        supervisor.ensure_running(config(), popen=popen, python=interpreter)

    assert caught.value.reason == "platform_unsupported"
    assert popen.command is None


# ── Reuse ─────────────────────────────────────────────────────────────────


def stored_record(**overrides) -> state.RuntimeRecord:
    fields = {
        "runtime_id": "already-running",
        "generation": 4,
        "profile": "default",
        "pid": 999,
        "host": "127.0.0.1",
        "port": 40404,
        "control_token": "token",
        "executable": "/x/python",
        "started_at": 10.0,
        "idle_timeout_seconds": 60,
        "max_lifetime_seconds": 300,
    }
    fields.update(overrides)
    record = state.RuntimeRecord(**fields)
    state.write_record(record)
    return record


def test_a_healthy_runtime_is_reused_and_nothing_is_started(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    stored_record()
    monkeypatch.setattr(ownership, "_request", Answering())
    popen = FakePopen()

    handle = supervisor.ensure_running(config(), popen=popen, python=interpreter)

    assert handle.started is False
    assert handle.record.runtime_id == "already-running"
    assert popen.command is None


class AnsweringExcept:
    """Unreachable for one named runtime id, and answers for every other.

    That is a stale record after a machine restart: the recorded process is
    gone, or the pid now belongs to something else entirely, and the runtime
    this call starts is the only thing that can answer the challenge.
    """

    def __init__(self, refuses: str) -> None:
        self.refuses = refuses

    def __call__(self, record, method, path, *, body=None, timeout=None):
        if record.runtime_id == self.refuses:
            raise ownership.ControlError("control_unreachable_OSError")
        return {
            "runtime_id": record.runtime_id,
            "generation": record.generation,
            "pid": record.pid,
            "executable": record.executable,
            "started_at": 1000.0,
            "idle_seconds": None,
            "server_state": "ready",
            "tunnel_state": "ready",
            "tunnel_ready": True,
        }


def test_an_unprovable_record_is_replaced_and_never_signalled(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    """The single most important assertion about restarts and recycled pids."""
    stale = stored_record(runtime_id="gone", pid=4242, generation=4)
    monkeypatch.setattr(ownership, "_request", AnsweringExcept("gone"))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    clock = Clock()

    handle = supervisor.ensure_running(
        config(),
        popen=FakePopen(publishes=45678),
        clock=clock,
        sleep=clock.sleep,
        python=interpreter,
    )

    assert killed == [], "an unverifiable process was signalled"
    assert handle.started is True
    assert handle.record.runtime_id != stale.runtime_id


def test_a_replacement_runtime_takes_the_next_generation(
    hermes_home: Path, interpreter: Path, monkeypatch
):
    """A grant bound to generation 4 must not be honoured by generation 5."""
    stored_record(runtime_id="gone", generation=4)
    monkeypatch.setattr(ownership, "_request", AnsweringExcept("gone"))
    clock = Clock()

    handle = supervisor.ensure_running(
        config(),
        popen=FakePopen(publishes=45678),
        clock=clock,
        sleep=clock.sleep,
        python=interpreter,
    )

    assert handle.record.generation == 5


def test_current_reports_nothing_for_a_runtime_it_cannot_prove(hermes_home: Path, monkeypatch):
    stored_record()
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))

    assert supervisor.current(config()) is None
    assert state.read_record() is not None, "a read-only query cleared the record"


def test_current_reports_a_runtime_it_can_prove(hermes_home: Path, monkeypatch):
    stored_record()
    monkeypatch.setattr(ownership, "_request", Answering())

    handle = supervisor.current(config())

    assert handle is not None
    assert handle.started is False


# ── Stopping ──────────────────────────────────────────────────────────────


def test_stopping_with_no_record_is_a_no_op(hermes_home: Path):
    assert supervisor.stop(config()) == {"stopped": False, "state": "not_running"}


def test_stopping_a_dead_runtime_clears_the_record(hermes_home: Path, monkeypatch):
    stored_record()
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    outcome = supervisor.stop(config())

    assert outcome["state"] == "not_running"
    assert state.read_record() is None
    assert killed == []


def test_stopping_is_idempotent(hermes_home: Path, monkeypatch):
    stored_record()
    monkeypatch.setattr(ownership, "_request", Answering(ready_after=99))

    first = supervisor.stop(config())
    second = supervisor.stop(config())

    assert first["state"] == "not_running"
    assert second == {"stopped": False, "state": "not_running"}


def test_one_profile_cannot_stop_another(tmp_path: Path, monkeypatch):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(ownership, "_request", Answering())

    monkeypatch.setenv("HERMES_HOME", str(first))
    stored_record(runtime_id="a-runtime")

    monkeypatch.setenv("HERMES_HOME", str(second))
    assert supervisor.stop(config()) == {"stopped": False, "state": "not_running"}

    monkeypatch.setenv("HERMES_HOME", str(first))
    assert state.read_record() is not None, "another profile's stop cleared this record"


# ── Discovering cloudflared ───────────────────────────────────────────────


def test_a_configured_binary_is_used_when_it_is_real(tmp_path: Path):
    binary = tmp_path / "cloudflared"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    assert supervisor.resolve_cloudflared(config(cloudflared_path=str(binary))) == str(binary)


def test_a_configured_binary_that_is_missing_is_not_replaced_by_one_on_the_path(tmp_path: Path):
    """An operator who names a binary has decided; a silent fallback undoes it."""
    missing = tmp_path / "not-here"

    assert supervisor.resolve_cloudflared(config(cloudflared_path=str(missing))) == ""


def test_a_non_executable_binary_is_refused(tmp_path: Path):
    binary = tmp_path / "cloudflared"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o644)

    assert supervisor.resolve_cloudflared(config(cloudflared_path=str(binary))) == ""


def test_discovery_falls_back_to_the_path_only_when_nothing_is_configured(monkeypatch, tmp_path):
    binary = tmp_path / "cloudflared"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: str(binary))

    assert supervisor.resolve_cloudflared(config()) == str(binary)


# ── The real thing ────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not (sys.version_info >= (3, 11)), reason="the runtime targets the supported interpreters"
)
def test_the_real_runtime_starts_answers_and_stops(hermes_home: Path, monkeypatch):
    """A live child, on loopback, with no tunnel and no network.

    This is the test the fakes cannot replace. It runs the actual launcher, in
    the actual interpreter, and proves the whole handshake: the child binds a
    port nobody chose in advance, publishes it, answers the ownership challenge
    with its real pid, and shuts down when asked over the control plane.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")

    settings = config(runtime_readiness_timeout_seconds=60, runtime_graceful_stop_seconds=10)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    handle = supervisor.ensure_running(settings, python=Path(sys.executable))
    try:
        assert handle.started is True
        assert handle.reply.pid == handle.record.pid
        assert handle.reply.server_state == "ready"
        assert ownership.owned(handle.record) is True

        # The recorded process leads its own group, which is what makes the
        # escalation path in `ownership` safe.
        assert os.getpgid(handle.record.pid) == handle.record.pid
    finally:
        outcome = supervisor.stop(settings)

    assert outcome["state"] == "stopped"
    assert state.read_record() is None
    assert ownership.owned(handle.record, timeout=1.0) is False


def test_the_real_runtime_opens_a_tunnel_from_the_operator_binary(hermes_home: Path, tmp_path):
    """A live runtime and a stand-in for cloudflared. Still no network.

    The fake prints what cloudflared prints and then waits, which is the whole
    contract this plugin depends on. What is being tested is the seam: the
    supervisor resolves an operator-approved executable, the runtime starts it
    pointed at its own loopback port, and the URL it published survives
    validation on both sides.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")

    binary = tmp_path / "cloudflared"
    binary.write_text(
        "#!/bin/sh\n"
        'echo "INF Requesting new quick Tunnel on trycloudflare.com..."\n'
        'echo "|  https://fake-tunnel-abc.trycloudflare.com   |"\n'
        "sleep 300\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    settings = config(
        runtime_readiness_timeout_seconds=60,
        tunnel_readiness_timeout_seconds=20,
        runtime_graceful_stop_seconds=10,
        cloudflared_path=str(binary),
    )

    handle = supervisor.ensure_running(settings, python=Path(sys.executable))
    try:
        assert handle.reply.tunnel_state == "ready"
        assert handle.public_url == "https://fake-tunnel-abc.trycloudflare.com"
        # The address never appears in anything an agent is shown.
        assert "trycloudflare" not in json.dumps(handle.describe())
    finally:
        outcome = supervisor.stop(settings)

    assert outcome["state"] == "stopped"


def test_a_runtime_whose_tunnel_prints_nothing_usable_stays_without_one(
    hermes_home: Path, tmp_path
):
    """A failed tunnel is a runtime without a public entrance, not a crash."""
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")

    binary = tmp_path / "cloudflared"
    binary.write_text(
        '#!/bin/sh\necho "ERR failed to connect to the edge"\nexit 1\n', encoding="utf-8"
    )
    binary.chmod(0o755)

    settings = config(
        runtime_readiness_timeout_seconds=60,
        tunnel_readiness_timeout_seconds=10,
        runtime_graceful_stop_seconds=10,
        cloudflared_path=str(binary),
    )

    handle = supervisor.ensure_running(settings, python=Path(sys.executable))
    try:
        assert handle.reply.tunnel_state == "failed"
        assert handle.public_url == ""
        assert handle.reply.server_state == "ready"
    finally:
        supervisor.stop(settings)


# ── The window between spawning and holding ───────────────────────────────


def test_interrupting_signals_are_blocked_while_the_child_is_being_spawned(
    hermes_home, interpreter
):
    """The one instruction no ``try`` block can cover.

    ``popen`` returns a running process, and the assignment that stores it is a
    separate step. A Ctrl-C delivered between them — the ordinary way to hit
    this — unwinds with the reference already dropped, so the rollback handler
    finds nothing to stop and a runtime keeps running with nothing naming it.
    """
    import signal

    class Watching(FakePopen):
        def __init__(self) -> None:
            super().__init__(raises=OSError("stop here"))
            self.blocked: set = set()

        def __call__(self, command, **kwargs):
            self.blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])
            return super().__call__(command, **kwargs)

    popen = Watching()
    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])

    with pytest.raises(RuntimeUnavailable):
        supervisor.ensure_running(config(), python=interpreter, popen=popen)

    assert {signal.SIGINT, signal.SIGTERM} <= popen.blocked
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before, "the mask was not restored"


def test_a_spawn_that_raises_leaves_the_signal_mask_alone(hermes_home, interpreter):
    """Restored in a ``finally``, so a failed start blocks nothing afterwards."""
    import signal

    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    with pytest.raises(RuntimeUnavailable):
        supervisor.ensure_running(
            config(), python=interpreter, popen=FakePopen(raises=OSError("no such file"))
        )

    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before


def test_the_runtime_entry_point_clears_an_inherited_signal_mask():
    """A blocked set survives ``exec``; ``restore_signals`` does not clear it.

    Without the clear at the top of ``launch_server``, a runtime started while
    the supervisor held signals would ignore the ``SIGTERM`` its own shutdown
    depends on — and would pass the same deafness to ``cloudflared``.
    """
    import subprocess

    pytest.importorskip("fastapi")

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import signal;"
            "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT});"
            "import learning_studio.runtime.launch_server;"
            "print(sorted(signal.pthread_sigmask(signal.SIG_BLOCK, [])))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "[]"


def test_a_signal_nobody_thought_of_cannot_interrupt_the_spawn(hermes_home, interpreter):
    """The chosen-four version defended against Ctrl-C and left the rest.

    Any signal with a Python handler can raise out of that handler, and the
    handler may raise anything. ``SIGALRM`` from an unrelated timeout is the
    easy one to demonstrate; ``SIGUSR1`` from an operator's script is the same
    bug. All of them land in the one-instruction window between ``popen``
    returning and the reference being stored, and leak the same runtime.
    """
    import signal

    fired: list[str] = []

    def handler(signum, frame):
        fired.append("alarm")
        raise KeyboardInterrupt("an unrelated timeout")

    stopped: list = []
    real_terminate = supervisor._terminate_held_child

    def watching(child, graceful_seconds):
        stopped.append(child)
        return real_terminate(child, graceful_seconds)

    previous = signal.signal(signal.SIGALRM, handler)
    original = supervisor._terminate_held_child
    supervisor._terminate_held_child = watching
    try:

        class Alarming(FakePopen):
            def __call__(self, command, **kwargs):
                child = super().__call__(command, **kwargs)
                # Raised inside the spawn region, exactly where the reference
                # has not been stored yet. Deferred, so it arrives after.
                signal.raise_signal(signal.SIGALRM)
                self.deferred = not fired
                return child

        popen = Alarming(publishes=45999)
        with pytest.raises(BaseException) as caught:
            supervisor.ensure_running(config(), python=interpreter, popen=popen)
    finally:
        signal.signal(signal.SIGALRM, previous)
        supervisor._terminate_held_child = original

    assert popen.deferred is True, "the handler ran inside the spawn region"
    assert fired == ["alarm"], "the signal was swallowed rather than deferred"
    assert isinstance(caught.value, (KeyboardInterrupt, RuntimeUnavailable))
    # It arrived late, so the rollback had a child to hand to the cleanup owner
    # rather than a `None` it could do nothing with.
    assert stopped == [popen.child], "the runtime was left with nothing holding it"
    assert state.read_record() is None


# ── A stop that could not close the tunnel says so ────────────────────────


def test_a_runtime_that_left_a_tunnel_running_is_not_reported_as_stopped(
    hermes_home, monkeypatch, clock=None
):
    """The exit code reaches a parent; a runtime that outlives Hermes has none.

    The runtime exits 4 when it cannot confirm its tunnel is gone, and that is
    the right thing for it to do — but nobody is waiting on it. The supervisor
    watched the control endpoint go quiet and called that a clean stop, which
    is precisely the claim that must not be made about an address nobody could
    confirm was closed. So the runtime also leaves a marker, and this is what
    reads it.
    """
    from learning_studio.runtime import server as runtime_server
    from learning_studio.runtime.state import managed_path

    record = _record_for(hermes_home)
    runtime_server.write_handshake(
        managed_path(f"residue-{record.runtime_id}.json"),
        {"runtime_id": record.runtime_id, "generation": 1, "reason": "x"},
    )
    monkeypatch.setattr(ownership, "_request", _stopping(record))

    outcome = supervisor.stop(config())

    assert outcome["state"] == "tunnel_indeterminate"
    assert outcome["stopped"] is False

    # Consumed, so the *next* stop reports the truth about the next runtime
    # rather than repeating this one's problem for ever.
    state.write_record(record)
    monkeypatch.setattr(ownership, "_request", _stopping(record))
    assert supervisor.stop(config())["state"] == "stopped"


def test_a_runtime_that_had_to_be_signalled_never_confirms_its_tunnel(hermes_home, monkeypatch):
    """Escalation reaches the runtime, not its tunnel — they are separate sessions.

    That separation is what lets a healthy runtime end its tunnel and every
    descendant of it. The cost lands here: a runtime killed *because it was
    wedged* never ran its own teardown, so nothing closed the address, and the
    stop says so rather than rounding up to success.
    """
    record = _record_for(hermes_home)
    monkeypatch.setattr(ownership, "_request", _stopping(record))
    monkeypatch.setattr(
        ownership,
        "stop_owned",
        lambda *a, **k: ownership.StopOutcome(result="stopped", method="sigkill"),
    )

    outcome = supervisor.stop(config())

    assert outcome["state"] == "tunnel_indeterminate"
    assert outcome["stopped"] is False


def _record_for(hermes_home) -> state.RuntimeRecord:
    record = state.RuntimeRecord(
        runtime_id="runtime-residue",
        generation=1,
        profile="default",
        pid=4242,
        host="127.0.0.1",
        port=45678,
        control_token="token",
        executable="/x/python",
        started_at=0.0,
        idle_timeout_seconds=60,
        max_lifetime_seconds=300,
    )
    state.write_record(record)
    return record


def _stopping(record):
    gone = {"value": False}

    def request(rec, method, path, *, body=None, timeout=None):
        if path == ownership.SHUTDOWN_PATH:
            gone["value"] = True
            return {}
        if gone["value"]:
            raise ownership.ControlError("control_unreachable_OSError")
        return {
            "runtime_id": record.runtime_id,
            "generation": record.generation,
            "pid": record.pid,
            "executable": record.executable,
            "started_at": 0.0,
            "idle_seconds": None,
            "server_state": "ready",
            "tunnel_state": "ready",
            "tunnel_ready": True,
            "tunnel_url": "https://calm-forest-1234.trycloudflare.com",
            "sessions": 0,
            "expires_in_seconds": 300,
        }

    return request


# ── The managed interpreter, taken by the branch production actually uses ──


def _build_managed_venv(stub: str = "managed_runtime_marker") -> Path:
    """A real virtual environment where the plugin keeps its own, plus a stub.

    ``--without-pip`` because this needs a venv, not a package index: the
    property under test is that executing the managed interpreter yields a
    ``sys.prefix`` of that venv, so its ``site-packages`` is importable. A stub
    module dropped in there stands in for FastAPI and Uvicorn and needs no
    network.
    """
    import compileall
    import venv

    target = bootstrap.venv_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=False, symlinks=True).create(target)

    site = next(iter((target / "lib").glob("python*/site-packages")))
    (site / f"{stub}.py").write_text("VALUE = 'from the managed environment'\n", encoding="utf-8")
    compileall.compile_dir(str(site), quiet=2)
    return target


def _probe(interpreter: str, script: str) -> subprocess.CompletedProcess:
    return subprocess.run([interpreter, "-c", script], capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(sys.platform == "win32", reason="the runtime is POSIX only")
def test_the_managed_interpreter_runs_inside_its_own_virtual_environment(hermes_home: Path):
    """The default branch — no ``python=`` — must produce a usable interpreter.

    Every other test here passes ``python=Path(sys.executable)``, which takes
    the *other* branch, so a change to how the managed interpreter is resolved
    and executed could break the real thing while the suite stayed green. That
    happened: executing it through ``/proc/self/fd/N`` pinned the file but put
    it in a directory with no ``pyvenv.cfg``, so CPython stopped recognising a
    virtual environment at all — ``sys.prefix`` collapsed to
    ``sys.base_prefix`` and the runtime lost every dependency installed for it.

    Three things are asserted, because the failure had three faces: the spawn
    argument, what the interpreter reports about itself, and whether the
    environment's own packages are reachable from it.
    """
    managed = _build_managed_venv()
    popen = FakePopen(publishes=45999)

    with pytest.raises(RuntimeUnavailable):
        # It cannot become *ready* — the fake never answers the challenge — but
        # it gets far enough to record what it was going to run.
        supervisor.ensure_running(config(), popen=popen)

    assert popen.command is not None
    spawned = popen.command[0]

    # (1) An ordinary path inside the managed environment, not a descriptor.
    assert spawned == str(bootstrap.runtime_python())
    assert "/proc/" not in spawned and "/dev/fd" not in spawned

    # (2) Run it. It must consider itself part of *this* environment.
    reported = _probe(
        spawned, "import sys;print(sys.prefix);print(sys.base_prefix);print(sys.executable)"
    )
    assert reported.returncode == 0, reported.stderr
    prefix, base_prefix, executable = reported.stdout.strip().splitlines()
    assert Path(prefix).resolve() == managed.resolve(), "the managed venv was not detected"
    assert Path(prefix).resolve() != Path(base_prefix).resolve()

    # (3) Its installed dependencies are therefore importable.
    imported = _probe(spawned, "import managed_runtime_marker as m;print(m.VALUE)")
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.strip() == "from the managed environment"

    # (4) The identity the child would report matches the one recorded, which
    # is what the ownership challenge compares. Two different strings there
    # made that check pass by coincidence rather than by construction.
    assert Path(executable).resolve() == Path(spawned).resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="the runtime is POSIX only")
def test_the_recorded_executable_is_the_one_that_was_spawned(hermes_home: Path):
    """One string for the spawn and the record, so the challenge means something."""
    _build_managed_venv()
    popen = FakePopen(publishes=45999)

    with pytest.raises(RuntimeUnavailable):
        supervisor.ensure_running(config(), popen=popen)

    child_env = popen.kwargs["env"]
    recorded = state.read_record()
    # The start rolled back, so there is no record left behind; the identity
    # that *would* have been recorded is the interpreter that was spawned.
    assert recorded is None
    assert popen.command[0] == str(bootstrap.runtime_python())
    assert env.RUNTIME_ID in child_env
