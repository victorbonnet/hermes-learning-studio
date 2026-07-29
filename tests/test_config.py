"""Configuration: validated, bounded, and failing closed.

Every setting here governs retention, isolation, or consent. A malformed
value must therefore raise rather than fall back to a default — an operator
who typed something wrong needs to find out from an error, not from a
learner's data being kept longer than they intended.
"""

from __future__ import annotations

import pytest

from learning_studio.config import CONFIG_SECTION, ConfigError, LearningStudioConfig


def _config(**settings) -> LearningStudioConfig:
    return LearningStudioConfig.from_mapping({CONFIG_SECTION: settings})


# ── Defaults ──────────────────────────────────────────────────────────────


def test_an_absent_section_yields_safe_defaults():
    config = LearningStudioConfig.from_mapping({})

    assert config.temporary_context_ttl_hours == 72
    assert config.journal_mode == "wal"
    assert config.busy_timeout_ms == 5000
    assert config.max_asset_bytes == 10 * 1024 * 1024
    assert config.max_asset_width == 8192
    assert config.max_asset_height == 8192
    assert config.max_asset_pixels == 40_000_000


def test_the_defaults_presume_no_subject():
    """A default subject or language would make the plugin opinionated."""
    config = LearningStudioConfig.from_mapping({})

    assert config.defaults == {}
    assert config.profile_context == {}


def test_unrelated_configuration_is_ignored():
    config = LearningStudioConfig.from_mapping(
        {"model": {"default": "something"}, "memory": {"provider": "honcho"}}
    )

    assert config == LearningStudioConfig()


def test_a_non_mapping_config_yields_defaults():
    assert LearningStudioConfig.from_mapping(None) == LearningStudioConfig()
    assert LearningStudioConfig.from_mapping("nonsense") == LearningStudioConfig()


def test_the_config_object_is_immutable():
    """Nothing may mutate settings at runtime and change them for later calls."""
    import dataclasses

    config = LearningStudioConfig()

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.temporary_context_ttl_hours = 1  # type: ignore[misc]


# ── Valid values ──────────────────────────────────────────────────────────


def test_valid_settings_are_applied():
    config = _config(
        temporary_context_ttl_hours=24,
        max_tracks_per_learner=5,
        busy_timeout_ms=2500,
        journal_mode="delete",
        memory_candidate_min_evidence=4,
        allow_durable_accessibility_needs=False,
        max_context_value_chars=500,
        max_asset_bytes=1_000_000,
        max_asset_width=4096,
        max_asset_height=2048,
        max_asset_pixels=8_000_000,
    )

    assert config.temporary_context_ttl_hours == 24
    assert config.max_tracks_per_learner == 5
    assert config.busy_timeout_ms == 2500
    assert config.journal_mode == "delete"
    assert config.memory_candidate_min_evidence == 4
    assert config.allow_durable_accessibility_needs is False
    assert config.max_context_value_chars == 500
    assert config.max_asset_bytes == 1_000_000
    assert config.max_asset_width == 4096
    assert config.max_asset_height == 2048
    assert config.max_asset_pixels == 8_000_000


def test_journal_mode_is_case_insensitive():
    assert _config(journal_mode="WAL").journal_mode == "wal"


def test_profile_context_and_defaults_accept_context_fields():
    config = _config(
        profile_context={"explanation_language": "English"},
        defaults={"session_duration": "20 minutes", "preferred_modalities": ["text"]},
    )

    assert config.profile_context == {"explanation_language": "English"}
    assert config.defaults["preferred_modalities"] == ["text"]


# ── Failing closed ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "settings",
    [
        {"temporary_context_ttl_hours": 0},
        {"temporary_context_ttl_hours": -1},
        {"temporary_context_ttl_hours": 99999},
        {"temporary_context_ttl_hours": "72"},
        {"temporary_context_ttl_hours": 72.5},
        {"temporary_context_ttl_hours": True},
        {"max_tracks_per_learner": 0},
        {"max_tracks_per_learner": 10_000},
        {"busy_timeout_ms": 1},
        {"busy_timeout_ms": 10_000_000},
        {"memory_candidate_min_evidence": 1},
        {"max_context_value_chars": 5},
        {"max_asset_bytes": 0},
        {"max_asset_width": 0},
        {"max_asset_height": "8192"},
        {"max_asset_pixels": True},
    ],
)
def test_an_out_of_range_or_wrong_typed_number_is_refused(settings: dict):
    with pytest.raises(ConfigError):
        _config(**settings)


def test_a_boolean_setting_rejects_a_truthy_string():
    """`allow_durable_accessibility_needs: "no"` must not read as True."""
    with pytest.raises(ConfigError, match="must be true or false"):
        _config(allow_durable_accessibility_needs="no")


@pytest.mark.parametrize("value", ["memory", "off", "none", 1, None, True])
def test_an_unsupported_journal_mode_is_refused(value):
    with pytest.raises(ConfigError):
        _config(journal_mode=value)


def test_an_unknown_setting_is_refused():
    """A misspelled consent setting must not look like one that is switched off."""
    with pytest.raises(ConfigError, match="persist_accessibility"):
        _config(persist_accessibility=True)


def test_the_error_names_the_setting_and_the_section():
    with pytest.raises(ConfigError) as exc:
        _config(busy_timeout_ms=-5)

    assert "learning_studio.busy_timeout_ms" in str(exc.value)


def test_a_non_mapping_section_is_refused():
    with pytest.raises(ConfigError, match="must be a mapping"):
        LearningStudioConfig.from_mapping({CONFIG_SECTION: "just a string"})


def test_an_unknown_context_field_in_defaults_is_refused():
    with pytest.raises(ConfigError, match="unknown context field"):
        _config(defaults={"favourite_colour": "blue"})


def test_an_oversized_default_value_is_refused():
    with pytest.raises(ConfigError):
        _config(defaults={"goal": "x" * 5000})


# ── The plugin does not mutate host configuration ─────────────────────────


def test_loading_does_not_mutate_the_mapping_it_was_given():
    original = {CONFIG_SECTION: {"temporary_context_ttl_hours": 24}, "model": {"default": "m"}}
    snapshot = {CONFIG_SECTION: {"temporary_context_ttl_hours": 24}, "model": {"default": "m"}}

    LearningStudioConfig.from_mapping(original)

    assert original == snapshot


def test_load_config_returns_defaults_outside_a_hermes_process():
    """Importable and usable without the host — the test and build path."""
    from learning_studio.config import load_config

    assert isinstance(load_config(), LearningStudioConfig)
