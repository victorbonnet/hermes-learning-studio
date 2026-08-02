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

#: This repository's fixtures, loaded under a name nothing can shadow.
#:
#: ``from tests.component_examples import ...`` cannot be used here. Hermes
#: ships its own top-level ``tests`` package, and these tests put the Hermes
#: checkout on ``sys.path`` — so after the first Hermes module is loaded,
#: ``tests`` may resolve to *theirs*, and the import fails with
#: ``ModuleNotFoundError: No module named 'tests.component_examples'``. The
#: failure depends on what else has been imported first, which makes it a test
#: that passes alone and fails in the suite. Loading the file by path under a
#: private name removes the ambiguity entirely.
_FIXTURES_MODULE = "_learning_studio_component_examples"


def fixtures():
    """The component examples, imported without going through ``tests``."""
    if _FIXTURES_MODULE in sys.modules:
        return sys.modules[_FIXTURES_MODULE]

    path = Path(__file__).resolve().parent / "component_examples.py"
    spec = importlib.util.spec_from_file_location(_FIXTURES_MODULE, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_FIXTURES_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # pragma: no cover - a broken fixture file
        del sys.modules[_FIXTURES_MODULE]
        raise
    return module


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


# ── Fixture loading survives Hermes shadowing the `tests` package ──────────


def test_fixtures_load_without_going_through_the_tests_package():
    """Runs without a Hermes checkout, because the collision does not need one.

    A ``tests`` package that does not contain ``component_examples`` is put
    where an import would find it — which is exactly what a Hermes checkout on
    ``sys.path`` does. The path-based loader must be unaffected.
    """
    import types

    shadow = types.ModuleType("tests")
    shadow.__path__ = []  # a package with no modules in it
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "tests" or name.startswith("tests.")
    }
    # The cached copy has to go too, or the import below would be answered
    # from ``sys.modules`` and the shadowing would never be exercised.
    for name in saved:
        del sys.modules[name]
    sys.modules["tests"] = shadow
    sys.modules.pop(_FIXTURES_MODULE, None)
    try:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tests.component_examples")

        module = fixtures()

        assert module.CANARY
        assert module.manifest()["schema_version"] == 1
    finally:
        sys.modules.pop(_FIXTURES_MODULE, None)
        sys.modules.pop("tests", None)
        sys.modules.update(saved)


def test_fixtures_load_with_a_foreign_tests_package_on_the_path(tmp_path: Path):
    """The real shape of the collision, without needing a Hermes checkout.

    A directory holding its own ``tests`` package is put at the front of
    ``sys.path``, which is exactly what ``_load_module`` does with the Hermes
    checkout. An import of ``tests.component_examples`` then resolves into
    *that* package and fails; the path-based loader is unaffected.
    """
    foreign = tmp_path / "hermes-checkout"
    (foreign / "tests").mkdir(parents=True)
    (foreign / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (foreign / "tests" / "test_gateway.py").write_text("", encoding="utf-8")

    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "tests" or name.startswith("tests.")
    }
    saved_path = list(sys.path)
    for name in saved_modules:
        del sys.modules[name]
    sys.path.insert(0, str(foreign))
    sys.modules.pop(_FIXTURES_MODULE, None)
    try:
        shadowed = importlib.import_module("tests")
        assert Path(shadowed.__file__).parent == foreign / "tests", (
            "the foreign package did not take precedence, so this proves nothing"
        )
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tests.component_examples")

        assert fixtures().manifest()["schema_version"] == 1
    finally:
        sys.path[:] = saved_path
        sys.modules.pop(_FIXTURES_MODULE, None)
        for name in [n for n in sys.modules if n == "tests" or n.startswith("tests.")]:
            del sys.modules[name]
        sys.modules.update(saved_modules)


def test_the_fixture_loader_is_idempotent():
    """Called twice, it returns the same module rather than reloading it."""
    sys.modules.pop(_FIXTURES_MODULE, None)
    try:
        assert fixtures() is fixtures()
    finally:
        sys.modules.pop(_FIXTURES_MODULE, None)


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
        "learning_studio_import_asset",
        "learning_studio_launch",
        "learning_studio_prepare",
        "learning_studio_results",
        "learning_studio_save_context",
        "learning_studio_status",
        "learning_studio_stop",
    ]

    # The real registry stores `check_fn`, and gates the tool on it. Checked
    # here rather than only against the fake context, because "Hermes accepts a
    # check_fn" is a claim about somebody else's code.
    from learning_studio.plugin import RUNTIME_TOOLS

    gated = {name for name, entry in registered.items() if entry.check_fn is not None}
    assert gated == set(RUNTIME_TOOLS)

    session_context = _load_session_context(src)

    accepted = set(inspect.signature(session_context.set_session_vars).parameters)

    def as_user(user_id: str):
        """Bind a platform identity exactly as the gateway does per message.

        Only parameters the *installed* Hermes actually declares are passed.
        Hermes versions differ here — ``chat_type`` exists on current upstream
        main but not on every release — and hardcoding the full call makes
        this test fail with a ``TypeError`` about the host's signature rather
        than telling us anything about this plugin.
        """
        candidate = {
            "platform": "telegram",
            "chat_id": "chat-1",
            "user_id": user_id,
            "user_name": "ignored-label",
            "session_key": f"telegram:{user_id}",
        }
        return session_context.set_session_vars(
            **{k: v for k, v in candidate.items() if k in accepted}
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

        from PIL import Image

        source = tmp_path / "live-profile" / "cache" / "images" / "real-host.png"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (2, 2), "blue").save(source, format="PNG")
        imported = json.loads(
            registry.dispatch(
                "learning_studio_import_asset",
                {
                    "source_path": str(source),
                    "title": "Real host diagram",
                    "alt_text": "A blue square used by the real host integration test.",
                    "provenance": "operator_selected",
                },
            )
        )
        assert imported["ok"] is True, imported
        assert str(source) not in json.dumps(imported)

        examples = fixtures()

        prepared = json.loads(
            registry.dispatch("learning_studio_prepare", {"manifest": examples.manifest()})
        )
        assert prepared["ok"] is True, prepared
        assert prepared["stored"] is True
        first_experience = prepared["experience_id"]
        # The response travels back through the model, so it is the one place
        # an answer key must never appear.
        assert examples.CANARY not in json.dumps(prepared)
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

        # Nor reach the first learner's prepared exercise, by id or otherwise.
        from learning_studio import service
        from learning_studio.identity import resolve_principal

        try:
            service.get_experience(principal=resolve_principal(), experience_id=first_experience)
        except service.NotFoundError:
            pass
        else:  # pragma: no cover - a failure here is the point of the test
            raise AssertionError("a second principal read the first principal's exercise")

        # A prepared exercise of their own is a different record entirely.
        own = json.loads(
            registry.dispatch("learning_studio_prepare", {"manifest": fixtures().manifest()})
        )
        assert own["ok"] is True, own
        assert own["experience_id"] != first_experience
    finally:
        session_context.clear_session_vars(tokens)

    db = tmp_path / "live-profile" / "workspace" / "learning-studio" / "learning-studio.sqlite3"
    assert db.is_file(), "the real run did not write to the profile-scoped storage root"

    # The database the real host produced must be at the current schema, with
    # the two experiences the two principals prepared kept apart.
    import sqlite3

    with sqlite3.connect(db) as inspection:
        version = inspection.execute("SELECT version FROM schema_version").fetchone()[0]
        owners = inspection.execute(
            "SELECT COUNT(DISTINCT learner_id) FROM experiences"
        ).fetchone()[0]
        payloads = " ".join(
            str(row[0])
            for row in inspection.execute("SELECT learner_payload FROM experience_components")
        )
        asset_count = inspection.execute("SELECT COUNT(*) FROM managed_assets").fetchone()[0]
        asset_columns = {row[1] for row in inspection.execute("PRAGMA table_info(managed_assets)")}

    from learning_studio.storage import SCHEMA_VERSION

    assert version == SCHEMA_VERSION
    assert owners == 2, "the two principals' exercises were not stored separately"
    assert asset_count == 1
    assert "source_path" not in asset_columns
    assert fixtures().CANARY not in payloads, "evaluator-only data reached the learner-facing table"


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


# ── Identity binding through the real host ─────────────────────────────────


def _bind(session_context, **wanted):
    """Call the host's ``set_session_vars`` with only what it declares.

    Signatures differ between Hermes versions; passing an argument the
    installed host does not have raises ``TypeError`` and tells us nothing
    about this plugin.
    """
    accepted = set(inspect.signature(session_context.set_session_vars).parameters)
    return session_context.set_session_vars(**{k: v for k, v in wanted.items() if k in accepted})


def test_set_session_vars_still_accepts_what_this_plugin_needs():
    """The host must keep binding the two values identity depends on."""
    session_context = _load_session_context(_hermes_src())

    accepted = set(inspect.signature(session_context.set_session_vars).parameters)

    for required in ("platform", "user_id"):
        assert required in accepted, (
            f"gateway.session_context.set_session_vars no longer accepts {required!r} — "
            "learning_studio.identity must be re-verified against the host"
        )


def test_the_plugin_depends_only_on_session_variables_the_host_defines():
    """Guards against reading a name this Hermes does not expose.

    ``HERMES_SESSION_CHAT_TYPE`` is the cautionary case: it exists on current
    upstream main but not on every release, and depending on it made the
    plugin's behaviour vary by host version for no benefit.
    """
    session_context = _load_session_context(_hermes_src())

    import learning_studio.destination as destination
    import learning_studio.identity as identity

    read_names: set[str] = set()
    for module in (identity, destination):
        source = Path(module.__file__).read_text(encoding="utf-8")
        # The call site in `destination` passes a module constant rather than a
        # literal, so both forms are collected: the literal call, and the
        # constants that name a session variable.
        read_names |= set(re.findall(r'session_value\(\s*"([A-Z_]+)"', source))
        read_names |= set(re.findall(r'^[A-Z_]+ = "(HERMES_SESSION_[A-Z_]+)"', source, re.M))
    assert read_names, "no session variables are read — has identity resolution moved?"

    unsupported = sorted(read_names - set(session_context._VAR_MAP))
    assert unsupported == [], (
        f"the plugin reads session variables this Hermes does not define: {unsupported}"
    )


def test_identity_comes_from_the_real_session_binding(tmp_path, monkeypatch):
    """Bind through the real host, then read back through the real resolver."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ident-profile"))
    session_context = _load_session_context(_hermes_src())

    from learning_studio.identity import IdentityError, resolve_principal

    tokens = _bind(session_context, platform="telegram", user_id="424242", chat_id="c")
    try:
        who = resolve_principal()
        assert who.platform == "telegram"
        assert who.user_id == "424242"
        assert who.source == "gateway_session"
    finally:
        session_context.clear_session_vars(tokens)

    # Once cleared, the previous learner must not still be resolvable. In a
    # process that has engaged the session system there is no "local user" to
    # fall back to, so refusing is the correct — and safer — outcome.
    with pytest.raises(IdentityError):
        resolve_principal()


def test_binding_user_a_then_user_b_does_not_bleed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ab-profile"))
    session_context = _load_session_context(_hermes_src())

    from learning_studio.identity import resolve_principal

    tokens = _bind(session_context, platform="telegram", user_id="aaa")
    try:
        assert resolve_principal().user_id == "aaa"
    finally:
        session_context.clear_session_vars(tokens)

    tokens = _bind(session_context, platform="telegram", user_id="bbb")
    try:
        assert resolve_principal().user_id == "bbb"
    finally:
        session_context.clear_session_vars(tokens)


def test_a_failure_after_binding_does_not_leak_identity(tmp_path, monkeypatch):
    """Cleanup sits in a finally established before state is changed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "leak-profile"))
    session_context = _load_session_context(_hermes_src())

    from learning_studio.identity import IdentityError, resolve_principal

    tokens = _bind(session_context, platform="telegram", user_id="leaky")
    try:
        raise RuntimeError("simulated mid-test failure")
    except RuntimeError:
        pass
    finally:
        session_context.clear_session_vars(tokens)

    # "leaky" must not be visible to whatever runs next. In an engaged
    # process that means a refusal, never a silent fallback to some default.
    with pytest.raises(IdentityError):
        resolve_principal()
