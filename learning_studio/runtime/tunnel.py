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

#: Bounds on what is read from the tunnel process before giving up on it.
MAX_OUTPUT_LINES = 400
MAX_OUTPUT_BYTES = 128 * 1024
MAX_LINE_BYTES = 8192

#: Where a URL may appear in a line of output. Cloudflared prints it inside a
#: box of ``+---+`` characters, so the line is not the URL.
_URL_IN_LINE = re.compile(r"https://[A-Za-z0-9.-]+\.[A-Za-z]{2,63}/?")


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


def url_in(line: str) -> str | None:
    """The Quick Tunnel URL in one line of output, if there is exactly one.

    Cloudflared prints the URL inside a drawn box, so the line has to be
    searched rather than parsed. Every candidate found is put through
    :func:`validate_quick_tunnel_url`; a line with two different valid ones is
    as ambiguous as a process with two, and is refused the same way.
    """
    found: set[str] = set()
    for match in _URL_IN_LINE.finditer(line):
        with contextlib.suppress(TunnelError):
            found.add(validate_quick_tunnel_url(match.group(0)))
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

    def stop(self) -> None:
        """End the tunnel. Idempotent, and never signals anything else.

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
    else:
        if url is None:
            tunnel.state = "failed"
            tunnel.reason = "tunnel_no_url"
        else:
            tunnel.state = "ready"
            tunnel.url = url
            return tunnel

    # Every failure path converges here: the child this call created is stopped,
    # and nothing is left running that nobody is watching.
    tunnel.stop()
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


async def _read_url(process) -> str | None:
    """Read bounded output until a URL appears or the process gives up.

    Both bounds matter and they bound different things: the line count and byte
    total stop a chatty process from being read forever, while the enclosing
    ``wait_for`` stops a silent one from being *waited* on forever.
    """
    stream = process.stdout
    if stream is None:  # pragma: no cover - the spawner always requests a pipe
        raise TunnelError("tunnel_no_output")

    total = 0
    for _ in range(MAX_OUTPUT_LINES):
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            # A single line longer than the reader's limit. Not output this
            # waits around to make sense of.
            raise TunnelError("tunnel_output_line_too_long") from exc
        if not raw:
            return None  # the stream closed: the process has finished talking
        total += len(raw)
        if total > MAX_OUTPUT_BYTES:
            raise TunnelError("tunnel_output_too_large")

        found = url_in(raw.decode("utf-8", errors="replace"))
        if found is not None:
            return found
    raise TunnelError("tunnel_output_too_many_lines")
