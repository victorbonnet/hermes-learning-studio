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

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[..., Any] | None = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> None:
        """Mirror of ``PluginContext.register_tool``.

        The parameter *order* matters and is easy to get wrong: the host's
        signature is ``(name, toolset, schema, handler, ...)``, not the
        ``(name, schema, handler, toolset="")`` that some documentation
        shows. A fake with the wrong order accepts positional calls the real
        host would silently mis-bind — registering a tool whose toolset is a
        schema dict — so this copy is kept faithful and the plugin calls it
        with keywords only.

        The validation below mirrors ``tools.registry.register``: a schema
        must carry the name, description, and parameters the model needs,
        and ``override`` is refused because a plugin may not shadow a
        built-in without an operator opt-in in config.yaml.
        """
        if not isinstance(schema, dict):
            raise TypeError(
                f"schema for tool '{name}' must be a dict, got {type(schema).__name__} "
                "(check the register_tool argument order)"
            )
        for key in ("name", "description", "parameters"):
            if key not in schema:
                raise ValueError(f"Tool schema for '{name}' is missing '{key}'.")
        if schema["name"] != name:
            raise ValueError(
                f"Tool schema name '{schema['name']}' does not match registered name '{name}'."
            )
        if not isinstance(toolset, str) or not toolset:
            raise ValueError(f"Tool '{name}' must declare a non-empty toolset.")
        if not callable(handler):
            raise TypeError(f"Handler for tool '{name}' is not callable.")
        if override:
            raise PermissionError(
                f"Plugin '{self.plugin_name}' cannot override built-in tool '{name}' "
                "without an operator opt-in (plugins.entries.<id>.allow_tool_override)."
            )
        if any(tool.name == name for tool in self.tools):
            raise ValueError(f"Tool '{name}' is already registered.")
        self.tools.append(RegisteredTool(name, toolset, schema, handler))

    # -- surface this PR must NOT use yet --------------------------------

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
