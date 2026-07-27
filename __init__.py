"""Hermes entry point for the Learning Studio plugin.

Hermes loads a directory plugin by importing this file under the
``hermes_plugins`` namespace via ``spec_from_file_location(...,
submodule_search_locations=[plugin_dir])``. The plugin directory is therefore
*not* placed on ``sys.path``, so the implementation package must be reached by
a relative import.

Hermes sets ``__package__`` before executing this file, so the relative import
is the live path. The absolute fallback covers importers that load this file
as a top-level module with the repository root on ``sys.path`` — notably
pytest, which imports the ancestor ``__init__.py`` of any collected test.
Both branches resolve to the same ``learning_studio`` source tree.

Keeping this shim thin means the same ``register`` is used whether Hermes
loads the plugin from ``~/.hermes/plugins/`` or through the
``hermes_agent.plugins`` entry point declared in ``pyproject.toml``.
"""

if __package__:
    from .learning_studio import __version__, register
else:  # pragma: no cover - exercised via pytest's own collector
    from learning_studio import __version__, register

__all__ = ["__version__", "register"]
