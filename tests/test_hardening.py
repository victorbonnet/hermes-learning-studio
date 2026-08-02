"""Three narrow failures, each one a way a check could be walked past.

- an extractor that handed the validator a *prefix* of a forbidden address, so
  the validator's refusals never saw the thing they would have refused;
- a managed path that followed a symbolic link, so a control secret, a lock, a
  ``chmod`` and an interpreter could all land outside the profile;
- an HTTP client that followed redirects, with a bot token in the request path.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import pytest

from learning_studio import telegram_launch
from learning_studio.runtime import bootstrap, state, tunnel
from learning_studio.runtime.errors import LaunchRefused
from learning_studio.runtime.state import ContainmentError

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the runtime owns processes through POSIX primitives only"
)

VALID = "https://calm-forest-1234.trycloudflare.com"


# ── The extractor hands over whole tokens ─────────────────────────────────


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("userinfo", f"|  {VALID}@evil.test  |"),
        ("port", f"|  {VALID}:4443  |"),
        ("path", f"|  {VALID}/admin  |"),
        ("query", f"|  {VALID}?next=evil.test  |"),
        ("fragment", f"|  {VALID}#top  |"),
        ("userinfo with a password", "|  https://user:pass@calm.trycloudflare.com  |"),
        ("trailing label", f"|  {VALID}.evil.test  |"),
        ("percent encoded", "|  https://calm%2Eforest.trycloudflare.com  |"),
    ],
)
def test_a_forbidden_suffix_is_never_trimmed_into_a_valid_address(label: str, line: str):
    """The exact regression: the pattern stopped at the awkward character.

    ``https://victim.trycloudflare.com@evil.test`` used to yield the prefix
    ``https://victim.trycloudflare.com``, which then passed validation — so a
    refusal the validator was perfectly capable of making was never asked for.
    """
    assert tunnel.url_in(line) is None


def test_the_address_cloudflared_actually_prints_is_still_found():
    """The rule has to keep working on the real thing, box and all."""
    lines = [
        "+--------------------------------------------------------------------+",
        f"|  {VALID}                                                           |",
        "+--------------------------------------------------------------------+",
    ]

    found = [tunnel.url_in(line) for line in lines]

    assert found == [None, VALID, None]


def test_a_token_is_cut_at_whitespace_and_box_characters_only():
    tokens = tunnel.tokens_in(f"|  {VALID}@evil.test  +  {VALID}/x  |")

    assert tokens == [f"{VALID}@evil.test", f"{VALID}/x"]


# ── Conflicting addresses across lines ────────────────────────────────────


class Stream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = list(lines)

    async def readline(self) -> bytes:
        if not self.lines:
            await asyncio.sleep(3600)
        return self.lines.pop(0)


class Process:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = Stream(lines)
        self.returncode = None
        self._gone = asyncio.Event()

    def terminate(self) -> None:
        self.returncode = -15
        self._gone.set()

    def kill(self) -> None:
        self.returncode = -9
        self._gone.set()

    async def wait(self) -> int:
        await self._gone.wait()
        return self.returncode


def read(lines: list[bytes], *, settle: float = 0.05):
    process = Process(lines)
    return asyncio.run(tunnel._read_url(process, settle_seconds=settle)), process


def test_a_conflicting_address_on_a_later_line_is_refused():
    """The docstring claimed "unanimous"; the code returned on the first line.

    So two different addresses in one process's output were refused when they
    shared a line and silently accepted when they did not.
    """
    with pytest.raises(tunnel.TunnelError) as caught:
        read(
            [
                f"|  {VALID}  |\n".encode(),
                b"|  https://other-tunnel-9999.trycloudflare.com  |\n",
            ]
        )

    assert caught.value.reason == "tunnel_url_conflicting"


def test_the_same_address_repeated_on_later_lines_is_not_a_conflict():
    found, _ = read([f"{VALID}\n".encode(), b"INF chatter\n", f"{VALID}\n".encode()])

    assert found == VALID


def test_the_settle_window_closes_rather_than_waiting_for_the_process_to_end():
    """cloudflared prints its address and then runs; it never stops talking."""
    found, _ = read([f"{VALID}\n".encode()])

    assert found == VALID


# ── Managed paths are contained ───────────────────────────────────────────


@pytest.fixture
def storage(hermes_home) -> Path:
    root = hermes_home / "workspace" / "learning-studio"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_a_symlinked_runtime_directory_is_refused(storage, tmp_path):
    """It used to redirect the record, the lock, and the control secret in it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, storage / "runtime")

    with pytest.raises(ContainmentError):
        state.runtime_dir()


def test_a_symlinked_runtime_directory_is_refused_before_anything_is_written(storage, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, storage / "runtime")

    with pytest.raises(ContainmentError):
        state.write_record(_record())

    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("name", ["runtime.json", "runtime.lock", "venv.stamp", "venv"])
def test_a_symlinked_managed_file_is_refused(storage, tmp_path, name: str):
    """``chmod`` follows a link, so this is also how another file's mode changes."""
    runtime = storage / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target"
    target.write_text("someone else's file", encoding="utf-8")
    os.symlink(target, runtime / name)

    with pytest.raises(ContainmentError):
        state.managed_path(name)


def test_a_symlinked_venv_cannot_redirect_the_interpreter(storage, tmp_path):
    """The one link that would change *what code runs*."""
    runtime = storage / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "other-venv"
    elsewhere.mkdir()
    os.symlink(elsewhere, runtime / "venv")

    with pytest.raises(ContainmentError):
        bootstrap.runtime_python()


def test_an_intermediate_symlink_is_caught_by_the_containment_check(hermes_home, tmp_path):
    """A link further up the path, which the last component knows nothing about."""
    elsewhere = tmp_path / "outside"
    (elsewhere / "learning-studio").mkdir(parents=True)
    workspace = hermes_home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.symlink(elsewhere, workspace / "linked")

    with pytest.raises(ContainmentError):
        state._require_contained(hermes_home, workspace / "linked" / "learning-studio")


def test_ordinary_creation_still_works(storage):
    directory = state.runtime_dir()

    assert directory.is_dir()
    assert not directory.is_symlink()
    assert directory.resolve().is_relative_to(storage.resolve())


def test_the_lock_refuses_to_open_through_a_link(storage, tmp_path):
    runtime = storage / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target"
    target.write_text("", encoding="utf-8")
    os.symlink(target, runtime / "runtime.lock")

    with pytest.raises(ContainmentError):
        state.ProfileLock().acquire()


def _record() -> state.RuntimeRecord:
    return state.RuntimeRecord(
        runtime_id="r",
        generation=1,
        profile="default",
        pid=1,
        host="127.0.0.1",
        port=1,
        control_token="secret",
        executable="/x/python",
        started_at=0.0,
        idle_timeout_seconds=60,
        max_lifetime_seconds=300,
    )


# ── The Telegram transport follows nothing ────────────────────────────────

TOKEN = "123456789:AAHfakeTokenForTestsOnly_notARealSecret"
LAUNCH_ID = "Kx7vQm2ZpL9dR4sT"


@dataclass
class Destination:
    chat_id: str = "1001"
    telegram_user_id: str = "1001"


class Redirecting:
    """An opener that answers with a redirect, and counts follow-ups."""

    def __init__(self, code: int, location: str) -> None:
        self.code = code
        self.location = location
        self.calls: list[str] = []

    def __call__(self, endpoint: str, body: bytes, timeout: int) -> bytes:
        self.calls.append(endpoint)
        raise urllib.error.HTTPError(
            endpoint, self.code, "Moved", {"Location": self.location}, None
        )


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.test/collect",
        f"https://evil.test/bot{TOKEN}/sendMessage",
        "/relative/path",
        "https://api.telegram.org/other",
    ],
)
def test_a_redirect_is_never_followed(code: int, location: str):
    opener = Redirecting(code, location)

    with pytest.raises(LaunchRefused) as caught:
        telegram_launch.deliver_web_app_button(
            destination=Destination(),
            url=VALID,
            label="Open",
            title="Title",
            launch_id=LAUNCH_ID,
            bot_token=TOKEN,
            opener=opener,
        )

    assert len(opener.calls) == 1, "the redirect was followed"
    assert TOKEN not in str(caught.value)


def test_the_shipped_opener_refuses_redirects_and_ignores_proxies(monkeypatch):
    """Checked on the opener itself, not on a fake that stands in for it.

    ``build_opener`` drops a ``ProxyHandler({})`` entirely — an empty proxy map
    defines no ``*_open`` methods, so there is nothing to add. That is the
    intended outcome: no proxy handling at all, rather than proxy handling
    configured from environment variables nobody audited, on a request whose
    path contains a bot token.
    """
    import urllib.request

    handlers = telegram_launch._OPENER.handlers

    assert any(type(handler).__name__ == "_NoRedirects" for handler in handlers)
    assert not any(isinstance(handler, urllib.request.ProxyHandler) for handler in handlers)
    assert not any(
        isinstance(handler, urllib.request.HTTPRedirectHandler)
        and type(handler) is urllib.request.HTTPRedirectHandler
        for handler in handlers
    )


def test_a_proxy_environment_variable_does_not_redirect_the_request(monkeypatch):
    """Rebuilt under a hostile environment, to prove the opener ignores it."""
    import urllib.request

    monkeypatch.setenv("https_proxy", "http://evil.test:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.test:8080")

    opener = urllib.request.build_opener(
        telegram_launch._NoRedirects(), urllib.request.ProxyHandler({})
    )

    assert not any(isinstance(handler, urllib.request.ProxyHandler) for handler in opener.handlers)


def test_the_redirect_handler_raises_rather_than_returning_none():
    """Returning None means "do not follow" but leaves the 3xx to be handled."""
    handler = telegram_launch._NoRedirects()

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(None, None, 302, "Moved", {}, "https://evil.test")


def test_a_reflected_token_in_a_location_header_never_receives_a_request():
    opener = Redirecting(302, f"https://evil.test/?leak={TOKEN}")

    with pytest.raises(LaunchRefused):
        telegram_launch.deliver_web_app_button(
            destination=Destination(),
            url=VALID,
            label="Open",
            title="Title",
            launch_id=LAUNCH_ID,
            bot_token=TOKEN,
            opener=opener,
        )

    assert opener.calls == [opener.calls[0]]
    assert all("evil.test" not in call for call in opener.calls)


def test_the_endpoint_is_checked_before_the_request_is_made(monkeypatch):
    """A constant, asserted at the point of use as well as where it is written."""
    monkeypatch.setattr(telegram_launch, "TELEGRAM_API_ORIGIN", "https://elsewhere.test")

    with pytest.raises(LaunchRefused):
        telegram_launch._urlopen("https://api.telegram.org/botX/sendMessage", b"{}", 1)


def test_every_transport_failure_still_hides_the_token(caplog):
    class Exploding:
        def __init__(self, error):
            self.error = error

        def __call__(self, endpoint, body, timeout):
            raise self.error

    failures = [
        urllib.error.URLError(f"nope {TOKEN}"),
        urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/x", 500, "boom", {}, None),
        TimeoutError(f"timed out talking to bot{TOKEN}"),
        OSError(f"socket error for {TOKEN}"),
    ]

    for error in failures:
        with caplog.at_level("DEBUG"), pytest.raises(LaunchRefused) as caught:
            telegram_launch.deliver_web_app_button(
                destination=Destination(),
                url=VALID,
                label="Open",
                title="Title",
                launch_id=LAUNCH_ID,
                bot_token=TOKEN,
                opener=Exploding(error),
            )

        rendered = " ".join(
            [str(caught.value), caught.value.reason, str(caught.value.__cause__ or "")]
        )
        assert TOKEN not in rendered
        assert "123456789" not in rendered

    assert TOKEN not in caplog.text


def test_a_telegram_description_is_never_relayed_even_when_it_quotes_us():
    class Refusing:
        def __call__(self, endpoint, body, timeout):
            return json.dumps(
                {"ok": False, "description": f"bad request for bot{TOKEN} chat 1001"}
            ).encode()

    with pytest.raises(LaunchRefused) as caught:
        telegram_launch.deliver_web_app_button(
            destination=Destination(),
            url=VALID,
            label="Open",
            title="Title",
            launch_id=LAUNCH_ID,
            bot_token=TOKEN,
            opener=Refusing(),
        )

    assert TOKEN not in str(caught.value)
    assert "chat 1001" not in str(caught.value)


# ── Descriptor-relative, not pathname-then-write ──────────────────────────


def test_managed_files_are_only_ever_touched_through_a_directory_descriptor():
    """The check and the write must be one lookup, not two.

    Every earlier version inspected a pathname and then wrote to that pathname.
    Between the two the name can be replaced — and the record holds a control
    secret, so a redirected write hands it to whoever planted the link. The
    three functions every managed file goes through take a directory descriptor
    and a bare name, which resolves once.

    The two deliberate exceptions are named: ``_open_directory`` is what
    *produces* the descriptor, and ``ProfileLock.acquire`` honours an explicit
    path when a test hands it one, where there is no managed directory to be
    relative to.
    """
    import ast

    tree = ast.parse(Path(state.__file__).read_text(encoding="utf-8"))
    relative_only = {"open_managed", "write_managed", "remove_managed", "read_managed"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in relative_only:
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr not in ("open", "unlink", "replace"):
                continue
            keywords = {keyword.arg for keyword in call.keywords}
            assert keywords & {"dir_fd", "src_dir_fd", "dst_dir_fd"}, (
                f"os.{call.func.attr} is called by pathname in {node.name} at line {call.lineno}"
            )

    # `chmod` follows links; `fchmod` cannot. There must be none of the former
    # anywhere in the module, not merely none in those four functions.
    chmods = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "chmod"
    ]
    assert chmods == [], f"a mode change is made by pathname at {chmods}"


def test_a_link_planted_at_the_record_name_redirects_nothing(storage, tmp_path):
    """The write lands inside the runtime directory, and the link goes with it.

    A rename does not follow a link at its destination, so replacing the record
    replaces the *link* — the file it pointed at is never opened, never
    written, and never has its mode changed. That is what the old
    write-by-pathname did do.
    """
    runtime = storage / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "someone-elses-file"
    victim.write_text("untouched", encoding="utf-8")
    victim.chmod(0o644)
    os.symlink(victim, runtime / "runtime.json")

    state.write_record(_record())

    assert victim.read_text(encoding="utf-8") == "untouched"
    assert victim.stat().st_mode & 0o777 == 0o644, "a mode change followed the link"
    assert not (runtime / "runtime.json").is_symlink()
    restored = state.read_record()
    assert restored is not None and restored.runtime_id == "r"


def test_a_record_read_back_through_a_planted_link_is_ignored(storage, tmp_path):
    """Reading refuses the link rather than believing what it points at.

    A record is the input to a decision about signalling a process. One planted
    by somebody else naming somebody else's pid is exactly the input that must
    not be half-believed.
    """
    runtime = storage / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(_record().to_json()), encoding="utf-8")
    os.symlink(forged, runtime / "runtime.json")

    assert state.read_record() is None


def test_a_link_planted_at_the_stamp_name_redirects_nothing(storage, tmp_path):
    """Same rule for the bootstrap stamp, which is written after a build."""
    from learning_studio.runtime import bootstrap

    runtime = storage / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "stamp-target"
    victim.write_text("untouched", encoding="utf-8")
    victim.chmod(0o644)
    os.symlink(victim, runtime / bootstrap.STAMP_FILENAME)

    bootstrap._write_stamp(runtime)

    assert victim.read_text(encoding="utf-8") == "untouched"
    assert victim.stat().st_mode & 0o777 == 0o644
    assert not (runtime / bootstrap.STAMP_FILENAME).is_symlink()


def test_a_managed_name_that_could_escape_the_directory_is_refused():
    """Nothing passes a name from a request today. This is for tomorrow's caller."""
    for name in ("", ".", "..", "../runtime.json", "sub/dir"):
        with pytest.raises(ContainmentError):
            state.remove_managed(name)
