"""The adversarial pass over the launch feature, as one file.

Each section below is a claim the pull request makes. Several of them are also
checked where the behaviour lives; they are restated here so that the whole set
can be read in one sitting, and so that a change which quietly weakened one of
them fails a test named after the promise rather than after the function.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import signal
from pathlib import Path

import pytest

from learning_studio import launch, telegram_launch
from learning_studio.config import LearningStudioConfig
from learning_studio.runtime import (
    bootstrap,
    environment,
    grants,
    manager,
    ownership,
    state,
    supervisor,
    tunnel,
)
from learning_studio.schemas import TOOL_SCHEMAS

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the runtime owns processes through POSIX primitives only"
)

PACKAGE = Path(__file__).resolve().parent.parent / "learning_studio"

RUNTIME_TOOLS = (
    "learning_studio_launch",
    "learning_studio_status",
    "learning_studio_results",
    "learning_studio_stop",
)


def sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def relative(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


# ── The model controls nothing that matters ───────────────────────────────


def test_no_tool_schema_anywhere_names_a_machine_or_a_person():
    """Across all eight tools, not just the new ones."""
    forbidden = {
        "host",
        "port",
        "url",
        "executable",
        "command",
        "argv",
        "pid",
        "signal",
        "env",
        "environment",
        "timeout",
        "lock_path",
        "chat_id",
        "telegram_user_id",
        "user_id",
        "learner_key",
        "learner_id",
        "bot_token",
        "token",
        "profile",
    }

    offenders = []
    for name, schema in TOOL_SCHEMAS.items():
        for field in schema["parameters"].get("properties", {}):
            if field in forbidden:
                offenders.append(f"{name}.{field}")

    assert offenders == [], offenders


def test_the_launch_orchestration_reads_no_argument_for_a_destination():
    source = Path(launch.__file__).read_text(encoding="utf-8")

    for reached in ("chat_id", "telegram_user_id", "bot_token"):
        assert f'args["{reached}"]' not in source
        assert f'args.get("{reached}")' not in source


def test_the_launch_signature_accepts_nothing_a_model_should_not_supply():
    parameters = set(inspect.signature(launch.launch_experience).parameters)

    assert parameters == {
        "principal",
        "experience_id",
        "initiation",
        "learner_quote",
        "learner_confirmed",
        "config",
        "deliver",
        "evidence",
    }


# ── No shell, anywhere ────────────────────────────────────────────────────


def test_no_module_passes_shell_true_or_builds_a_command_string():
    offenders: list[str] = []
    for path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "shell":
                    continue
                if not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False):
                    offenders.append(f"{relative(path)}:{node.lineno}")

    assert offenders == [], offenders


def test_every_command_this_plugin_builds_is_a_list_of_strings():
    built = tunnel.command("/usr/bin/cloudflared", "http://127.0.0.1:1")

    assert isinstance(built, list)
    assert all(isinstance(part, str) for part in built)
    assert not any(" " in part and part.startswith("-") for part in built)


# ── Nothing unprovable is signalled ───────────────────────────────────────


def record(**overrides) -> state.RuntimeRecord:
    fields = {
        "runtime_id": "r",
        "generation": 1,
        "profile": "default",
        "pid": 4242,
        "host": "127.0.0.1",
        "port": 40404,
        "control_token": "token",
        "executable": "/x/python",
        "started_at": 0.0,
        "idle_timeout_seconds": 60,
        "max_lifetime_seconds": 300,
    }
    fields.update(overrides)
    return state.RuntimeRecord(**fields)


def test_a_recycled_process_id_is_never_signalled(monkeypatch):
    """The nastiest realistic failure, stated as one test.

    The runtime died; the operating system gave its number to somebody's
    database. The database cannot answer a challenge it was never told about,
    so it is not signalled, and the record is simply forgotten.
    """
    stranger = record()
    monkeypatch.setattr(
        ownership,
        "_request",
        lambda *a, **k: (_ for _ in ()).throw(ownership.ControlError("control_unreachable")),
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    outcome = ownership.stop_owned(stranger, graceful_seconds=1)

    assert outcome.result == "not_running"
    assert killed == []


def test_a_process_that_knows_the_secret_but_is_not_ours_is_not_signalled(monkeypatch):
    """Every identifying field has to agree, not just the one that authenticates."""
    ours = record()
    monkeypatch.setattr(
        ownership,
        "_request",
        lambda rec, *a, **k: {
            "runtime_id": rec.runtime_id,
            "generation": rec.generation,
            "pid": rec.pid + 1,  # something else is answering for us
            "executable": rec.executable,
            "started_at": 0.0,
            "idle_seconds": None,
            "server_state": "ready",
            "tunnel_state": "ready",
            "tunnel_ready": False,
            "tunnel_url": "",
        },
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    assert ownership.stop_owned(ours, graceful_seconds=1).result == "not_running"
    assert killed == []


def test_no_process_is_ever_addressed_by_number_here(monkeypatch):
    """A number can name a stranger; a descriptor cannot.

    The group kill existed to reach ``cloudflared``, which used to share the
    runtime's process group. It has its own session now — so the runtime can
    end it and its descendants without ending itself — and the runtime's group
    holds nothing but the runtime. Signalling that group by number bought a
    reuse race in exchange for nothing, so it is gone.
    """
    source = Path(ownership.__file__).read_text(encoding="utf-8")

    assert "os.killpg" not in source
    assert "os.kill(" not in source
    assert "os.getpgid" not in source


def test_a_signal_is_only_ever_sent_through_a_pinned_identity():
    """No signal in this module is addressed by number.

    The first version proved ownership in userspace, discarded the proof, then
    called ``getpgid`` and ``killpg`` on a number — and between those the
    runtime could exit and the number could be reused. The second held a pidfd
    but still delivered with ``killpg``, which narrowed the window without
    closing it. There is now no numeric signalling here at all: the descriptor
    is the identity, and it is the thing that delivers.
    """
    import ast

    tree = ast.parse(Path(ownership.__file__).read_text(encoding="utf-8"))
    calls: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in ("killpg", "kill", "pidfd_send_signal")
            ):
                calls.setdefault(node.name, []).append(inner.func.attr)

    assert list(calls) == ["signal_runtime"], calls
    assert calls["signal_runtime"] == ["pidfd_send_signal"], calls["signal_runtime"]


def test_the_pidfd_sender_is_probed_where_python_actually_puts_it():
    """``pidfd_open`` is in ``os``; ``pidfd_send_signal`` is in ``signal``.

    Probing the wrong module is not a crash and not a test failure — `hasattr`
    just answers False, for ever. The whole escalation path then reports the
    platform unsupported, on Linux, where it is supported, and a wedged runtime
    is never stopped. That is exactly what happened, and nothing caught it,
    which is why this assertion is about the standard library rather than about
    this package.
    """
    import signal as signal_module

    assert not hasattr(os, "pidfd_send_signal"), "the probe below would be vacuous"
    assert ownership.handle_supported() == (
        hasattr(os, "pidfd_open") and hasattr(signal_module, "pidfd_send_signal")
    )

    source = Path(ownership.__file__).read_text(encoding="utf-8")
    assert "signal.pidfd_send_signal" in source
    assert "os.pidfd_send_signal" not in source


def test_escalation_fails_closed_where_no_identity_can_be_pinned(monkeypatch):
    """macOS has no pidfd. Refusing to signal is the correct answer there."""
    monkeypatch.setattr(ownership, "handle_supported", lambda: False)
    monkeypatch.setattr(ownership, "_request", _answering(record()))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    clock = _Clock()
    outcome = ownership.stop_owned(record(), graceful_seconds=1, clock=clock, sleep=clock.sleep)

    assert outcome.result == "unprovable"
    assert killed == [], "a signal was sent to an identity nothing was holding"


def test_a_pid_reused_between_proof_and_signal_is_never_reached(monkeypatch):
    """The canary: the runtime exits, its number is recycled, we signal nobody.

    Simulated deterministically rather than by racing: acquiring the handle is
    what would have pinned the identity, and here it fails because the process
    has already gone — which is exactly the state a recycled pid is reached
    from.
    """
    monkeypatch.setattr(ownership, "handle_supported", lambda: True)
    monkeypatch.setattr(ownership, "_request", _answering(record()))
    monkeypatch.setattr(
        os,
        "pidfd_open",
        lambda pid, flags: (_ for _ in ()).throw(ProcessLookupError(pid)),
        raising=False,
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    clock = _Clock()
    ownership.stop_owned(record(), graceful_seconds=1, clock=clock, sleep=clock.sleep)

    assert killed == []


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _answering(rec):
    def request(record_, method, path, *, body=None, timeout=None):
        if path == ownership.SHUTDOWN_PATH:
            return {}
        return {
            "runtime_id": record_.runtime_id,
            "generation": record_.generation,
            "pid": record_.pid,
            "executable": record_.executable,
            "started_at": 0.0,
            "idle_seconds": None,
            "server_state": "ready",
            "tunnel_state": "ready",
            "tunnel_ready": False,
            "tunnel_url": "",
        }

    return request


# ── Corrupt state fails closed ────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{",
        "[]",
        json.dumps({"schema": 1}),
        json.dumps({"schema": 2, "pid": 1}),
        json.dumps({"schema": 1, "pid": "1"}),
    ],
)
def test_a_malformed_record_is_no_runtime_at_all(hermes_home, payload: str):
    state.runtime_dir()
    state.record_path().write_text(payload, encoding="utf-8")

    assert state.read_record() is None


def test_a_malformed_record_leaves_status_saying_nothing_is_running(hermes_home):
    state.runtime_dir()
    state.record_path().write_text("{not json", encoding="utf-8")

    reported = manager.status(LearningStudioConfig())

    assert reported["running"] is False
    assert reported["stale_record"] is False


# ── Profile and learner isolation ─────────────────────────────────────────


def test_one_profile_cannot_see_or_stop_another(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(first))
    state.write_record(record(runtime_id="a-runtime"))

    monkeypatch.setenv("HERMES_HOME", str(second))
    assert state.read_record() is None
    assert manager.stop(LearningStudioConfig())["state"] == "not_running"

    monkeypatch.setenv("HERMES_HOME", str(first))
    assert state.read_record() is not None


def test_a_grant_admits_only_the_account_it_was_created_for():
    """A copied button URL, in somebody else's hands, opens nothing."""
    store = grants.GrantStore(profile="default", generation=1, clock=lambda: 0.0)
    created = store.create(
        {"telegram_user_id": "1001", "learner_id": "learner-a", "experience_id": "exp-1"}
    )
    selector = created["launch_id"]
    store.activate(selector)

    assert store.admit(launch_id=selector, telegram_user_id="2002") is None
    assert store.admit(launch_id="Zz" + "0" * 20, telegram_user_id="1001") is None
    assert store.admit(launch_id=selector, telegram_user_id="1001") is not None


@pytest.mark.parametrize(
    "selector", ["", "short", "../../etc", "x" * 200, "has spaces here at all", None]
)
def test_a_malformed_selector_admits_nothing(selector):
    store = grants.GrantStore(profile="default", generation=1, clock=lambda: 0.0)
    created = store.create({"telegram_user_id": "1001", "learner_id": "l", "experience_id": "e"})
    store.activate(created["launch_id"])

    assert store.admit(launch_id=selector, telegram_user_id="1001") is None


def test_a_selector_from_another_generation_admits_nothing():
    """A runtime that was replaced must not honour the old one's buttons."""
    first = grants.GrantStore(profile="default", generation=4, clock=lambda: 0.0)
    created = first.create({"telegram_user_id": "1001", "learner_id": "l", "experience_id": "e"})
    first.activate(created["launch_id"])
    second = grants.GrantStore(profile="default", generation=5, clock=lambda: 0.0)

    assert second.admit(launch_id=created["launch_id"], telegram_user_id="1001") is None


def test_an_expired_selector_admits_nothing():
    clock = {"now": 0.0}
    store = grants.GrantStore(profile="default", generation=1, clock=lambda: clock["now"])
    created = store.create({"telegram_user_id": "1001", "learner_id": "l", "experience_id": "e"})
    store.activate(created["launch_id"])

    clock["now"] = grants.DEFAULT_GRANT_TTL_SECONDS + 1

    assert store.admit(launch_id=created["launch_id"], telegram_user_id="1001") is None


def test_a_revoked_selector_admits_nothing():
    store = grants.GrantStore(profile="default", generation=1, clock=lambda: 0.0)
    created = store.create({"telegram_user_id": "1001", "learner_id": "l", "experience_id": "e"})
    store.activate(created["launch_id"])

    store.revoke(created["launch_id"])

    assert store.admit(launch_id=created["launch_id"], telegram_user_id="1001") is None


def test_progress_cannot_be_read_for_another_account():
    store = grants.GrantStore(profile="default", generation=1, clock=lambda: 0.0)
    store.create({"telegram_user_id": "1001", "learner_id": "learner-a", "experience_id": "exp-1"})

    theirs = store.progress({"telegram_user_id": "2002", "experience_id": "exp-1"})

    assert theirs == {"found": False}


def test_a_grant_is_bound_to_one_runtime_generation():
    """A grant from generation 4 must not be honoured by generation 5."""
    store = grants.GrantStore(profile="default", generation=4, clock=lambda: 0.0)

    created = store.create({"telegram_user_id": "1001", "learner_id": "l", "experience_id": "e"})

    assert created["generation"] == 4


# ── Nothing leaks ─────────────────────────────────────────────────────────


def test_the_runtime_record_summary_carries_no_secret_or_locator():
    described = json.dumps(record(control_token="s3cret").describe())

    for forbidden in ("s3cret", "127.0.0.1", "40404", "/x/python", "4242"):
        assert forbidden not in described, forbidden


def test_no_error_message_shown_to_an_agent_contains_a_locator():
    from learning_studio.runtime import errors

    messages = [
        value for name, value in vars(errors).items() if name.isupper() and isinstance(value, str)
    ]

    assert messages
    for message in messages:
        for forbidden in ("http://", "https://", "/Users", "/home", "127.0.0.1", "TOKEN", ".env"):
            assert forbidden not in message, message


def test_a_bot_token_shaped_string_is_removed_by_the_redactor():
    token = "7654321:AAEabcdefghijklmnopqrstuvwxyz0123456"

    cleaned = telegram_launch.redact(f"POST /bot{token}/sendMessage failed for 7654321", token)

    assert token not in cleaned
    assert "7654321" not in cleaned


def test_the_control_secret_travels_in_the_environment_and_not_an_argument(hermes_home):
    """The process table is readable by every user on the machine.

    Checked by building both halves rather than by reading the source: the
    command is what `ps` would show, and the environment is what it would not.
    """
    secret = "a-very-secret-control-token"
    child = supervisor.child_environment(
        record(control_token=secret),
        handshake=Path("/tmp/handshake.json"),
        cloudflared="/usr/bin/cloudflared",
        source={},
    )
    command = [str(bootstrap.runtime_python()), str(supervisor.LAUNCHER)]

    assert child[environment.CONTROL_TOKEN] == secret
    assert secret not in " ".join(command)
    assert len(command) == 2


def test_the_tunnel_child_is_given_neither_credential_nor_profile():
    assert "TELEGRAM_BOT_TOKEN" not in environment.TUNNEL_INHERITED
    assert "HERMES_HOME" not in environment.TUNNEL_INHERITED
    assert set(environment.TUNNEL_INHERITED) < set(environment.INHERITED)


def test_the_child_environment_is_an_allowlist_not_a_copy():
    """A copy-and-delete list passes tomorrow's credential straight through."""
    child = supervisor.child_environment(
        record(),
        handshake=Path("/tmp/h.json"),
        cloudflared="",
        source={"ANTHROPIC_API_KEY": "sk-x", "AWS_SESSION_TOKEN": "y", "HOME": "/home/x"},
    )

    assert "ANTHROPIC_API_KEY" not in child
    assert "AWS_SESSION_TOKEN" not in child
    assert child["HOME"] == "/home/x"


# ── Tunnel validation resists look-alikes ─────────────────────────────────


@pytest.mark.parametrize(
    "candidate",
    [
        "https://trycloudflare.com.attacker.test",
        "https://eviltrycloudflare.com",
        "https://a.trycloudflare.com.evil.test",
        "https://a.trycloudflare.com@evil.test",
        "https://a.trycloudflare.com:8443",
        "http://a.trycloudflare.com",
        "https://аbc.trycloudflare.com",
        "https://a.trycloudflare.com/../x",
    ],
)
def test_a_look_alike_address_is_never_accepted(candidate: str):
    with pytest.raises(tunnel.TunnelError):
        tunnel.validate_quick_tunnel_url(candidate)


def test_a_runtime_reporting_a_look_alike_is_not_believed(monkeypatch):
    monkeypatch.setattr(
        ownership,
        "_request",
        lambda rec, *a, **k: {
            "runtime_id": rec.runtime_id,
            "generation": rec.generation,
            "pid": rec.pid,
            "executable": rec.executable,
            "started_at": 0.0,
            "idle_seconds": None,
            "server_state": "ready",
            "tunnel_state": "ready",
            "tunnel_ready": True,
            "tunnel_url": "https://a.trycloudflare.com.evil.test",
        },
    )

    with pytest.raises(ownership.ControlError):
        ownership.query(record())


# ── Consent cannot be fabricated or spent twice ───────────────────────────


def test_a_launch_cannot_be_authorised_by_words_nobody_wrote():
    """The model may read the meaning; it may not supply the words."""
    from learning_studio.evidence import EvidenceKey, EvidenceStore

    store = EvidenceStore(clock=lambda: 0.0)
    store.record(
        EvidenceKey("default", "telegram", "1001", "", "1001", "555"),
        "what does chlorophyll actually do",
    )

    assert (
        store.state(
            EvidenceKey("default", "telegram", "1001", "", "1001", "555"),
            "quiz me on photosynthesis",
        )
        == "mismatched"
    )


def test_one_trusted_message_authorises_one_launch():
    from learning_studio.evidence import EvidenceKey, EvidenceStore

    store = EvidenceStore(clock=lambda: 0.0)
    key = EvidenceKey("default", "telegram", "1001", "", "1001", "555")
    store.record(key, "quiz me on photosynthesis")

    assert store.spend(key) is True
    assert store.spend(key) is False


def test_a_spent_message_never_becomes_fresh_again():
    """The exact regression: expiry used to delete the record and re-open it."""
    clock = {"now": 0.0}
    from learning_studio.evidence import EvidenceKey, EvidenceStore

    store = EvidenceStore(clock=lambda: clock["now"])
    key = EvidenceKey("default", "telegram", "1001", "", "1001", "555")
    store.record(key, "quiz me on photosynthesis")
    store.spend(key)

    for elapsed in (1, 601, 1200, 1799):
        clock["now"] = elapsed
        assert store.state(key, "quiz me on photosynthesis") == "spent"


def test_one_learner_message_does_not_authorise_another_identity():
    from learning_studio.evidence import EvidenceKey, EvidenceStore

    store = EvidenceStore(clock=lambda: 0.0)
    store.record(EvidenceKey("default", "telegram", "1001", "", "1001", "555"), "quiz me now")

    for other in (
        EvidenceKey("default", "telegram", "1001", "", "2002", "555"),
        EvidenceKey("family", "telegram", "1001", "", "1001", "555"),
        EvidenceKey("default", "telegram", "-100777", "", "1001", "555"),
        EvidenceKey("default", "telegram", "1001", "", "1001", "556"),
    ):
        assert store.state(other, "quiz me now") == "absent"


def test_consent_evidence_is_never_written_to_disk():
    """A learner's own words, held in memory, in the process that had them."""
    source = Path(PACKAGE / "evidence.py").read_text(encoding="utf-8")

    for forbidden in ("open(", "write_text", "sqlite", "storage", "Path("):
        assert forbidden not in source, forbidden


# ── The rest of the plugin is unchanged ───────────────────────────────────


def test_the_plugin_still_writes_no_hermes_memory():
    for path in sources():
        body = path.read_text(encoding="utf-8")
        assert "memory_tool" not in body, relative(path)
        assert "MEMORY.md" not in body, relative(path)


def test_results_propose_no_durable_memory_from_running_an_exercise():
    """A one-off event is not a fact that stays true."""
    source = Path(launch.__file__).read_text(encoding="utf-8")

    assert '"memory_candidates": []' in source


def test_nothing_downloads_or_installs_anything():
    for path in sources():
        body = path.read_text(encoding="utf-8").lower()
        for forbidden in ("urlretrieve", "wget ", "curl -", "apt-get", "brew install"):
            assert forbidden not in body, f"{relative(path)} mentions {forbidden}"


def test_the_bootstrap_is_never_run_automatically():
    """A launch reports a missing environment; it does not build one."""
    for path in sources():
        if relative(path) in ("runtime/bootstrap.py",):
            continue
        body = path.read_text(encoding="utf-8")
        assert "bootstrap.bootstrap(" not in body, relative(path)


def test_the_bootstrap_never_asks_for_privilege():
    body = Path(bootstrap.__file__).read_text(encoding="utf-8")

    for forbidden in ("sudo", "setuid", "os.setuid", "--break-system-packages"):
        assert forbidden not in body, forbidden


def test_registration_is_still_free_of_side_effects(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    from learning_studio import register

    register(ctx)

    assert not (tmp_path / "fresh").exists(), "registration created state on disk"
    assert len(ctx.tools) == 12


def test_the_group_is_never_signalled_when_the_leader_could_not_be(monkeypatch):
    """If the pinned identity refuses the signal, the number is not used instead.

    ``pidfd_send_signal`` failing means the leader is gone or unreachable — and
    a gone leader is exactly when its pid becomes available to somebody else.
    Falling through to ``killpg`` there would send the signal to whoever
    inherited the number, which is the failure this whole module exists to
    prevent.
    """
    handle = ownership.ProcessHandle(4242, 99)
    monkeypatch.setattr(ownership, "_request", _answering(record()))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda fd, sig: (_ for _ in ()).throw(ProcessLookupError(fd)),
        raising=False,
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    assert handle.signal_runtime(signal.SIGTERM, record()) is False
    assert killed == [], "a signal went to a number after the identity refused it"


def test_the_runtime_is_signalled_by_descriptor_and_nothing_else_is(monkeypatch):
    """The successful path touches the descriptor and no number at all."""
    handle = ownership.ProcessHandle(4242, 99)
    monkeypatch.setattr(ownership, "_request", _answering(record()))
    order: list[str] = []
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda fd, sig: order.append(f"pidfd:{fd}:{sig}"),
        raising=False,
    )
    monkeypatch.setattr(os, "killpg", lambda pid, sig: order.append(f"group:{pid}:{sig}"))

    assert handle.signal_runtime(signal.SIGTERM, record()) is True
    assert order == [f"pidfd:99:{int(signal.SIGTERM)}"]
