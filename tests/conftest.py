"""Shared fixtures and path helpers for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make ``learning_studio`` and ``tests.fake_hermes`` importable when pytest is
# invoked from anywhere.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def ctx():
    """A fake Hermes plugin context bound to this plugin's manifest name."""
    from tests.fake_hermes import FakePluginContext

    return FakePluginContext(plugin_name="learning-studio")
