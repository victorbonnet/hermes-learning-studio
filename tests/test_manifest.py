"""``plugin.yaml`` must parse and stay consistent with the package identity."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


@pytest.fixture
def manifest(repo_root: Path) -> dict:
    manifest_path = repo_root / "plugin.yaml"
    assert manifest_path.is_file(), "plugin.yaml must exist at the repository root"
    with manifest_path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_manifest_parses_to_a_mapping(manifest):
    assert isinstance(manifest, dict)


def test_manifest_identity(manifest):
    assert manifest["name"] == "learning-studio"
    assert manifest["version"] == "0.1.0"
    assert manifest["description"].strip()


def test_manifest_declares_no_tools_or_hooks_yet(manifest):
    """The foundation registers no runtime surface, so it must claim none."""
    assert manifest.get("provides_tools", []) == []
    assert manifest.get("provides_hooks", []) == []


def test_manifest_requires_no_env_vars(manifest):
    """Requiring an env var would gate the plugin off before it can load."""
    assert manifest.get("requires_env", []) == []


def test_manifest_is_a_standalone_plugin(manifest):
    """Anything other than ``standalone`` routes to a different subsystem."""
    assert manifest.get("kind", "standalone") == "standalone"


def test_manifest_name_matches_skill_namespace(manifest, ctx):
    """Hermes derives the skill namespace from the manifest name."""
    from learning_studio import register

    register(ctx)

    namespace = ctx.qualified_skill_names[0].split(":", 1)[0]
    assert namespace == manifest["name"]


def test_manifest_version_matches_package_version(manifest):
    import learning_studio

    assert learning_studio.__version__ == manifest["version"]


def test_root_init_and_package_exist(repo_root: Path):
    assert (repo_root / "__init__.py").is_file()
    assert (repo_root / "learning_studio" / "__init__.py").is_file()


def test_reserved_identities_are_stable():
    """These names are load-bearing across PRs; changing one is a breaking change.

    ``PLUGIN_NAME`` is the skill namespace, ``SKILL_NAME`` is how the agent
    addresses the bundled skill, and ``TOOLSET_NAME`` is reserved for the tools
    later PRs register.
    """
    import learning_studio

    assert learning_studio.PLUGIN_NAME == "learning-studio"
    assert learning_studio.SKILL_NAME == "adaptive-learning"
    assert learning_studio.TOOLSET_NAME == "plugin_learning_studio"


def test_entry_point_name_matches_manifest_name(repo_root: Path, manifest):
    """Directory installs read plugin.yaml; pip installs use the entry-point
    name as the manifest name. They must agree or the skill namespace differs
    depending on how the plugin was installed.

    This checks the *name* only. Whether the entry-point *value* actually
    loads the way Hermes expects is covered behaviourally in
    ``tests/test_entry_point.py``.
    """
    tomllib = pytest.importorskip("tomllib")

    with (repo_root / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    entry_points = pyproject["project"]["entry-points"]["hermes_agent.plugins"]
    assert list(entry_points) == [manifest["name"]]
