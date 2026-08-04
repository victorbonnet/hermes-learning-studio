"""``register(ctx)`` must register exactly one skill and nothing else."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def test_register_registers_exactly_one_skill(ctx):
    from learning_studio import register

    register(ctx)

    assert len(ctx.skills) == 1
    assert ctx.skills[0].name == "adaptive-learning"


def test_registered_skill_path_exists_and_is_a_skill_md(ctx):
    from learning_studio import register

    register(ctx)

    path = ctx.skills[0].path
    assert path.is_file(), f"registered skill path does not exist: {path}"
    assert path.name == "SKILL.md"


def test_registered_skill_has_a_description(ctx):
    from learning_studio import register

    register(ctx)

    assert ctx.skills[0].description.strip()


def test_skill_is_addressable_as_namespaced_name(ctx):
    from learning_studio import register

    register(ctx)

    assert ctx.qualified_skill_names == ["learning-studio:adaptive-learning"]


def test_register_registers_exactly_the_twelve_tools(ctx):
    """Twelve tools, no more. Four for the record, four for the runtime, four
    for the evaluation runtime's attempts, review plan, reminder opt-in, and
    erasure."""
    from learning_studio import register

    register(ctx)

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


def test_registered_tools_share_the_reserved_toolset(ctx):
    from learning_studio import register

    register(ctx)

    assert {tool.toolset for tool in ctx.tools} == {"plugin_learning_studio"}


def test_register_registers_one_observe_only_hook_and_no_commands(ctx):
    """One hook, and it only watches.

    ``pre_gateway_dispatch`` is how launching learns what the learner actually
    said rather than taking the model's word for it. The callback records and
    returns ``None``, so message dispatch is unchanged. Slash commands and CLI
    subcommands are still not registered at all.
    """
    from learning_studio import register
    from learning_studio.plugin import CONSENT_EVIDENCE_HOOK

    register(ctx)

    assert [name for name, _ in ctx.hooks] == [CONSENT_EVIDENCE_HOOK]
    assert ctx.commands == []
    assert ctx.cli_commands == []


def test_the_registered_hook_only_observes(ctx):
    """It must never skip or rewrite a message: returning None is "carry on"."""
    from learning_studio import register

    register(ctx)
    callback = dict(ctx.hooks)[  # noqa: RUF015 - one hook, named for clarity
        "pre_gateway_dispatch"
    ]

    assert callback(event=None) is None


def test_root_shim_loads_the_way_hermes_loads_it(repo_root: Path, ctx):
    """Hermes imports the plugin directory by file location, not via sys.path.

    It uses ``spec_from_file_location(..., submodule_search_locations=[dir])``
    under the ``hermes_plugins`` namespace, so the plugin directory itself is
    never on ``sys.path``. This test reproduces that exact mechanism to prove
    the root ``__init__.py`` resolves its subpackage by relative import.
    """
    module_name = "hermes_plugins.learning_studio_under_test"
    init_file = repo_root / "__init__.py"

    parent_name = "hermes_plugins"
    if parent_name not in sys.modules:
        parent_spec = importlib.util.spec_from_loader(parent_name, loader=None)
        assert parent_spec is not None
        parent = importlib.util.module_from_spec(parent_spec)
        parent.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent_name] = parent

    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[str(repo_root)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(repo_root)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)

        assert hasattr(module, "register"), "root __init__.py must expose register()"
        module.register(ctx)
        assert ctx.qualified_skill_names == ["learning-studio:adaptive-learning"]
    finally:
        for name in [n for n in sys.modules if n.startswith(module_name)]:
            del sys.modules[name]
