"""A fake Hermes plugin context for tests.

Hermes is not a dependency of this repository, so the plugin's contract with
the host is exercised against a stand-in that mirrors the parts of
``hermes_cli.plugins.PluginContext`` this plugin actually uses.

The validation rules below are copied from the host implementation so the
tests fail here rather than at runtime inside Hermes:

- ``register_skill`` rejects ``':'`` in the skill name (the namespace is
  derived from the manifest name automatically).
- Skill names must match ``[a-zA-Z0-9_-]+``.
- The skill path must exist.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class RegisteredSkill:
    """One ``ctx.register_skill()`` call."""

    name: str
    path: Path
    description: str


@dataclass
class RegisteredTool:
    """One ``ctx.register_tool()`` call."""

    name: str
    toolset: str
    schema: dict[str, Any]
    handler: Callable[..., Any]


class FakePluginContext:
    """Records what ``register(ctx)`` asks the host to register."""

    def __init__(self, plugin_name: str = "learning-studio") -> None:
        self.plugin_name = plugin_name
        self.skills: list[RegisteredSkill] = []
        self.tools: list[RegisteredTool] = []
        self.hooks: list[tuple[str, Callable[..., Any]]] = []
        self.commands: list[str] = []
        self.cli_commands: list[str] = []

    # -- surface used by this plugin -------------------------------------

    def register_skill(self, name: str, path: Path, description: str = "") -> None:
        if ":" in name:
            raise ValueError(
                f"Skill name '{name}' must not contain ':' "
                f"(the namespace is derived from the plugin name "
                f"'{self.plugin_name}' automatically)."
            )
        if not name or not SKILL_NAME_RE.match(name):
            raise ValueError(f"Invalid skill name '{name}'. Must match [a-zA-Z0-9_-]+.")
        if not Path(path).exists():
            raise FileNotFoundError(f"SKILL.md not found at {path}")
        self.skills.append(RegisteredSkill(name, Path(path), description))

    # -- surface this PR must NOT use yet --------------------------------

    def register_tool(
        self,
        name: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        toolset: str = "",
        **_kwargs: Any,
    ) -> None:
        self.tools.append(RegisteredTool(name, toolset, schema, handler))

    def register_hook(self, event_name: str, callback: Callable[..., Any]) -> None:
        self.hooks.append((event_name, callback))

    def register_command(
        self, name: str, handler: Callable[..., Any], description: str = ""
    ) -> None:
        self.commands.append(name)

    def register_cli_command(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.cli_commands.append(name)

    # -- convenience -----------------------------------------------------

    @property
    def qualified_skill_names(self) -> list[str]:
        """Skill names as the agent will address them via ``skill_view()``."""
        return [f"{self.plugin_name}:{skill.name}" for skill in self.skills]


@dataclass
class FakeManifest:
    """Mirror of the host's ``PluginManifest`` fields this plugin relies on."""

    name: str
    version: str = ""
    description: str = ""
    provides_tools: list[str] = field(default_factory=list)
    provides_hooks: list[str] = field(default_factory=list)
    kind: str = "standalone"
