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
        "learning_studio_attempts",
        "learning_studio_erase_learner",
        "learning_studio_get_context",
        "learning_studio_import_asset",
        "learning_studio_launch",
        "learning_studio_prepare",
        "learning_studio_results",
        "learning_studio_review_plan",
        "learning_studio_save_context",
        "learning_studio_set_review_reminders",
        "learning_studio_status",
        "learning_studio_stop",
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

    result = json.loads(handler({"temporary_context": {"subject": "anything"}}))

    assert result["ok"] is True


def test_import_tool_fails_safely_when_pillow_is_absent(
    without_optional_deps, tmp_path, monkeypatch
):
    import json

    home = tmp_path / "home"
    source = home / "cache" / "images" / "candidate.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not decoded before the lazy Pillow import")
    monkeypatch.setenv("HERMES_HOME", str(home))
    module = importlib.import_module("learning_studio")
    from tests.fake_hermes import FakePluginContext

    ctx = FakePluginContext(plugin_name="learning-studio")
    module.register(ctx)
    handler = next(t.handler for t in ctx.tools if t.name == "learning_studio_import_asset")
    result = json.loads(
        handler(
            {
                "source_path": str(source),
                "title": "Candidate",
                "alt_text": "A candidate educational image.",
                "provenance": "operator_selected",
            }
        )
    )

    assert result["ok"] is False
    assert "optional media dependency" in result["error"]
    assert str(source) not in json.dumps(result)
    assert not (home / "workspace" / "learning-studio" / "assets").exists()


def test_registration_does_not_reach_the_web_package(without_optional_deps):
    """The Mini App API must not be imported by enabling the plugin.

    It lives behind the ``web`` extra, so an install that never opted in has
    no FastAPI — and would crash at startup if registration touched it.
    """
    module = importlib.import_module("learning_studio")
    from tests.fake_hermes import FakePluginContext

    module.register(FakePluginContext(plugin_name="learning-studio"))

    assert "learning_studio.web.app" not in sys.modules


def test_the_web_package_itself_imports_without_fastapi(without_optional_deps):
    """Asking *whether* the extra is installed must not require it."""
    web = importlib.import_module("learning_studio.web")

    assert web.extra_is_installed() is False
    assert "fastapi" not in sys.modules


def test_the_api_module_needs_the_extra_and_says_so(without_optional_deps):
    with pytest.raises(ImportError):
        importlib.import_module("learning_studio.web.app")


def test_telegram_verification_needs_no_optional_dependency(without_optional_deps):
    """Authentication is standard library, so it is testable and auditable."""
    telegram_auth = importlib.import_module("learning_studio.telegram_auth")

    with pytest.raises(telegram_auth.InitDataError):
        telegram_auth.verify_init_data(
            "auth_date=1&hash=" + "a" * 64, bot_token="x", now=1, max_age_seconds=60
        )


def test_the_web_extra_is_declared_separately_from_the_base_package():
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["project"]["dependencies"] == []
    extras = pyproject["project"]["optional-dependencies"]
    assert any(spec.startswith("fastapi") for spec in extras["web"])
    assert not any(spec.startswith("fastapi") for spec in extras["media"])


def test_sqlite3_is_the_only_storage_dependency():
    """The standard library, so a bare install still persists."""
    import learning_studio.storage as storage

    assert storage.sqlite3.__name__ == "sqlite3"
