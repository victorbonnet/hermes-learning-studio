"""Learning Studio — a standalone Hermes plugin for adaptive learning.

Importing this package must stay cheap and dependency-free: Hermes imports it
and calls :func:`register` during startup for every enabled plugin. Optional
media dependencies, and the optional web dependencies, belong behind
lazy imports inside the code paths that need them, never at module scope.
"""

from .plugin import PLUGIN_NAME, SKILL_NAME, TOOLSET_NAME, register

__version__ = "0.1.0"

__all__ = ["PLUGIN_NAME", "SKILL_NAME", "TOOLSET_NAME", "__version__", "register"]
