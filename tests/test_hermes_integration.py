"""Integration tests against a real Hermes checkout.

Hermes is not on PyPI and is not a dependency of this repository, so these
tests are opt-in: point ``HERMES_AGENT_SRC`` at a clone of
``NousResearch/hermes-agent`` and they run against the host's *actual* skill
machinery instead of this repo's fake context.

    HERMES_AGENT_SRC=/path/to/hermes-agent uv run pytest tests/test_hermes_integration.py

They exist because the reference-loading mechanism is a claim about someone
else's code. The unit tests can only check that SKILL.md *says* the right
thing; these check that what it says is true of Hermes as it actually is.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
from pathlib import Path

import pytest

HERMES_SRC_ENV = "HERMES_AGENT_SRC"


@pytest.fixture(autouse=True)
def _restore_process_state():
    """Undo everything importing Hermes does to this process.

    These tests deliberately put a Hermes checkout on ``sys.path`` and import
    real host modules. Left in place, those imports change the behaviour of
    every later test in the session: ``learning_studio.paths`` starts
    resolving through the real ``hermes_constants``, host code scaffolds
    profile files such as ``SOUL.md`` into the temporary home, and the
    filesystem-isolation test then fails for a reason that has nothing to do
    with this plugin.

    The isolation assertion is right and stays as it is; the pollution is the
    bug. So the snapshot below is thorough on purpose: module table, import
    path, and the host's own tool registry.
    """
    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)

    yield

    for name in [n for n in sys.modules if n not in saved_modules]:
        del sys.modules[name]
    sys.modules.update(saved_modules)
    sys.path[:] = saved_path

    # The real registry is a process-global singleton; tools registered by a
    # test would otherwise still be there for the next one.
    registry = saved_modules.get("tools.registry")
    if registry is not None:  # pragma: no cover - only when the host was loaded
        try:
            from learning_studio import TOOLSET_NAME

            for name, entry in list(registry.registry._tools.items()):
                if entry.toolset == TOOLSET_NAME:
                    del registry.registry._tools[name]
        except Exception:
            pass


def _hermes_src() -> Path:
    raw = os.environ.get(HERMES_SRC_ENV)
    if not raw:
        pytest.skip(f"set {HERMES_SRC_ENV} to a hermes-agent checkout to run integration tests")
    path = Path(raw).expanduser()
    if not path.is_dir():
        pytest.skip(f"{HERMES_SRC_ENV}={path} is not a directory")
    return path


def _load_module(name: str, path: Path, src: Path):
    """Import a single Hermes source file without installing the package.

    Hermes modules import their siblings absolutely (``hermes_cli.…``), so the
    checkout root goes on ``sys.path`` first. A checkout whose own third-party
    dependencies are absent skips rather than fails — that is an environment
    gap, not a defect in this plugin.
    """
    if not path.is_file():
        pytest.skip(f"not found in this Hermes checkout: {path}")

    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    if name in sys.modules:
        return sys.modules[name]

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because ``@dataclass`` resolves annotations
    # via ``sys.modules[cls.__module__]``; an unregistered module makes that
    # lookup return None and the class definition fails.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        del sys.modules[name]
        pytest.skip(f"Hermes checkout is missing its own dependencies: {exc}")
    except Exception:
        del sys.modules[name]
        raise
    return module


def _load_session_context(src: Path):
    """Import ``gateway.session_context`` under its real dotted name.

    It has to be the real name, not an alias: ``learning_studio.identity``
    does ``from gateway.session_context import get_session_env``, so a copy
    loaded under another name would hold different ContextVars and the test
    would bind identity somewhere the plugin never looks.

    A synthetic parent package is installed first so importing the submodule
    does not execute ``gateway/__init__.py``, which pulls in third-party
    dependencies a bare checkout does not have.
    """
    import types

    if "gateway.session_context" in sys.modules:
        return sys.modules["gateway.session_context"]

    if "gateway" not in sys.modules:
        parent = types.ModuleType("gateway")
        parent.__path__ = [str(src / "gateway")]
        sys.modules["gateway"] = parent

    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return importlib.import_module("gateway.session_context")


@pytest.fixture
def skill_preprocessing():
    src = _hermes_src()
    return _load_module(
        "_hermes_skill_preprocessing", src / "agent" / "skill_preprocessing.py", src
    )


# ── The mechanism SKILL.md tells the agent to use actually works ───────────


def test_skill_dir_token_expands_to_the_real_skill_directory(skill_preprocessing, skill_dir: Path):
    """``${HERMES_SKILL_DIR}`` must resolve, or every reference path is dead."""
    rendered = skill_preprocessing.substitute_template_vars(
        'read_file("${HERMES_SKILL_DIR}/references/selection-cards.md")',
        skill_dir,
        None,
    )

    assert "${HERMES_SKILL_DIR}" not in rendered, "token was not substituted"
    assert str(skill_dir) in rendered


def test_every_reference_resolves_after_substitution(
    skill_preprocessing, skill_dir: Path, skill_md: str
):
    """Expand the token the way Hermes does, then open every path we advertise."""
    rendered = skill_preprocessing.substitute_template_vars(skill_md, skill_dir, None)

    advertised = re.findall(r'read_file\("([^"]+)"\)', rendered)
    assert advertised, "SKILL.md advertises no read_file path"

    for target in advertised:
        assert Path(target).is_file(), f"advertised path does not resolve: {target}"


def test_template_substitution_is_on_by_default(skill_preprocessing):
    """If ``template_vars`` defaulted off, the primary idiom would ship broken."""
    source = inspect.getsource(skill_preprocessing.preprocess_skill_content)

    assert 'get("template_vars", True)' in source, (
        "Hermes no longer enables template_vars by default — SKILL.md's "
        "fallback instructions become the primary path"
    )


# ── The mechanism SKILL.md warns against is still broken ───────────────────


def test_serve_plugin_skill_still_ignores_file_path():
    """The reason we use read_file rather than skill_view(name, file_path).

    ``skill_view`` routes qualified ``plugin:skill`` names to
    ``_serve_plugin_skill()``, which has no ``file_path`` parameter — so the
    argument is silently dropped and SKILL.md is returned with
    ``success: True``. If Hermes ever adds it, this test fails and SKILL.md's
    warning can be simplified.
    """
    src = _hermes_src()
    skills_tool = (src / "tools" / "skills_tool.py").read_text(encoding="utf-8")

    signature = re.search(r"def _serve_plugin_skill\((.*?)\) -> str:", skills_tool, re.S)
    assert signature, "could not find _serve_plugin_skill in this Hermes checkout"

    assert "file_path" not in signature.group(1), (
        "_serve_plugin_skill now accepts file_path — Hermes may have fixed "
        "plugin reference loading; re-evaluate the read_file workaround"
    )


def test_plugin_dispatch_returns_before_file_path_is_handled():
    """Confirms the drop is structural, not just a missing parameter name."""
    src = _hermes_src()
    skills_tool = (src / "tools" / "skills_tool.py").read_text(encoding="utf-8")

    dispatch = skills_tool.index("return _serve_plugin_skill(")
    handling = skills_tool.index("if file_path and skill_dir:")

    assert dispatch < handling, (
        "file_path handling now precedes the plugin dispatch — re-evaluate "
        "whether skill_view can open plugin references directly"
    )


# ── The runtime APIs this plugin depends on ────────────────────────────────


def test_get_hermes_home_exists_and_is_profile_aware(monkeypatch, tmp_path: Path):
    """``paths.hermes_home()`` delegates to this; a rename would break isolation."""
    src = _hermes_src()
    constants = _load_module("_hermes_constants", src / "hermes_constants.py", src)

    assert hasattr(constants, "get_hermes_home"), (
        "hermes_constants.get_hermes_home is gone — learning_studio.paths must be updated"
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-x"))
    assert Path(constants.get_hermes_home()) == tmp_path / "profile-x"


def test_the_plugin_resolves_storage_under_the_real_hermes_home(monkeypatch, tmp_path: Path):
    """End to end: the host's resolver, through this plugin's path helper."""
    src = _hermes_src()
    _load_module("_hermes_constants", src / "hermes_constants.py", src)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-y"))
    from learning_studio.paths import storage_root

    assert storage_root() == tmp_path / "profile-y" / "workspace" / "learning-studio"


def test_register_tool_signature_matches_what_the_plugin_calls():
    """The parameter order is easy to get wrong and silent when wrong.

    The host's signature is ``(name, toolset, schema, handler, ...)``. Public
    documentation has shown ``(name, schema, handler, toolset="")``, which
    would bind a schema dict to ``toolset``. This plugin passes keywords
    only, so what matters is that every keyword it passes still exists.
    """
    src = _hermes_src()
    plugins_source = (src / "hermes_cli" / "plugins.py").read_text(encoding="utf-8")

    signature = re.search(r"def register_tool\((.*?)\) -> None:", plugins_source, re.S)
    assert signature, "could not find PluginContext.register_tool in this Hermes checkout"

    params = signature.group(1)
    for keyword in ("name", "toolset", "schema", "handler", "description"):
        assert f"{keyword}:" in params, (
            f"PluginContext.register_tool no longer accepts '{keyword}' — "
            "learning_studio.plugin.register must be updated"
        )


def test_the_toolset_name_does_not_collide_with_a_builtin_toolset():
    """A collision would make the registry reject our tools at startup."""
    src = _hermes_src()
    registry_source = (src / "tools" / "registry.py").read_text(encoding="utf-8")

    from learning_studio import TOOLSET_NAME

    assert f'toolset="{TOOLSET_NAME}"' not in registry_source


def test_the_tool_names_do_not_shadow_a_builtin_tool():
    """Shadowing a built-in is rejected without an operator opt-in we do not have."""
    src = _hermes_src()
    from learning_studio.schemas import TOOL_SCHEMAS

    builtin_names: set[str] = set()
    for path in (src / "tools").glob("*.py"):
        builtin_names.update(
            re.findall(
                r'registry\.register\(\s*name=["\']([a-z0-9_]+)["\']',
                path.read_text(encoding="utf-8", errors="ignore"),
            )
        )

    collisions = sorted(set(TOOL_SCHEMAS) & builtin_names)
    assert collisions == [], f"tool names collide with Hermes built-ins: {collisions}"


def test_the_plugin_registers_and_runs_through_the_real_plugin_context(monkeypatch, tmp_path: Path):
    """Register through the host's own PluginContext, then call the tools.

    The fake context mirrors the host, but only the host proves the plugin
    actually loads. This drives the real ``PluginContext`` and the real tool
    registry, in an isolated HERMES_HOME, with identity supplied the way the
    gateway supplies it.
    """
    import json

    src = _hermes_src()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "live-profile"))

    plugins = _load_module("_hermes_plugins", src / "hermes_cli" / "plugins.py", src)

    # Not via _load_module: PluginContext.register_tool does
    # ``from tools.registry import registry``, so the assertions have to read
    # that exact module object. Loading a second copy under an alias would
    # inspect a registry nothing ever wrote to.
    registry_module = importlib.import_module("tools.registry")

    manifest = plugins.PluginManifest(name="learning-studio", version="0.1.0")
    manager = plugins.PluginManager()
    ctx = plugins.PluginContext(manifest, manager)

    from learning_studio import TOOLSET_NAME, register

    register(ctx)

    registry = registry_module.registry
    registered = {
        name: entry for name, entry in registry._tools.items() if entry.toolset == TOOLSET_NAME
    }
    assert sorted(registered) == [
        "learning_studio_get_context",
        "learning_studio_save_context",
    ]

    session_context = _load_session_context(src)

    def as_user(user_id: str):
        """Bind a platform identity exactly as the gateway does per message."""
        return session_context.set_session_vars(
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            user_id=user_id,
            user_name="ignored-label",
            session_key=f"telegram:{user_id}",
        )

    # ── First authenticated learner ──────────────────────────────────────
    tokens = as_user("111111")
    try:
        saved = json.loads(
            registry.dispatch(
                "learning_studio_save_context",
                {"track": {"name": "Live track", "confirmed": True, "context": {"goal": "g"}}},
            )
        )
        assert saved["ok"] is True, saved
        assert saved["outcome"]["track"]["status"] == "created"
        assert saved["hermes_memory_updated"] is False

        fetched = json.loads(registry.dispatch("learning_studio_get_context", {}))
        assert fetched["confirmed_context"]["goal"]["value"] == "g"
    finally:
        session_context.clear_session_vars(tokens)

    # ── Second authenticated learner, same profile ───────────────────────
    tokens = as_user("222222")
    try:
        other = json.loads(registry.dispatch("learning_studio_get_context", {}))
        assert other["tracks"] == [], "a second principal saw the first principal's track"

        # And cannot reach it by naming the first learner in arguments.
        impersonation = json.loads(
            registry.dispatch("learning_studio_get_context", {"learner_key": "111111"})
        )
        assert impersonation["ok"] is False
    finally:
        session_context.clear_session_vars(tokens)

    db = tmp_path / "live-profile" / "workspace" / "learning-studio" / "learning-studio.sqlite3"
    assert db.is_file(), "the real run did not write to the profile-scoped storage root"


def test_the_session_user_id_is_a_real_host_supplied_value():
    """The identity this plugin trusts must come from the platform payload.

    If Hermes ever stopped binding ``user_id`` from the message source, this
    plugin's isolation guarantee would quietly become a guess.
    """
    src = _hermes_src()
    run_source = (src / "gateway" / "run.py").read_text(encoding="utf-8")

    assert "user_id=str(context.source.user_id)" in run_source, (
        "the gateway no longer binds user_id from the message source — "
        "learning_studio.identity must be re-verified"
    )


def test_session_context_exposes_the_variables_identity_depends_on():
    src = _hermes_src()
    session_context = _load_session_context(src)

    assert hasattr(session_context, "get_session_env")
    assert hasattr(session_context, "session_context_engaged")
    for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_USER_ID"):
        assert name in session_context._VAR_MAP, f"{name} is no longer a session variable"
