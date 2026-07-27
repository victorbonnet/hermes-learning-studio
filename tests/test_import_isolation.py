"""Registration must not drag in optional web/media dependencies.

Later PRs add a FastAPI dashboard and image handling. Those must stay behind
lazy imports: Hermes calls ``register(ctx)`` on every startup for every
enabled plugin, so an unconditional ``import fastapi`` would break the plugin
on any install that did not opt into the extras.
"""

from __future__ import annotations

import importlib
import sys

import pytest

BLOCKED = (
    "fastapi",
    "PIL",
    "uvicorn",
    "starlette",
    "pydantic",
    "telegram",
    "telethon",
    "aiogram",
    "cloudflare",
    "httpx",
    "requests",
    "aiohttp",
    "yaml",
)


class _BlockedFinder:
    """Meta-path finder that makes the named packages unimportable."""

    def __init__(self, blocked: tuple[str, ...]) -> None:
        self.blocked = blocked

    def find_module(self, fullname, path=None):  # pragma: no cover - legacy API
        return None

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in self.blocked:
            raise ImportError(f"{fullname} is blocked in this test environment")
        return None


@pytest.fixture
def without_optional_deps():
    """Simulate an install where FastAPI and Pillow are absent."""
    finder = _BlockedFinder(BLOCKED)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name.split(".", 1)[0] in BLOCKED or name.startswith("learning_studio")
    }
    for name in saved_modules:
        del sys.modules[name]
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        for name in [n for n in sys.modules if n.startswith("learning_studio")]:
            del sys.modules[name]
        sys.modules.update(saved_modules)


def test_import_succeeds_without_optional_deps(without_optional_deps):
    module = importlib.import_module("learning_studio")

    assert hasattr(module, "register")


def test_register_succeeds_without_optional_deps(without_optional_deps):
    module = importlib.import_module("learning_studio")
    from tests.fake_hermes import FakePluginContext

    ctx = FakePluginContext(plugin_name="learning-studio")
    module.register(ctx)

    assert ctx.qualified_skill_names == ["learning-studio:adaptive-learning"]


def test_optional_deps_are_not_imported_as_a_side_effect(without_optional_deps):
    """Belt and braces: nothing blocked should end up in sys.modules."""
    importlib.import_module("learning_studio")

    leaked = [name for name in sys.modules if name.split(".", 1)[0] in BLOCKED]
    assert leaked == []


def test_the_tools_are_registered_without_optional_deps(without_optional_deps):
    """Registration must produce the full surface on a bare install.

    A tool that only appears when FastAPI happens to be installed is a tool
    the agent cannot rely on.
    """
    module = importlib.import_module("learning_studio")
    from tests.fake_hermes import FakePluginContext

    ctx = FakePluginContext(plugin_name="learning-studio")
    module.register(ctx)

    assert sorted(tool.name for tool in ctx.tools) == [
        "learning_studio_get_context",
        "learning_studio_save_context",
    ]


def test_the_tools_run_without_optional_deps(without_optional_deps, tmp_path, monkeypatch):
    """Not just registered — actually usable. SQLite is standard library."""
    import json

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    module = importlib.import_module("learning_studio")
    from tests.fake_hermes import FakePluginContext

    ctx = FakePluginContext(plugin_name="learning-studio")
    module.register(ctx)
    handler = next(t.handler for t in ctx.tools if t.name == "learning_studio_save_context")

    result = json.loads(
        handler({"learner_key": "user-8008", "temporary_context": {"subject": "anything"}})
    )

    assert result["ok"] is True


def test_sqlite3_is_the_only_storage_dependency():
    """The standard library, so a bare install still persists."""
    import learning_studio.storage as storage

    assert storage.sqlite3.__name__ == "sqlite3"
