"""The plugin-local runtime environment: repeatable, bounded, and nobody else's.

Every test here runs against an injected process runner. Building a real
virtual environment would install packages from a network index into a
developer's machine as a side effect of running the suite, which is precisely
the behaviour this module exists to keep away from Hermes' own environment.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from learning_studio.runtime import bootstrap


class FakeRunner:
    """Records the argument arrays it is given, and answers as told.

    ``creates_venv`` reproduces the one visible effect of ``python -m venv``:
    an interpreter appears at the expected path. Without it the tests could not
    tell "the command ran" from "the command worked", which is the distinction
    the stamp logic turns on.
    """

    def __init__(self, *results, creates_venv: bool = False, stderr: str = "boom\n") -> None:
        self.results = list(results) or [0, 0]
        self.creates_venv = creates_venv
        self.stderr = stderr
        self.commands: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.kwargs.append(kwargs)
        code = self.results.pop(0) if self.results else 0
        if self.creates_venv and "venv" in command and code == 0:
            python = bootstrap.runtime_python()
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
        return subprocess.CompletedProcess(
            command, code, stdout="installed\n", stderr="" if code == 0 else self.stderr
        )


def creating(*results: int) -> FakeRunner:
    """A runner that succeeds and leaves a venv layout behind."""
    return FakeRunner(*(results or (0, 0)), creates_venv=True)


def run_bootstrap(hermes_home: Path, runner=None, **kwargs) -> bootstrap.BootstrapResult:
    return bootstrap.bootstrap(runner=runner or creating(), **kwargs)


# ── Success and repeatability ─────────────────────────────────────────────


def test_bootstrapping_creates_the_environment(hermes_home: Path):
    result = run_bootstrap(hermes_home)

    assert result.outcome == "created"
    assert bootstrap.is_bootstrapped() is True


def test_a_second_bootstrap_does_nothing(hermes_home: Path):
    run_bootstrap(hermes_home)
    runner = FakeRunner()

    result = bootstrap.bootstrap(runner=runner)

    assert result.outcome == "current"
    assert runner.commands == [], "an up-to-date environment was rebuilt"


def test_forcing_rebuilds_even_when_current(hermes_home: Path):
    run_bootstrap(hermes_home)

    result = run_bootstrap(hermes_home, force=True)

    assert result.outcome == "created"


def test_changed_requirements_invalidate_the_stamp(hermes_home: Path, monkeypatch):
    run_bootstrap(hermes_home)
    monkeypatch.setattr(bootstrap, "requirements_digest", lambda: "a-different-digest")

    assert bootstrap.is_bootstrapped() is False


def test_a_stamp_without_an_interpreter_is_not_a_bootstrap(hermes_home: Path):
    run_bootstrap(hermes_home)
    bootstrap.runtime_python().unlink()

    assert bootstrap.is_bootstrapped() is False


def test_an_interpreter_without_a_stamp_is_not_a_bootstrap(hermes_home: Path):
    run_bootstrap(hermes_home)
    bootstrap.stamp_path().unlink()

    assert bootstrap.is_bootstrapped() is False


def test_a_corrupt_stamp_is_not_a_bootstrap(hermes_home: Path):
    run_bootstrap(hermes_home)
    bootstrap.stamp_path().write_text("{not json", encoding="utf-8")

    assert bootstrap.is_bootstrapped() is False


def test_removing_the_environment_is_idempotent(hermes_home: Path):
    run_bootstrap(hermes_home)

    bootstrap.remove()
    bootstrap.remove()

    assert bootstrap.is_bootstrapped() is False


# ── Failure ───────────────────────────────────────────────────────────────


def test_a_failed_creation_reports_and_writes_no_stamp(hermes_home: Path):
    result = bootstrap.bootstrap(runner=FakeRunner(1))

    assert result.outcome == "failed"
    assert not bootstrap.stamp_path().exists()
    assert bootstrap.is_bootstrapped() is False


def test_a_failed_install_reports_and_writes_no_stamp(hermes_home: Path):
    result = bootstrap.bootstrap(runner=creating(0, 1))

    assert result.outcome == "failed"
    assert not bootstrap.stamp_path().exists()


def test_failure_detail_is_bounded(hermes_home: Path):
    result = bootstrap.bootstrap(runner=FakeRunner(1, stderr="x" * 100_000))

    assert len(result.detail) <= bootstrap.MAX_DETAIL_CHARS


# ── What it may and may not do ────────────────────────────────────────────


def test_every_command_is_an_argument_array_and_never_a_shell(hermes_home: Path):
    runner = creating()

    bootstrap.bootstrap(runner=runner)

    assert runner.commands, "no command was run at all"
    for command in runner.commands:
        assert isinstance(command, list)
        assert all(isinstance(part, str) for part in command)
    for kwargs in runner.kwargs:
        assert kwargs.get("shell") in (None, False)


def test_every_command_is_bounded_in_time(hermes_home: Path):
    runner = creating()

    bootstrap.bootstrap(runner=runner)

    for kwargs in runner.kwargs:
        assert isinstance(kwargs.get("timeout"), int)
        assert kwargs["timeout"] > 0


def test_the_environment_is_built_from_the_running_interpreter(hermes_home: Path):
    """And by ``venv``: nothing here downloads or installs an interpreter."""
    runner = creating()

    bootstrap.bootstrap(runner=runner)

    assert runner.commands[0][:3] == [sys.executable, "-m", "venv"]


def test_the_install_targets_the_new_environment_and_requires_one(hermes_home: Path):
    runner = creating()

    bootstrap.bootstrap(runner=runner)

    install = runner.commands[1]
    assert install[0] == str(bootstrap.runtime_python())
    assert install[1:4] == ["-m", "pip", "install"]
    assert "--require-virtualenv" in install, "pip could have installed into the host environment"


def test_nothing_installs_an_operating_system_package_or_asks_for_privilege(hermes_home: Path):
    runner = creating()

    bootstrap.bootstrap(runner=runner)

    forbidden = ("sudo", "su", "apt", "apt-get", "brew", "yum", "dnf", "pacman", "apk", "curl")
    for command in runner.commands:
        assert command[0].rsplit("/", 1)[-1] not in forbidden, command


def test_nothing_downloads_cloudflared(hermes_home: Path):
    """The tunnel binary is an operator prerequisite, never a download."""
    runner = creating()

    bootstrap.bootstrap(runner=runner)

    assert "cloudflared" not in json.dumps(runner.commands)


def test_the_environment_lives_under_this_profile_only(hermes_home: Path):
    assert bootstrap.venv_dir().is_relative_to(hermes_home)
    assert bootstrap.runtime_python().is_relative_to(hermes_home)


def test_two_profiles_get_two_environments(tmp_path: Path, monkeypatch):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(first))
    one = bootstrap.venv_dir()
    monkeypatch.setenv("HERMES_HOME", str(second))
    other = bootstrap.venv_dir()

    assert one != other


# ── The pinned requirements ───────────────────────────────────────────────


def test_the_requirements_are_shipped_inside_the_package():
    assert bootstrap.REQUIREMENTS.is_file()
    assert bootstrap.REQUIREMENTS.is_relative_to(Path(bootstrap.__file__).parent)


def test_the_direct_dependencies_are_pinned():
    lines = [
        line.strip()
        for line in bootstrap.REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert lines, "the requirements file declares nothing"
    for line in lines:
        assert "==" in line, f"{line} is not pinned"


def test_the_requirements_declare_only_what_the_runtime_serves_with():
    names = {
        line.split("==")[0].strip().lower()
        for line in bootstrap.REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    assert names == {"fastapi", "uvicorn"}


@pytest.mark.parametrize("forbidden", ["cloudflare", "requests", "httpx", "python-telegram-bot"])
def test_the_requirements_pull_in_no_tunnel_or_telegram_client(forbidden: str):
    assert forbidden not in bootstrap.REQUIREMENTS.read_text(encoding="utf-8")
