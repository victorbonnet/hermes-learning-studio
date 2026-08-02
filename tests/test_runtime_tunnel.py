"""The Quick Tunnel: what may become a URL, and what starting one may do.

No test here opens a tunnel, resolves a name, or reaches Cloudflare. The
process is a fake that prints lines; the interesting half of the module is a
pure function over a string, and it is attacked accordingly.
"""

from __future__ import annotations

import asyncio

import pytest

from learning_studio.runtime import environment as env
from learning_studio.runtime import tunnel

VALID = "https://calm-forest-1234.trycloudflare.com"


def validate(candidate):
    return tunnel.validate_quick_tunnel_url(candidate)


# ── What a URL has to be ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "candidate",
    [
        "https://calm-forest-1234.trycloudflare.com",
        "https://calm-forest-1234.trycloudflare.com/",
        "  https://calm-forest-1234.trycloudflare.com  ",
        "HTTPS://Calm-Forest-1234.TryCloudflare.com",
        "https://a.trycloudflare.com",
        "https://x1.trycloudflare.com",
    ],
)
def test_a_genuine_quick_tunnel_url_is_accepted_and_normalised(candidate: str):
    accepted = validate(candidate)

    assert accepted.startswith("https://")
    assert accepted.endswith(".trycloudflare.com")
    assert accepted == accepted.lower()


def test_the_normalised_form_is_lowercase_and_has_no_trailing_slash():
    assert validate("HTTPS://Calm-Forest-1234.TryCloudflare.com/") == VALID


@pytest.mark.parametrize(
    ("label", "candidate"),
    [
        # Scheme
        ("plain http", "http://calm-forest-1234.trycloudflare.com"),
        ("no scheme", "calm-forest-1234.trycloudflare.com"),
        ("scheme-relative", "//calm-forest-1234.trycloudflare.com"),
        ("javascript", "javascript:alert(1)"),
        ("data", "data:text/html,hello"),
        ("file", "file:///etc/passwd"),
        ("ws", "wss://calm-forest-1234.trycloudflare.com"),
        # Look-alike hosts
        ("suffix without a dot", "https://eviltrycloudflare.com"),
        ("apex inside a longer label", "https://trycloudflare.com.evil.test"),
        ("apex as a prefix", "https://trycloudflare.company.test"),
        ("wrong tld", "https://calm-forest.trycloudflare.co"),
        ("wrong tld 2", "https://calm-forest.trycloudflare.com.br"),
        ("hyphen instead of a dot", "https://calm-forest-trycloudflare.com"),
        ("the bare apex", "https://trycloudflare.com"),
        ("two labels deep", "https://a.b.trycloudflare.com"),
        ("punycode label", "https://xn--trycloudflare-1234.com"),
        ("punycode subdomain", "https://xn--80ak6aa92e.trycloudflare.com.evil.test"),
        # Authority tricks
        ("userinfo", "https://calm-forest-1234.trycloudflare.com@evil.test"),
        ("userinfo with a password", "https://user:pass@calm.trycloudflare.com"),
        ("bare at sign", "https://calm.trycloudflare.com@"),
        ("explicit https port", "https://calm-forest.trycloudflare.com:443"),
        ("other port", "https://calm-forest.trycloudflare.com:8080"),
        ("malformed port", "https://calm-forest.trycloudflare.com:notaport"),
        ("backslash authority", "https:\\\\calm-forest.trycloudflare.com"),
        # Extra components
        ("query string", "https://calm-forest.trycloudflare.com?next=evil.test"),
        ("fragment", "https://calm-forest.trycloudflare.com#top"),
        ("path", "https://calm-forest.trycloudflare.com/admin"),
        ("path traversal", "https://calm-forest.trycloudflare.com/../evil"),
        # Encoding and Unicode
        ("percent-encoded dot", "https://calm%2Eforest.trycloudflare.com"),
        ("percent-encoded slash", "https://calm.trycloudflare.com%2F@evil.test"),
        ("cyrillic lookalike", "https://cаlm-forest.trycloudflare.com"),
        ("fullwidth", "https://ｃalm.trycloudflare.com"),
        ("zero width joiner", "https://calm​forest.trycloudflare.com"),
        ("trailing newline and a second url", "https://a.trycloudflare.com\nhttps://evil.test"),
        ("embedded tab", "https://a.trycloudflare.com\tx"),
        ("embedded space", "https://a .trycloudflare.com"),
        # Shape
        ("empty label", "https://.trycloudflare.com"),
        ("label starting with a hyphen", "https://-calm.trycloudflare.com"),
        ("label ending with a hyphen", "https://calm-.trycloudflare.com"),
        ("overlong", "https://" + "a" * 300 + ".trycloudflare.com"),
        ("empty", ""),
        ("whitespace", "   "),
    ],
)
def test_a_hostile_or_malformed_candidate_is_refused(label: str, candidate: str):
    with pytest.raises(tunnel.TunnelError):
        validate(candidate)


@pytest.mark.parametrize("candidate", [None, 42, b"https://a.trycloudflare.com", ["x"], {}])
def test_a_candidate_that_is_not_a_string_is_refused(candidate):
    with pytest.raises(tunnel.TunnelError):
        validate(candidate)


def test_the_refusal_reason_does_not_quote_the_candidate():
    """Reasons are logged; a refused candidate is somebody else's text."""
    with pytest.raises(tunnel.TunnelError) as caught:
        validate("https://evil.test/steal?token=abcdef")

    assert "evil.test" not in caught.value.reason
    assert "abcdef" not in caught.value.reason


# ── Finding one in a line of output ───────────────────────────────────────


def test_the_url_is_found_inside_the_box_cloudflared_draws():
    line = "|  https://calm-forest-1234.trycloudflare.com                        |"

    assert tunnel.url_in(line) == VALID


def test_a_line_with_no_url_yields_nothing():
    assert tunnel.url_in("INF Requesting new quick Tunnel on trycloudflare.com...") is None


def test_a_line_whose_only_url_is_unusable_yields_nothing():
    assert tunnel.url_in("see https://evil.test/ for details") is None


def test_a_line_offering_two_different_valid_urls_is_refused():
    """There is no principled way to choose, and choosing wrong misroutes a learner."""
    line = "https://one.trycloudflare.com or https://two.trycloudflare.com"

    with pytest.raises(tunnel.TunnelError):
        tunnel.url_in(line)


def test_a_line_repeating_one_url_is_not_a_conflict():
    line = f"{VALID} ... {VALID}"

    assert tunnel.url_in(line) == VALID


# ── The command and the environment ───────────────────────────────────────


def test_the_command_is_an_argument_array_pointed_at_the_local_server():
    argv = tunnel.command("/usr/bin/cloudflared", "http://127.0.0.1:45678")

    assert argv == [
        "/usr/bin/cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://127.0.0.1:45678",
    ]


def test_the_target_is_built_from_the_runtime_own_address():
    assert tunnel.loopback_target("127.0.0.1", 45678) == "http://127.0.0.1:45678"
    assert tunnel.loopback_target("::1", 45678) == "http://[::1]:45678"


def test_the_tunnel_carries_no_credential_and_no_profile():
    child = tunnel.child_environment(
        {
            "TELEGRAM_BOT_TOKEN": "123:secret",
            "HERMES_HOME": "/profiles/family",
            "TELEGRAM_ALLOWED_USERS": "1001",
            "HOME": "/home/someone",
            "AWS_SECRET_ACCESS_KEY": "nope",
        }
    )

    assert child == {"HOME": "/home/someone"}
    assert "TELEGRAM_BOT_TOKEN" not in child
    assert "HERMES_HOME" not in child


def test_the_tunnel_environment_is_narrower_than_the_runtime_one():
    assert set(env.TUNNEL_INHERITED) < set(env.INHERITED)


# ── Starting one ──────────────────────────────────────────────────────────


class FakeStream:
    def __init__(self, lines: list[bytes], *, raises: Exception | None = None) -> None:
        self.lines = list(lines)
        self.raises = raises

    async def readline(self) -> bytes:
        if self.raises:
            raise self.raises
        return self.lines.pop(0) if self.lines else b""


class FakeProcess:
    def __init__(self, lines: list[bytes], **kwargs) -> None:
        self.stdout = FakeStream(lines, **kwargs)
        self.returncode = None
        self.terminated = False
        self._gone = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._gone.set()

    def kill(self) -> None:
        self.returncode = -9
        self._gone.set()

    async def wait(self) -> int:
        # `aclose` waits for the process to actually be gone, so a fake that
        # never finishes waiting would make every teardown time out.
        await self._gone.wait()
        return self.returncode


class Spawner:
    def __init__(self, process=None, *, raises: Exception | None = None) -> None:
        self.process = process
        self.raises = raises
        self.argv: list[str] | None = None
        self.environment: dict[str, str] | None = None

    async def __call__(self, argv, environment):
        self.argv = list(argv)
        self.environment = dict(environment)
        if self.raises:
            raise self.raises
        return self.process


def open_it(spawner, *, timeout=5.0, executable="/usr/bin/cloudflared"):
    return asyncio.run(
        tunnel.open_tunnel(
            executable=executable,
            target="http://127.0.0.1:45678",
            environment={"HOME": "/home/someone"},
            timeout_seconds=timeout,
            spawn=spawner,
        )
    )


def test_a_tunnel_that_publishes_a_valid_url_becomes_ready():
    spawner = Spawner(FakeProcess([b"INF starting\n", f"|  {VALID}  |\n".encode()]))

    opened = open_it(spawner)

    assert opened.state == "ready"
    assert opened.url == VALID
    assert spawner.argv[0] == "/usr/bin/cloudflared"


def test_a_missing_executable_fails_without_starting_anything():
    spawner = Spawner(FakeProcess([]))

    opened = open_it(spawner, executable="")

    assert opened.state == "failed"
    assert opened.reason == "tunnel_executable_absent"
    assert spawner.argv is None


def test_a_binary_that_will_not_start_fails_safely():
    opened = open_it(Spawner(raises=OSError("no such file")))

    assert opened.state == "failed"
    assert opened.reason == "tunnel_spawn_failed"


def test_a_process_that_exits_without_a_url_fails():
    opened = open_it(Spawner(FakeProcess([b"ERR could not reach the edge\n"])))

    assert opened.state == "failed"
    assert opened.reason == "tunnel_no_url"


def test_a_process_that_never_speaks_times_out_and_is_stopped():
    process = FakeProcess([])
    process.stdout = _SilentStream()
    spawner = Spawner(process)

    opened = open_it(spawner, timeout=0.05)

    assert opened.state == "failed"
    assert opened.reason == "tunnel_readiness_timeout"
    assert process.terminated is True


class _SilentStream:
    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""  # pragma: no cover


def test_output_that_never_ends_is_refused():
    lines = [b"INF chatter\n"] * (tunnel.MAX_OUTPUT_LINES + 5)
    process = FakeProcess(lines)

    opened = open_it(Spawner(process))

    assert opened.state == "failed"
    assert opened.reason == "tunnel_output_too_many_lines"
    assert process.terminated is True


def test_output_larger_than_the_byte_budget_is_refused():
    line = b"x" * 4096 + b"\n"
    process = FakeProcess([line] * (tunnel.MAX_OUTPUT_BYTES // 4096 + 2))

    opened = open_it(Spawner(process))

    assert opened.state == "failed"
    assert opened.reason == "tunnel_output_too_large"


def test_a_single_enormous_line_is_refused():
    process = FakeProcess([], raises=ValueError("line too long"))

    opened = open_it(Spawner(process))

    assert opened.state == "failed"
    assert opened.reason == "tunnel_output_line_too_long"


def test_a_process_printing_two_different_tunnels_is_refused():
    process = FakeProcess([b"https://one.trycloudflare.com https://two.trycloudflare.com\n"])

    opened = open_it(Spawner(process))

    assert opened.state == "failed"
    assert opened.reason == "tunnel_url_conflicting"
    assert process.terminated is True


def test_a_look_alike_in_the_output_never_becomes_the_url():
    process = FakeProcess([b"|  https://calm-forest.trycloudflare.com.evil.test  |\n"])

    opened = open_it(Spawner(process))

    assert opened.state == "failed"
    assert opened.url == ""


def test_a_failed_tunnel_reports_no_url_and_is_not_ready():
    opened = open_it(Spawner(FakeProcess([b"nothing useful\n"])))

    assert opened.ready is False
    assert opened.describe() == {"tunnel_state": "failed", "tunnel_ready": False}


def test_stopping_is_idempotent_and_touches_only_its_own_child():
    process = FakeProcess([f"{VALID}\n".encode()])
    opened = open_it(Spawner(process))

    opened.stop()
    opened.stop()

    assert process.terminated is True
    assert opened.state == "stopped"


def test_the_safe_description_never_carries_the_url():
    opened = open_it(Spawner(FakeProcess([f"{VALID}\n".encode()])))

    assert VALID not in str(opened.describe())


# ── Nothing here downloads or installs anything ───────────────────────────


def test_the_module_never_names_a_downloader_or_a_package_manager():
    from pathlib import Path

    source = Path(tunnel.__file__).read_text(encoding="utf-8")

    for forbidden in ("curl", "wget", "brew install", "apt-get", "urlretrieve", "download"):
        assert forbidden not in source.lower().replace("never downloads", ""), forbidden


def test_the_tunnel_is_started_without_a_shell():
    """Parsed rather than grepped: the docstring names the call it avoids."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(tunnel.__file__).read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                assert keyword.arg != "shell" or keyword.value.value is False

    assert "create_subprocess_exec" in called
    assert "create_subprocess_shell" not in called
    assert "system" not in called and "popen" not in called
