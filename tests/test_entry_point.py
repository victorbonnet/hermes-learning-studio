"""The pip entry point must satisfy Hermes' loader contract.

Hermes loads a pip-installed plugin with the equivalent of::

    module = ep.load()
    register_fn = getattr(module, "register", None)
    if register_fn is None:
        # "Plugin '<name>' has no register() function"

So the entry-point value must name the *module* (``learning_studio``). A
``module:attribute`` value such as ``learning_studio:register`` makes
``ep.load()`` return the function itself, which has no ``.register``
attribute — the plugin then fails to load with no traceback, only a warning.

These tests exercise the real loading behaviour rather than asserting on the
text of pyproject.toml, so a regression to the broken value fails here.
"""

from __future__ import annotations

import importlib.metadata
import sys
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from tests.fake_hermes import FakePluginContext

tomllib = pytest.importorskip("tomllib")

ENTRY_POINTS_GROUP = "hermes_agent.plugins"
PLUGIN_NAME = "learning-studio"
DISTRIBUTION = "hermes-learning-studio"

#: Modules that must never be pulled in by loading and registering the plugin.
OPTIONAL_DEPS = ("fastapi", "starlette", "uvicorn", "PIL", "pydantic")


def hermes_load(ep: EntryPoint):
    """Reproduce ``PluginManager._load_entrypoint_module`` + the register lookup.

    Returns ``(module, register_fn)`` exactly as Hermes would compute them.
    """
    module = ep.load()
    return module, getattr(module, "register", None)


@pytest.fixture
def declared_entry_point(repo_root: Path) -> EntryPoint:
    """The entry point as declared in pyproject.toml (the source of truth)."""
    with (repo_root / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    declared = pyproject["project"]["entry-points"][ENTRY_POINTS_GROUP]
    assert list(declared) == [PLUGIN_NAME], (
        f"expected exactly one entry point named {PLUGIN_NAME!r}, got {list(declared)}"
    )
    return EntryPoint(name=PLUGIN_NAME, value=declared[PLUGIN_NAME], group=ENTRY_POINTS_GROUP)


# ── The declared entry point satisfies Hermes' contract ────────────────────


def test_declared_entry_point_loads(declared_entry_point: EntryPoint):
    module, _ = hermes_load(declared_entry_point)

    assert module is not None


def test_declared_entry_point_loads_a_module_not_a_function(
    declared_entry_point: EntryPoint,
):
    """The regression guard: `ep.load()` must return the module itself."""
    import types

    module, _ = hermes_load(declared_entry_point)

    assert isinstance(module, types.ModuleType), (
        f"entry point loaded {type(module).__name__}, not a module — "
        f"the value must be 'learning_studio', with no ':register' suffix"
    )
    assert module.__name__ == "learning_studio"


def test_loaded_module_exposes_callable_register(declared_entry_point: EntryPoint):
    _, register_fn = hermes_load(declared_entry_point)

    assert register_fn is not None, "Hermes would report: no register() function"
    assert callable(register_fn)


def test_entry_point_registration_registers_exactly_one_skill(
    declared_entry_point: EntryPoint,
):
    _, register_fn = hermes_load(declared_entry_point)
    ctx = FakePluginContext(plugin_name=PLUGIN_NAME)

    register_fn(ctx)

    assert len(ctx.skills) == 1


def test_entry_point_registers_the_expected_qualified_name(
    declared_entry_point: EntryPoint,
):
    _, register_fn = hermes_load(declared_entry_point)
    ctx = FakePluginContext(plugin_name=PLUGIN_NAME)

    register_fn(ctx)

    assert ctx.qualified_skill_names == ["learning-studio:adaptive-learning"]


def test_entry_point_path_imports_no_optional_dependencies(
    declared_entry_point: EntryPoint,
):
    for name in [n for n in sys.modules if n.split(".", 1)[0] in OPTIONAL_DEPS]:
        del sys.modules[name]

    _, register_fn = hermes_load(declared_entry_point)
    register_fn(FakePluginContext(plugin_name=PLUGIN_NAME))

    leaked = sorted(
        {n.split(".", 1)[0] for n in sys.modules if n.split(".", 1)[0] in OPTIONAL_DEPS}
    )
    assert leaked == []


# ── The old value must NOT satisfy it ──────────────────────────────────────


def test_old_module_colon_attribute_value_would_fail_hermes():
    """Proves these tests discriminate: the previous value breaks the contract.

    If this ever starts passing, ``hermes_load`` no longer reflects Hermes'
    loader and the tests above stopped protecting anything.
    """
    broken = EntryPoint(
        name=PLUGIN_NAME, value="learning_studio:register", group=ENTRY_POINTS_GROUP
    )

    loaded, register_fn = hermes_load(broken)

    assert callable(loaded), "the broken value loads the function, not the module"
    assert register_fn is None, (
        "expected the function to have no .register attribute — this is exactly "
        "what made Hermes report 'no register() function'"
    )


# ── Installed distribution metadata agrees with the declaration ────────────


def test_installed_metadata_matches_declaration(declared_entry_point: EntryPoint):
    """Guards against a stale build: what is installed must match pyproject."""
    try:
        importlib.metadata.distribution(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        pytest.skip(f"{DISTRIBUTION} is not installed in this environment")

    eps = importlib.metadata.entry_points()
    group_eps = (
        eps.select(group=ENTRY_POINTS_GROUP)
        if hasattr(eps, "select")
        else [ep for ep in eps if ep.group == ENTRY_POINTS_GROUP]
    )
    installed = [ep for ep in group_eps if ep.name == PLUGIN_NAME]

    assert installed, f"no installed entry point named {PLUGIN_NAME!r}"
    assert installed[0].value == declared_entry_point.value

    module, register_fn = hermes_load(installed[0])
    assert module.__name__ == "learning_studio"
    assert callable(register_fn)
