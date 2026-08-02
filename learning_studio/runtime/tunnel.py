"""A temporary public entrance, and the rules for believing what opens it.

``cloudflared tunnel --url http://127.0.0.1:<port>`` prints a hostname it has
just been given and this plugin then sends that hostname to a learner. So the
interesting part of this module is not starting a process — it is deciding
which of the strings a program printed on its error stream may become a URL a
person is asked to tap.

The answer is: almost none of them.

Validation, and why each rule is there
--------------------------------------

A candidate has to survive all of these, in order, before it is a URL:

- **A closed character set.** ASCII, and only the characters a URL is built
  from. This is first because it is what defeats the entire class of homograph
  and normalisation tricks: ``https://аbc.trycloudflare.com`` with a Cyrillic
  ``а`` never reaches a parser that might disagree with a human about what it
  says.
- **No percent-encoding.** A Quick Tunnel URL has none. Allowing it would mean
  the string a person reads and the string a client resolves are produced by
  two different pieces of logic, which is exactly how ``%2e`` bugs happen.
- **No userinfo, query, or fragment.** ``https://trycloudflare.com@evil.test``
  is a URL whose *host* is ``evil.test`` and which reads, to a person, as
  Cloudflare. There is no legitimate Quick Tunnel URL with an ``@`` in it.
- **No port.** Cloudflared never prints one. "Probably harmless" is not a
  reason to accept a component that only widens what this can point at.
- **One shape, stated as a whole-string pattern.** Exactly one label, then
  ``.trycloudflare.com``, then optionally a single trailing slash. Written as
  an anchored pattern over the whole candidate rather than as a suffix test,
  because a suffix test accepts ``eviltrycloudflare.com`` and a naive
  ``in`` test accepts ``trycloudflare.com.evil.test``.
- **Then parsed, and checked again.** ``urlsplit`` runs last and its opinion
  of the scheme, host, port and userinfo must agree with the pattern's. Two
  independent readings agreeing is worth something; a parser consulted alone
  is one implementation's idea of what a string means.

And two rules about the output as a whole:

- **Bounded.** A fixed number of lines and bytes. A program that has decided to
  print a megabyte is not one whose output this waits for.
- **Unanimous.** Two *different* valid URLs in one process's output is a
  refusal, not a choice. There is no principled way to pick, and picking wrong
  sends a learner's exercise to somebody else's tunnel.

What this does not do
---------------------

It never downloads ``cloudflared``, never invokes a package manager, and never
searches for an executable: the supervisor resolves one absolute path in the
process that has the operator's ``PATH``, and this module runs that or nothing.
It never returns cloudflared's own output to a caller — not the URL line, not
an error, not a warning — because that output is another program's text and
this plugin does not relay strings it did not write.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import environment as env

logger = logging.getLogger(__name__)

#: The only apex this plugin will ever hand to a learner.
QUICK_TUNNEL_DOMAIN = "trycloudflare.com"

#: The whole candidate, anchored. One label, the apex, an optional trailing
#: slash, and nothing else — no port, no userinfo, no query, no fragment.
#: ``\A``/``\Z`` rather than ``^``/``$`` because ``$`` also matches before a
#: trailing newline, which would accept ``https://x.trycloudflare.com\nevil``.
_QUICK_TUNNEL_URL = re.compile(
    r"\Ahttps://(?!-)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
    + QUICK_TUNNEL_DOMAIN.replace(".", r"\.")
    + r"/?\Z"
)

#: Characters a URL is built from. Anything else — a Unicode lookalike, a
#: control character, a space, a backslash — fails before parsing.
_URL_CHARACTERS = re.compile(r"\A[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\Z")

#: A candidate longer than this is not a hostname anybody typed.
MAX_URL_CHARS = 253

#: How long to keep reading after the first address, watching for a second.
#: Short, because it is added to every launch; long enough that a conflicting
#: line printed in the same breath is seen.
URL_SETTLE_SECONDS = 0.75

#: Bounds on what is read from the tunnel process before giving up on it.
MAX_OUTPUT_LINES = 400
MAX_OUTPUT_BYTES = 128 * 1024
MAX_LINE_BYTES = 8192

#: How a candidate is cut out of a line of output.
#:
#: This is the rule the first version got wrong, and the mistake is worth
#: stating because it looks like a detail. The pattern used to be
#: ``https://[A-Za-z0-9.-]+\.[A-Za-z]{2,63}/?`` — which stops at ``@``, ``:``,
#: ``/`` and ``?``. So given ``https://victim.trycloudflare.com@evil.test`` it
#: yielded the *prefix* ``https://victim.trycloudflare.com``, and the validator
#: then accepted that prefix and reported the address as clean.
#:
#: The validator was never wrong: it refuses userinfo, ports and paths, and its
#: tests prove it. What was wrong is that it was never shown the whole thing.
#: So extraction now takes a **complete token** — everything from ``https://``
#: up to whitespace or a box-drawing character — and hands all of it over.
#: Anything the validator dislikes is then refused rather than trimmed off.
_URL_TOKEN = re.compile(r"https://[^\s|+\-]\S*")

#: Characters cloudflared draws its box with, which end a token without being
#: part of it. Everything else — including ``@``, ``:``, ``/`` and ``?`` — is
#: kept, because keeping it is the entire point.
_TOKEN_TRAILING = "|+-–—*"


class TunnelError(Exception):
    """The tunnel could not be opened, or printed something unusable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_quick_tunnel_url(candidate: object) -> str:
    """Return the candidate as a URL fit to send a learner, or refuse it.

    The return value is the *normalised* form — lowercased host, no trailing
    slash — so that two spellings of one tunnel compare equal and only one
    string is ever stored, logged, or sent.
    """
    if not isinstance(candidate, str):
        raise TunnelError("tunnel_url_not_a_string")
    text = candidate.strip()
    if not text or len(text) > MAX_URL_CHARS:
        raise TunnelError("tunnel_url_length")
    if not _URL_CHARACTERS.match(text):
        # Unicode, control characters, whitespace and backslashes all land here.
        raise TunnelError("tunnel_url_characters")
    if "%" in text:
        raise TunnelError("tunnel_url_percent_encoded")

    lowered = text.lower()
    if not _QUICK_TUNNEL_URL.match(lowered):
        raise TunnelError("tunnel_url_not_a_quick_tunnel")

    # The second reading. If the parser disagrees with the pattern about any of
    # this, the string means two things and is refused for that reason alone.
    parts = urlsplit(lowered)
    if parts.scheme != "https":
        raise TunnelError("tunnel_url_scheme")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise TunnelError("tunnel_url_userinfo")
    if parts.query or parts.fragment:
        raise TunnelError("tunnel_url_extra_components")
    if parts.path not in ("", "/"):
        raise TunnelError("tunnel_url_path")
    try:
        if parts.port is not None:
            raise TunnelError("tunnel_url_port")
    except ValueError as exc:
        raise TunnelError("tunnel_url_port") from exc

    host = parts.hostname or ""
    if host != parts.netloc.lower():
        raise TunnelError("tunnel_url_netloc")
    if not host.endswith("." + QUICK_TUNNEL_DOMAIN):
        raise TunnelError("tunnel_url_host")
    label = host[: -len("." + QUICK_TUNNEL_DOMAIN)]
    if not label or "." in label:
        # Exactly one label. A deeper name is not the shape a Quick Tunnel has,
        # and accepting one would mean trusting a delegation nobody reviewed.
        raise TunnelError("tunnel_url_host_depth")

    return f"https://{host}"


def tokens_in(line: str) -> list[str]:
    """Every complete URL-shaped token in a line, boundaries and all.

    "Complete" is the whole idea. A token runs from ``https://`` to whitespace
    or a box-drawing character, and everything in between — userinfo, port,
    path, query, fragment — comes with it, so the validator sees what
    cloudflared actually printed rather than a convenient prefix of it.
    """
    found: list[str] = []
    for match in _URL_TOKEN.finditer(line):
        token = match.group(0).rstrip(_TOKEN_TRAILING)
        if token:
            found.append(token)
    return found


def url_in(line: str) -> str | None:
    """The Quick Tunnel URL in one line of output, if there is exactly one.

    Cloudflared prints the URL inside a drawn box, so the line has to be
    searched rather than parsed. Every *complete* token found is put through
    :func:`validate_quick_tunnel_url`; a line with two different valid ones is
    as ambiguous as a process with two, and is refused the same way.
    """
    found: set[str] = set()
    for token in tokens_in(line):
        with contextlib.suppress(TunnelError):
            found.add(validate_quick_tunnel_url(token))
    if len(found) > 1:
        raise TunnelError("tunnel_url_conflicting")
    return found.pop() if found else None


def loopback_target(host: str, port: int) -> str:
    """The one address this tunnel is allowed to publish.

    Built from the runtime's own bound host and port, so a tunnel can only ever
    front the server of the generation that started it.
    """
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{int(port)}"


def command(executable: str, target: str) -> list[str]:
    """The argument array. No shell, and no string a caller assembled.

    ``--no-autoupdate`` because a process this plugin starts must not replace
    its own executable on disk halfway through a learner's exercise.
    """
    return [executable, "tunnel", "--no-autoupdate", "--url", target]


def child_environment(source: dict[str, str]) -> dict[str, str]:
    """The tunnel's environment: less than the runtime's, on purpose.

    No bot token, no allowlist, no ``HERMES_HOME``. A tunnel forwards bytes to
    a loopback port; it has no business holding a credential or knowing where a
    learner's database lives.
    """
    return {name: source[name] for name in env.TUNNEL_INHERITED if source.get(name)}


@dataclass
class QuickTunnel:
    """One tunnel process, and what is known about it.

    Owned by the runtime that started it — the handle below is a live child of
    *this* process, which is a stronger claim than any recorded process id, so
    stopping it needs no challenge.
    """

    #: ``pending`` | ``ready`` | ``failed`` | ``stopped``
    state: str = "pending"
    url: str = ""
    reason: str = ""
    process: object | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return self.state == "ready" and bool(self.url)

    def describe(self) -> dict[str, object]:
        """Safe status. The URL is deliberately absent."""
        return {"tunnel_state": self.state, "tunnel_ready": self.ready}

    @property
    def alive(self) -> bool:
        """True when the tunnel process is still running.

        Read on every watchdog tick. A tunnel that exits after publishing a URL
        leaves a runtime that believes it has a public entrance and has not —
        so the learner taps a button that goes nowhere and the agent has been
        told the exercise is open.
        """
        process = self.process
        return process is not None and getattr(process, "returncode", None) is None

    async def aclose(self, *, grace_seconds: float = 5.0) -> None:
        """End the tunnel and **wait for it to be gone**. Idempotent.

        Terminate, wait, kill, wait. The previous version called ``terminate``
        and returned, so a cloudflared that ignores SIGTERM outlived the stop
        that reported success — and a caller was told the public address was
        closed while it was still open.

        The only process touched is the child this object is holding. There is
        no process id here that was read from anywhere.
        """
        process = self.process
        self.process = None
        if self.state != "failed":
            self.state = "stopped"
        if process is None:
            return

        with contextlib.suppress(ProcessLookupError, OSError):
            if getattr(process, "returncode", None) is None:
                process.terminate()
        if await _reaped(process, grace_seconds):
            return

        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        if not await _reaped(process, grace_seconds):
            # Reported rather than hidden: the caller has to be able to say
            # "the address may still be open" instead of claiming it is closed.
            logger.warning("the tunnel process did not exit after being killed")
            raise TunnelError("tunnel_cleanup_indeterminate")

    def stop(self) -> None:
        """Synchronous best effort, for a caller with no event loop.

        Prefer :meth:`aclose`, which waits. This exists because a teardown path
        that cannot await still has to try, and terminating without waiting is
        better than not terminating.
        """
        process = self.process
        self.process = None
        if self.state != "failed":
            self.state = "stopped"
        if process is None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            if getattr(process, "returncode", None) is None:
                process.terminate()


async def _reaped(process, timeout: float) -> bool:
    """Wait, bounded, for a process to be gone."""
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (TimeoutError, ProcessLookupError, OSError):
        return getattr(process, "returncode", None) is not None
    return True


async def open_tunnel(
    *,
    executable: str,
    target: str,
    environment: dict[str, str],
    timeout_seconds: float,
    spawn=None,
) -> QuickTunnel:
    """Start ``cloudflared`` and wait, bounded, for a URL worth believing.

    Returns a :class:`QuickTunnel` in either the ``ready`` or ``failed`` state.
    It never raises for an ordinary failure — a missing binary, a crash, a
    timeout, unusable output — because every one of those means the same thing
    to the caller and is rolled back the same way.
    """
    tunnel = QuickTunnel()
    if not executable:
        tunnel.state = "failed"
        tunnel.reason = "tunnel_executable_absent"
        return tunnel

    spawner = spawn or _spawn
    try:
        process = await spawner(command(executable, target), environment)
    except (OSError, ValueError) as exc:
        logger.warning("the tunnel process could not be started: %s", type(exc).__name__)
        tunnel.state = "failed"
        tunnel.reason = "tunnel_spawn_failed"
        return tunnel

    tunnel.process = process
    try:
        url = await asyncio.wait_for(_read_url(process), timeout=timeout_seconds)
    except TimeoutError:
        tunnel.state = "failed"
        tunnel.reason = "tunnel_readiness_timeout"
    except TunnelError as exc:
        tunnel.state = "failed"
        tunnel.reason = exc.reason
    except BaseException:
        # Cancellation, most likely: the runtime is shutting down while this is
        # still waiting. The process was created by this call and nothing else
        # knows about it yet, so it has to be stopped here or it outlives the
        # runtime that started it.
        #
        # Synchronously, and deliberately so. Awaiting inside a cancelled task
        # raises `CancelledError` again at the first suspension point, so an
        # `await tunnel.aclose()` here would never reach the process at all.
        # `stop()` signals without suspending, which is the part that has to
        # happen; the bounded wait is what cannot be had on this path.
        tunnel.stop()
        raise
    else:
        if url is None:
            tunnel.state = "failed"
            tunnel.reason = "tunnel_no_url"
        elif not tunnel.alive:
            # A process that printed an address and then exited has published
            # nothing: the hostname it named is not being served. Reporting it
            # ready would send a learner to a URL that answers nobody.
            tunnel.state = "failed"
            tunnel.reason = "tunnel_exited_after_publishing"
        else:
            tunnel.state = "ready"
            tunnel.url = url
            return tunnel

    # Every failure path converges here: the child this call created is stopped
    # and waited for, so nothing is left running that nobody is watching.
    with contextlib.suppress(TunnelError):
        await tunnel.aclose()
    tunnel.state = "failed"
    return tunnel


async def _spawn(argv: list[str], environment: dict[str, str]):
    """Start the tunnel as an argument array, with a bounded line reader.

    ``create_subprocess_exec`` rather than ``create_subprocess_shell``: there is
    no shell anywhere in this package, so there is no string for one to
    reinterpret and no quoting to get wrong.
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=environment,
        limit=MAX_LINE_BYTES,
        start_new_session=False,
    )


async def _read_url(process, *, settle_seconds: float = URL_SETTLE_SECONDS) -> str | None:
    """Read bounded output until a URL appears, then keep reading for a moment.

    Three bounds, each stopping a different thing: the line count and byte
    total stop a chatty process from being read forever, and the enclosing
    ``wait_for`` stops a silent one from being *waited* on forever.

    The settle window is the fourth, and it is what makes the "unanimous
    output" rule true rather than merely stated. The first version returned on
    the first line carrying a URL, so a *second, different* address on the next
    line was never looked at — the module refused conflicting URLs within one
    line and silently accepted them across two. Now the first address is held,
    reading continues for a brief bounded window, and a conflicting one turns
    the whole thing into a refusal.

    It is a window rather than "read to the end" because cloudflared does not
    end: it prints its address and then runs. Waiting for the stream to close
    would mean waiting for the tunnel to die.
    """
    stream = process.stdout
    if stream is None:  # pragma: no cover - the spawner always requests a pipe
        raise TunnelError("tunnel_no_output")

    total = 0
    first: str | None = None
    deadline: float | None = None

    for _ in range(MAX_OUTPUT_LINES):
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        if timeout == 0.0:
            return first
        try:
            raw = await (
                stream.readline()
                if timeout is None
                else asyncio.wait_for(stream.readline(), timeout=timeout)
            )
        except TimeoutError:
            # The settle window closed with nothing else to say. That is the
            # ordinary case: one address, then silence.
            return first
        except (ValueError, asyncio.LimitOverrunError) as exc:
            # A single line longer than the reader's limit. Not output this
            # waits around to make sense of.
            raise TunnelError("tunnel_output_line_too_long") from exc
        if not raw:
            return first  # the stream closed: the process has finished talking
        total += len(raw)
        if total > MAX_OUTPUT_BYTES:
            raise TunnelError("tunnel_output_too_large")

        found = url_in(raw.decode("utf-8", errors="replace"))
        if found is None:
            continue
        if first is None:
            first = found
            deadline = time.monotonic() + settle_seconds
        elif found != first:
            raise TunnelError("tunnel_url_conflicting")

    if first is not None:
        return first
    raise TunnelError("tunnel_output_too_many_lines")
