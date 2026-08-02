"""Runtime records, the profile lock, and proof of process ownership.

The property under test throughout is the same one: *nothing is signalled that
has not proved it belongs to this plugin*. Every corrupt record, every recycled
process id, and every mismatched identity has to end in "do nothing", not in a
best guess.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from learning_studio.runtime import ownership, state
from learning_studio.runtime.errors import RuntimeUnavailable

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the runtime owns processes through POSIX primitives only"
)


def record(**overrides) -> state.RuntimeRecord:
    fields = {
        "runtime_id": "runtime-abc",
        "generation": 1,
        "profile": "default",
        "pid": 4242,
        "host": "127.0.0.1",
        "port": 51515,
        "control_token": "secret-token",
        "executable": "/opt/runtime/bin/python",
        "started_at": 1000.0,
        "idle_timeout_seconds": 1800,
        "max_lifetime_seconds": 7200,
    }
    fields.update(overrides)
    return state.RuntimeRecord(**fields)


# ── The record ────────────────────────────────────────────────────────────


def test_a_written_record_reads_back_identical(hermes_home: Path):
    original = record()

    state.write_record(original)

    assert state.read_record() == original


def test_the_record_file_is_owner_only(hermes_home: Path):
    state.write_record(record())

    mode = stat.S_IMODE(state.record_path().stat().st_mode)

    assert mode == 0o600, oct(mode)


def test_the_record_directory_is_owner_only(hermes_home: Path):
    state.runtime_dir()

    mode = stat.S_IMODE(state.runtime_dir().stat().st_mode)

    assert mode == 0o700, oct(mode)


def test_a_replaced_record_never_mixes_two_runtimes(hermes_home: Path):
    """Atomic replace, so a reader sees one whole record or the previous one."""
    state.write_record(record(runtime_id="first", port=1111))
    state.write_record(record(runtime_id="second", generation=2, port=2222))

    current = state.read_record()

    assert current is not None
    assert (current.runtime_id, current.port) == ("second", 2222)


def test_writing_leaves_no_temporary_file_behind(hermes_home: Path):
    state.write_record(record())

    strays = [path.name for path in state.runtime_dir().iterdir() if path.name.startswith(".")]

    assert strays == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": 99},
        {"pid": "4242"},
        {"pid": -1},
        {"pid": 0},
        {"port": 0},
        {"port": 70000},
        {"generation": 0},
        {"runtime_id": ""},
        {"control_token": 12345},
        {"started_at": "recently"},
        {"idle_timeout_seconds": 0},
    ],
)
def test_a_corrupt_record_reads_as_no_runtime(hermes_home: Path, mutation: dict):
    """Every unusable field ends in the same place: this profile has no runtime.

    Which is the only safe answer, because the field a lenient reader would
    have kept is a process id.
    """
    payload = record().to_json()
    payload.update(mutation)
    state.runtime_dir()
    state.record_path().write_text(json.dumps(payload), encoding="utf-8")

    assert state.read_record() is None


def test_a_record_that_is_not_json_reads_as_no_runtime(hermes_home: Path):
    state.runtime_dir()
    state.record_path().write_text("{not json", encoding="utf-8")

    assert state.read_record() is None


def test_an_oversized_record_is_refused_without_being_parsed(hermes_home: Path):
    state.runtime_dir()
    state.record_path().write_text("x" * (state.MAX_RECORD_BYTES + 1), encoding="utf-8")

    assert state.read_record() is None


def test_a_missing_record_reads_as_no_runtime(hermes_home: Path):
    assert state.read_record() is None


def test_clearing_the_record_is_idempotent(hermes_home: Path):
    state.write_record(record())

    state.clear_record()
    state.clear_record()

    assert state.read_record() is None


def test_the_record_describes_itself_without_the_control_token(hermes_home: Path):
    described = json.dumps(record().describe())

    assert "secret-token" not in described
    assert "/opt/runtime/bin/python" not in described
    assert "51515" not in described
    assert "127.0.0.1" not in described


def test_generations_advance_and_start_at_one():
    assert state.next_generation(None) == 1
    assert state.next_generation(record(generation=7)) == 8


def test_the_control_url_is_built_from_the_record_only():
    assert record().control_url == "http://127.0.0.1:51515"
    assert record(host="::1").control_url == "http://[::1]:51515"


# ── Profile isolation ─────────────────────────────────────────────────────


def test_two_profiles_keep_separate_records(tmp_path: Path, monkeypatch):
    """One profile's runtime is invisible — and unstoppable — from another."""
    first = tmp_path / "profile-a"
    second = tmp_path / "profile-b"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(first))
    state.write_record(record(runtime_id="a-runtime"))

    monkeypatch.setenv("HERMES_HOME", str(second))
    assert state.read_record() is None

    state.write_record(record(runtime_id="b-runtime"))
    assert state.read_record().runtime_id == "b-runtime"  # type: ignore[union-attr]

    monkeypatch.setenv("HERMES_HOME", str(first))
    assert state.read_record().runtime_id == "a-runtime"  # type: ignore[union-attr]


# ── The lock ──────────────────────────────────────────────────────────────


def test_the_lock_excludes_a_second_holder(hermes_home: Path):
    with state.ProfileLock(), pytest.raises(RuntimeUnavailable) as caught:
        state.ProfileLock().acquire()

    assert caught.value.reason == "runtime_locked"


def test_the_lock_is_released_on_exit(hermes_home: Path):
    with state.ProfileLock():
        pass

    with state.ProfileLock():
        pass  # a second acquisition must simply work


def test_releasing_an_unheld_lock_is_harmless(hermes_home: Path):
    lock = state.ProfileLock()

    lock.release()
    lock.release()


def test_concurrent_acquisition_admits_exactly_one(hermes_home: Path):
    """Twelve threads race; exactly one may be inside at a time."""
    state.runtime_dir()
    admitted: list[int] = []
    refused: list[int] = []
    inside = threading.Lock()
    start = threading.Barrier(12)

    def attempt(index: int) -> None:
        start.wait()
        lock = state.ProfileLock()
        try:
            lock.acquire()
        except RuntimeUnavailable:
            refused.append(index)
            return
        try:
            assert inside.acquire(blocking=False), "two holders were inside the lock at once"
            admitted.append(index)
            inside.release()
        finally:
            lock.release()

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(admitted) + len(refused) == 12
    assert admitted, "nobody acquired the lock at all"


# ── Ownership ─────────────────────────────────────────────────────────────


class FakeControl:
    """A stand-in control endpoint, recording every request it is asked to make."""

    def __init__(self, reply: dict | None, *, fail: str | None = None) -> None:
        self.reply = reply
        self.fail = fail
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, record_, method, path, *, body=None, timeout=None):
        self.calls.append((method, path, body))
        if self.fail:
            raise ownership.ControlError(self.fail)
        return dict(self.reply or {})


def reply_for(rec: state.RuntimeRecord, **overrides) -> dict:
    payload = {
        "runtime_id": rec.runtime_id,
        "generation": rec.generation,
        "pid": rec.pid,
        "executable": rec.executable,
        "started_at": rec.started_at,
        "idle_seconds": 1.0,
        "server_state": "ready",
        "tunnel_state": "ready",
        "tunnel_ready": True,
        "tunnel_url": "",
    }
    payload.update(overrides)
    return payload


def test_ownership_is_proved_by_a_matching_control_reply(monkeypatch):
    rec = record()
    monkeypatch.setattr(ownership, "_request", FakeControl(reply_for(rec)))

    assert ownership.owned(rec) is True


@pytest.mark.parametrize(
    "mismatch",
    [
        {"runtime_id": "someone-elses-runtime"},
        {"generation": 99},
        {"pid": 5555},
        {"executable": "/usr/bin/python3"},
    ],
)
def test_a_reply_that_disagrees_with_the_record_proves_nothing(monkeypatch, mismatch: dict):
    """Recycled process ids and impostors both fail here, for the same reason."""
    rec = record()
    monkeypatch.setattr(ownership, "_request", FakeControl(reply_for(rec, **mismatch)))

    assert ownership.owned(rec) is False


def test_an_unreachable_runtime_proves_nothing(monkeypatch):
    rec = record()
    monkeypatch.setattr(
        ownership, "_request", FakeControl(None, fail="control_unreachable_OSError")
    )

    assert ownership.owned(rec) is False


def test_a_malformed_reply_proves_nothing(monkeypatch):
    rec = record()
    monkeypatch.setattr(ownership, "_request", FakeControl({"runtime_id": "x"}))

    assert ownership.owned(rec) is False


def test_a_control_call_refuses_before_it_acts_when_ownership_fails(monkeypatch):
    rec = record()
    control = FakeControl(reply_for(rec, pid=99999))
    monkeypatch.setattr(ownership, "_request", control)

    with pytest.raises(ownership.ControlError):
        ownership.call(rec, ownership.GRANT_PATH, {"launch_id": "x"})

    assert [path for _method, path, _body in control.calls] == [ownership.STATUS_PATH]


def test_a_control_call_proves_ownership_before_every_action(monkeypatch):
    rec = record()
    control = FakeControl(reply_for(rec))
    monkeypatch.setattr(ownership, "_request", control)

    ownership.call(rec, ownership.GRANT_PATH, {"launch_id": "x"})

    assert [path for _method, path, _body in control.calls] == [
        ownership.STATUS_PATH,
        ownership.GRANT_PATH,
    ]


# ── Stopping ──────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_stopping_a_runtime_that_is_not_ours_signals_nothing(monkeypatch):
    """The single most important assertion in this file."""
    rec = record()
    monkeypatch.setattr(ownership, "_request", FakeControl(reply_for(rec, pid=1)))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    outcome = ownership.stop_owned(rec, graceful_seconds=1)

    assert outcome.result == "not_running"
    assert killed == []


def test_a_dead_runtime_stops_idempotently(monkeypatch):
    rec = record()
    monkeypatch.setattr(
        ownership, "_request", FakeControl(None, fail="control_unreachable_OSError")
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    first = ownership.stop_owned(rec, graceful_seconds=1)
    second = ownership.stop_owned(rec, graceful_seconds=1)

    assert (first.result, second.result) == ("not_running", "not_running")
    assert killed == []


def test_a_live_runtime_is_asked_to_stop_before_it_is_signalled(monkeypatch):
    rec = record()
    clock = Clock()
    calls: list[str] = []
    alive = {"value": True}

    def request(record_, method, path, *, body=None, timeout=None):
        calls.append(path)
        if path == ownership.SHUTDOWN_PATH:
            alive["value"] = False
            return {}
        if not alive["value"]:
            raise ownership.ControlError("control_unreachable_OSError")
        return reply_for(rec)

    monkeypatch.setattr(ownership, "_request", request)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    outcome = ownership.stop_owned(rec, graceful_seconds=5, clock=clock, sleep=clock.sleep)

    assert (outcome.result, outcome.method) == ("stopped", "control")
    assert ownership.SHUTDOWN_PATH in calls
    assert killed == []


def test_a_wedged_runtime_escalates_to_its_own_process_group(monkeypatch):
    """Escalation targets the group, from a pinned identity, proved again first."""
    monkeypatch.setattr(ownership, "handle_supported", lambda: True)
    monkeypatch.setattr(os, "pidfd_open", lambda pid, flags: 99, raising=False)
    monkeypatch.setattr(os, "close", lambda fd: None)
    rec = record()
    clock = Clock()
    alive = {"value": True}

    def request(record_, method, path, *, body=None, timeout=None):
        if path == ownership.SHUTDOWN_PATH:
            return {}
        if not alive["value"]:
            raise ownership.ControlError("control_unreachable_OSError")
        return reply_for(rec)

    monkeypatch.setattr(ownership, "_request", request)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    killed: list[tuple[int, int]] = []
    signalled: list[tuple[int, int]] = []

    def killpg(pid, sig):
        killed.append((pid, sig))
        alive["value"] = False

    monkeypatch.setattr(os, "killpg", killpg)
    # The leader is reached through the pinned descriptor, and the group by
    # number only afterwards.
    monkeypatch.setattr(
        os, "pidfd_send_signal", lambda fd, sig: signalled.append((fd, sig)), raising=False
    )

    outcome = ownership.stop_owned(rec, graceful_seconds=2, clock=clock, sleep=clock.sleep)

    assert outcome.result == "stopped"
    assert outcome.method == "sigterm"
    assert signalled and signalled[0][0] == 99
    assert killed and killed[0][0] == rec.pid


def test_a_runtime_that_leads_no_process_group_is_never_signalled(monkeypatch):
    """A group whose leader is somebody else would take strangers with it."""
    rec = record()
    clock = Clock()
    monkeypatch.setattr(ownership, "_request", FakeControl(reply_for(rec)))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    outcome = ownership.stop_owned(rec, graceful_seconds=1, clock=clock, sleep=clock.sleep)

    assert outcome.result == "unprovable"
    assert killed == []


# ── Platform support ──────────────────────────────────────────────────────


def test_the_platform_gate_agrees_with_the_signalling_gate():
    """Two implementations of one rule, kept honest against each other."""
    from learning_studio.runtime.availability import runtime_tools_supported

    assert runtime_tools_supported() == ownership.platform_supported()


def test_stopping_is_refused_outright_on_an_unsupported_platform(monkeypatch):
    monkeypatch.setattr(ownership, "platform_supported", lambda: False)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    outcome = ownership.stop_owned(record(), graceful_seconds=1)

    assert outcome.result == "unprovable"
    assert killed == []


def test_the_control_plane_speaks_http_from_the_standard_library():
    """No HTTP client dependency creeps in through the ownership path.

    The control plane talks to a loopback port, which ``http.client`` does
    perfectly well. Reaching for ``httpx`` or ``requests`` here would put a
    third-party package on the path that ``register(ctx)`` and every tool call
    walk, for no capability this needs.
    """
    source = Path(ownership.__file__).read_text(encoding="utf-8")

    assert "import http.client" in source
    for forbidden in ("httpx", "requests", "aiohttp", "urllib3"):
        assert forbidden not in source, forbidden


# ── The tunnel address, as it crosses the control plane ───────────────────


def test_a_reported_tunnel_url_is_revalidated_on_the_way_in(monkeypatch):
    """The runtime already checked it. The rule still lives in one module."""
    rec = record()
    monkeypatch.setattr(
        ownership,
        "_request",
        FakeControl(reply_for(rec, tunnel_url="https://calm-forest.trycloudflare.com")),
    )

    reply = ownership.query(rec)

    assert reply.tunnel_url == "https://calm-forest.trycloudflare.com"


@pytest.mark.parametrize(
    "reported",
    [
        "https://calm-forest.trycloudflare.com.evil.test",
        "http://calm-forest.trycloudflare.com",
        "https://calm.trycloudflare.com@evil.test",
        "https://evil.test",
        12345,
    ],
)
def test_a_runtime_reporting_an_unusable_url_is_not_believed(monkeypatch, reported):
    rec = record()
    monkeypatch.setattr(ownership, "_request", FakeControl(reply_for(rec, tunnel_url=reported)))

    with pytest.raises(ownership.ControlError):
        ownership.query(rec)


def test_a_runtime_without_a_tunnel_reports_an_empty_address(monkeypatch):
    rec = record()
    monkeypatch.setattr(ownership, "_request", FakeControl(reply_for(rec, tunnel_url="")))

    assert ownership.query(rec).tunnel_url == ""


def test_the_safe_description_of_a_reply_never_carries_the_address(monkeypatch):
    rec = record()
    monkeypatch.setattr(
        ownership,
        "_request",
        FakeControl(reply_for(rec, tunnel_url="https://calm-forest.trycloudflare.com")),
    )

    described = json.dumps(ownership.query(rec).describe())

    assert "trycloudflare" not in described
