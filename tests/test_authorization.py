"""Mini App authorisation: an intersection that can only ever narrow."""

from __future__ import annotations

import pytest

from learning_studio.authorization import (
    GROUP_ONLY_SOURCES,
    effective_allowed_users,
    is_authorized,
    profile_allowed_users,
)


def config(**extra) -> dict:
    return {"platforms": {"telegram": {"extra": extra}}}


# ── Reading the profile's own allowlist ───────────────────────────────────


def test_the_environment_allowlist_is_honoured():
    allowed = profile_allowed_users(env={"TELEGRAM_ALLOWED_USERS": "1001,2002"})

    assert allowed == {"1001", "2002"}


def test_the_config_allowlist_is_honoured():
    allowed = profile_allowed_users(env={}, host_config=config(allow_from=["1001"]))

    assert allowed == {"1001"}


def test_admins_are_allowed_users_too():
    allowed = profile_allowed_users(env={}, host_config=config(allow_admin_from=["7"]))

    assert allowed == {"7"}


def test_both_sources_are_unioned_as_hermes_unions_them():
    allowed = profile_allowed_users(
        env={"TELEGRAM_ALLOWED_USERS": "1001"}, host_config=config(allow_from=["2002"])
    )

    assert allowed == {"1001", "2002"}


@pytest.mark.parametrize("raw", ["", "   ", ",,", "abc", "-5", "0", "1001abc"])
def test_unusable_entries_do_not_become_allowlist_members(raw: str):
    assert profile_allowed_users(env={"TELEGRAM_ALLOWED_USERS": raw}) == frozenset()


def test_whitespace_around_ids_is_tolerated():
    assert profile_allowed_users(env={"TELEGRAM_ALLOWED_USERS": " 1001 , 2002 "}) == {
        "1001",
        "2002",
    }


# ── Groups authorise nothing here ─────────────────────────────────────────


@pytest.mark.parametrize("source", GROUP_ONLY_SOURCES)
def test_group_only_authorisations_grant_no_mini_app_access(source: str):
    """Being allowed to speak in a room is not being allowed into a record."""
    allowed = profile_allowed_users(
        env={source: "1001"},
        host_config=config(**{source: ["1001"]}),
    )

    assert allowed == frozenset()


# ── The intersection ──────────────────────────────────────────────────────


def test_the_plugin_restriction_narrows_the_profile_allowlist():
    allowed = effective_allowed_users(
        plugin_restriction=("1001",),
        env={"TELEGRAM_ALLOWED_USERS": "1001,2002,3003"},
    )

    assert allowed == {"1001"}


def test_an_empty_plugin_restriction_leaves_the_profile_allowlist_alone():
    allowed = effective_allowed_users(
        plugin_restriction=(), env={"TELEGRAM_ALLOWED_USERS": "1001,2002"}
    )

    assert allowed == {"1001", "2002"}


def test_the_plugin_can_never_broaden_profile_access():
    """The whole point: a plugin setting cannot add a user the host excluded."""
    allowed = effective_allowed_users(
        plugin_restriction=("2002", "3003", "4004"),
        env={"TELEGRAM_ALLOWED_USERS": "1001,2002"},
    )

    assert allowed == {"2002"}
    assert "3003" not in allowed
    assert "4004" not in allowed


def test_a_restriction_disjoint_from_the_profile_allows_nobody():
    allowed = effective_allowed_users(
        plugin_restriction=("9009",), env={"TELEGRAM_ALLOWED_USERS": "1001"}
    )

    assert allowed == frozenset()


def test_no_profile_allowlist_means_nobody_is_authorised():
    """An unconfigured deployment is closed, not open."""
    assert effective_allowed_users(plugin_restriction=("1001",), env={}) == frozenset()
    assert effective_allowed_users(plugin_restriction=None, env={}) == frozenset()


# ── The membership test ───────────────────────────────────────────────────


def test_membership_is_exact():
    allowed = frozenset({"1001"})

    assert is_authorized("1001", allowed)
    assert not is_authorized("10010", allowed)
    assert not is_authorized("100", allowed)


@pytest.mark.parametrize("user_id", ["", "2002"])
def test_absent_or_unknown_identity_is_denied(user_id: str):
    assert not is_authorized(user_id, frozenset({"1001"}))


def test_an_empty_allowlist_denies_everyone():
    assert not is_authorized("1001", frozenset())
