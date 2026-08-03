"""The plugin must never reach Hermes memory. Proved three ways.

SKILL.md promises the learner that only the agent decides what enters Hermes
memory. That promise is worth exactly as much as the code behind it, so this
module attacks it from three directions:

1. **Static** — no source file imports a memory module or names a memory file.
2. **Dynamic** — memory modules are made unimportable, then every code path
   (import, register, get, save, candidate generation) is exercised.
3. **Indirect** — no subprocess, MCP, or tool-dispatch escape hatch exists
   that could reach memory without importing it.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "learning_studio"

#: Module roots that are, or lead to, Hermes memory.
MEMORY_MODULES = (
    "memory",
    "memory_tool",
    "memory_provider",
    "memory_store",
    "hermes_memory",
)

#: Files a memory write would touch.
MEMORY_FILES = ("MEMORY.md", "USER.md", "memory.db", "memories.db", "memory.json")

#: Modules that can run something outside this process. Memory must not be
#: reachable "through subprocesses, terminal commands, MCP, delegation, or
#: indirect wrappers" either, and an import check alone would not catch that.
ESCAPE_MODULES = ("subprocess", "multiprocessing", "pty", "popen2", "commands")

#: Call targets that spawn a process without importing one of the above.
ESCAPE_CALLS = ("system", "popen", "execv", "execve", "execvp", "spawnv", "fork")

#: The runtime feature starts processes, so "nothing here can start one" stopped
#: being true and is not the property to assert any more. The property that
#: still holds — and that these two files are held to below, one test each — is
#: that what they start is a closed set of programs, run without a shell, in an
#: environment that names no memory store.
#:
#: Their *own* source is scanned by every other test in this module, so a memory
#: import inside the supervisor would still fail ``test_no_source_imports_a_
#: memory_module``. What is exempted here is only "imports subprocess at all".
PROCESS_STARTERS = (
    "runtime/bootstrap.py",
    "runtime/supervisor.py",
    "runtime/tunnel.py",
)


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_the_package_has_sources_to_scan():
    """Guard against the scans below passing because they found nothing."""
    assert len(_sources()) >= 5


# ── Static ────────────────────────────────────────────────────────────────


def test_no_source_imports_a_memory_module():
    offenders: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                parts = name.split(".")
                if root in MEMORY_MODULES or any(p in MEMORY_MODULES for p in parts):
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert offenders == [], f"plugin imports Hermes memory: {offenders}"


def test_no_source_names_a_memory_file():
    offenders = [
        f"{path.name} mentions {needle}"
        for path in _sources()
        for needle in MEMORY_FILES
        if needle in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"plugin references memory storage files: {offenders}"


def test_no_source_can_shell_out():
    """No ad-hoc process escape, and none at all outside the two supervisors.

    Parsed rather than grepped: a substring search for ``pty`` matches the
    word "empty", and one for ``subprocess`` matches a comment promising not
    to use it. Only real imports and real calls count.

    ``os.system``, ``os.popen`` and the ``exec``/``fork`` family are refused
    everywhere, including in the two files that are allowed to start a process:
    those interfaces take a command *string* or replace this process, and
    neither is a thing this plugin ever needs.
    """
    offenders: list[str] = []
    for path in _sources():
        relative = path.relative_to(PACKAGE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr in ESCAPE_CALLS:
                        base = func.value
                        if isinstance(base, ast.Name) and base.id == "os":
                            offenders.append(f"{relative}:{node.lineno} calls os.{func.attr}")
                continue
            if relative in PROCESS_STARTERS:
                continue
            for name in names:
                if name.split(".")[0] in ESCAPE_MODULES:
                    offenders.append(f"{relative}:{node.lineno} imports {name}")
    assert offenders == [], f"plugin has a process-escape hatch: {offenders}"


def test_the_process_starters_run_a_closed_set_of_programs():
    """Whatever the runtime starts, it is not a route to the host's memory tool.

    The programs are: the interpreter that is running, the plugin's own
    virtual environment interpreter, the launcher file this package ships, and
    the operator's ``cloudflared``. None of them is Hermes, none of them is a
    shell, and none is named by anything a model can write.
    """
    from learning_studio.runtime import bootstrap, supervisor

    assert supervisor.LAUNCHER.name == "launch_server.py"
    assert supervisor.LAUNCHER.is_relative_to(PACKAGE)
    assert bootstrap.runtime_python().name == "python"

    source = (PACKAGE / "runtime" / "supervisor.py").read_text(encoding="utf-8")
    for host_program in ("hermes", "memory_tool", "sh", "bash", "zsh"):
        assert f'"{host_program}"' not in source


def test_nothing_this_plugin_starts_runs_through_a_shell():
    for name in PROCESS_STARTERS:
        source = (PACKAGE / name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "shell":
                    continue
                assert isinstance(keyword.value, ast.Constant), name
                assert keyword.value.value is False, f"{name} passes shell=True"


def test_the_runtime_environment_names_no_memory_store():
    """A child process cannot be pointed at a memory file it is never told about."""
    from learning_studio.runtime import environment

    declared = set(environment.OWN_VARIABLES) | set(environment.INHERITED)
    declared |= set(environment.TUNNEL_INHERITED)

    for name in declared:
        assert "MEMORY" not in name.upper(), name
    for needle in MEMORY_FILES:
        assert needle not in " ".join(sorted(declared))


def test_no_source_dispatches_another_tool():
    """``ctx.dispatch_tool`` would route straight to the host's memory tool."""
    offenders = [
        path.name for path in _sources() if "dispatch_tool" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"plugin dispatches host tools: {offenders}"


# ── Dynamic ───────────────────────────────────────────────────────────────


class _MemoryBlocker:
    """Makes every memory module unimportable, and records any attempt."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in MEMORY_MODULES or any(part in MEMORY_MODULES for part in fullname.split(".")):
            self.attempts.append(fullname)
            raise ImportError(f"{fullname} is blocked: the plugin must not touch Hermes memory")
        return None


@pytest.fixture
def memory_blocked():
    blocker = _MemoryBlocker()
    saved = {name: mod for name, mod in sys.modules.items() if name.startswith("learning_studio")}
    for name in saved:
        del sys.modules[name]
    sys.meta_path.insert(0, blocker)
    try:
        yield blocker
    finally:
        sys.meta_path.remove(blocker)
        for name in [n for n in sys.modules if n.startswith("learning_studio")]:
            del sys.modules[name]
        sys.modules.update(saved)


def test_import_does_not_touch_memory(memory_blocked):
    importlib.import_module("learning_studio")

    assert memory_blocked.attempts == []


def test_registration_does_not_touch_memory(memory_blocked):
    from tests.fake_hermes import FakePluginContext

    module = importlib.import_module("learning_studio")
    module.register(FakePluginContext(plugin_name="learning-studio"))

    assert memory_blocked.attempts == []


def test_the_whole_write_and_read_cycle_does_not_touch_memory(memory_blocked, hermes_home: Path):
    """Registration, save, candidate generation, and read — the full path."""
    service = importlib.import_module("learning_studio.service")
    identity = importlib.import_module("learning_studio.identity")
    who = identity.Principal(
        profile="default", platform="telegram", user_id="5005", source="gateway_session"
    )

    service.save_context(
        principal=who,
        temporary_context={"subject": "anything"},
        track={"name": "T", "confirmed": True, "context": {"goal": "g"}},
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Prefers worked examples first.",
                "evidence_summary": "Said so directly.",
                "origin": "explicit_durable_preference",
            }
        ],
    )
    service.get_context(principal=who, include_memory_candidates=True)

    assert memory_blocked.attempts == []


def test_the_blocker_would_catch_a_real_import(memory_blocked):
    """Proves the fixture discriminates rather than passing vacuously."""
    with pytest.raises(ImportError):
        importlib.import_module("memory")

    assert memory_blocked.attempts == ["memory"]


# ── The plugin writes nothing outside its own storage ─────────────────────


def test_saving_writes_only_inside_the_learning_studio_directory(hermes_home: Path):
    from learning_studio import service
    from learning_studio.identity import Principal

    service.save_context(
        principal=Principal(
            profile="default", platform="telegram", user_id="6006", source="gateway_session"
        ),
        track={"name": "T", "confirmed": True, "context": {"goal": "g"}},
    )

    written = {p for p in hermes_home.rglob("*") if p.is_file()}
    studio = hermes_home / "workspace" / "learning-studio"
    outside = sorted(str(p.relative_to(hermes_home)) for p in written if studio not in p.parents)
    assert outside == [], f"plugin wrote outside its storage root: {outside}"
