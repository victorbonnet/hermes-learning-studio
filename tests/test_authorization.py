"""Mini App authorisation: an intersection that can only ever narrow.

The rules under test are not this plugin's invention — they mirror
``gateway/authz_mixin.py::_is_user_authorized`` for a direct message. The two
regressions at the top of this file are the reason the mirroring is explicit:
both were real defects that authorised somebody Hermes denies.
"""

from __future__ import annotations

import pytest

from learning_studio.authorization import (
    ENV_ALLOWLISTS,
    GROUP_ONLY_SOURCES,
    NON_AUTHORISING_CONFIG_KEYS,
    any_env_allowlist_configured,
    effective_allowed_users,
    is_authorized,
    profile_allowed_users,
)


def extra_config(**extra) -> dict:
    """``platforms.telegram.extra`` — the bridged shape."""
    return {"platforms": {"telegram": {"extra": extra}}}


def top_level_config(**keys) -> dict:
    """``platforms.telegram`` — the shape operators actually write."""
    return {"platforms": {"telegram": keys}}


def gateway_config(**keys) -> dict:
    """``gateway.platforms.telegram`` — the currently documented shape."""
    return {"gateway": {"platforms": {"telegram": keys}}}


# ── Regressions: two ways this module used to broaden host access ──────────


def test_allow_admin_from_is_not_an_authorisation_source():
    """Regression. It gates slash-command privilege, not access.

    Hermes reads ``allow_admin_from`` only in ``gateway/slash_access.py``. An
    earlier version of this module unioned it into the allowlist, so a user the
    host excluded from Telegram entirely could reach the Mini App by appearing
    there.
    """
    allowed = profile_allowed_users(
        env={},
        host_config=top_level_config(allow_from=["1001"], allow_admin_from=["9999"]),
    )

    assert allowed == {"1001"}
    assert "9999" not in allowed


@pytest.mark.parametrize("key", NON_AUTHORISING_CONFIG_KEYS)
def test_no_admin_key_grants_access_in_any_shape(key: str):
    for build in (extra_config, top_level_config, gateway_config):
        assert profile_allowed_users(env={}, host_config=build(**{key: ["9999"]})) == frozenset()


def test_a_present_allow_from_bounds_access_whatever_the_environment_says():
    """Regression. ``allow_from`` is the intake gate's *sole authority*.

    ``plugins/platforms/telegram/adapter.py::_is_user_authorized_from_message``
    drops a DM from anyone outside ``allow_from`` before the runner is reached,
    so an environment allowlist cannot add a user back. Letting the environment
    win — the previous fix's mistake — authorised 2002 here, whom Hermes never
    delivers a message from at all.
    """
    allowed = profile_allowed_users(
        env={"TELEGRAM_ALLOWED_USERS": "2002"},
        host_config=top_level_config(allow_from=["1001"]),
    )

    assert allowed == frozenset()
    assert "2002" not in allowed


def test_neither_gate_can_add_a_user_the_other_denies():
    """The intersection, stated as a property: both gates must permit."""
    both = profile_allowed_users(
        env={"TELEGRAM_ALLOWED_USERS": "1001,2002"},
        host_config=top_level_config(allow_from=["1001", "3003"]),
    )

    assert both == {"1001"}


def test_an_empty_allow_from_authorises_nobody():
    """Hermes tests ``is not None``, so a present empty list is a lockout.

    Collapsing "present but empty" into "absent" would turn a deliberate
    lockout into a fallback onto the environment allowlist.
    """
    for build in (extra_config, top_level_config, gateway_config):
        assert (
            profile_allowed_users(
                env={"TELEGRAM_ALLOWED_USERS": "1001,2002"}, host_config=build(allow_from=[])
            )
            == frozenset()
        )


@pytest.mark.parametrize("present", ENV_ALLOWLISTS)
def test_an_environment_allowlist_cannot_widen_a_configured_allow_from(present: str):
    allowed = profile_allowed_users(
        env={present: "9999"}, host_config=top_level_config(allow_from=["1001"])
    )

    assert "9999" not in allowed


# ── Reading the environment allowlists ────────────────────────────────────


def test_the_platform_environment_allowlist_authorises():
    assert profile_allowed_users(env={"TELEGRAM_ALLOWED_USERS": "1001,2002"}) == {"1001", "2002"}


def test_the_global_gateway_allowlist_authorises():
    """``GATEWAY_ALLOWED_USERS`` authorises in Hermes, so it authorises here."""
    assert profile_allowed_users(env={"GATEWAY_ALLOWED_USERS": "1001"}) == {"1001"}


def test_the_platform_and_global_environment_allowlists_are_unioned():
    allowed = profile_allowed_users(
        env={"TELEGRAM_ALLOWED_USERS": "1001", "GATEWAY_ALLOWED_USERS": "2002"}
    )

    assert allowed == {"1001", "2002"}


def test_whitespace_around_ids_is_tolerated():
    assert profile_allowed_users(env={"TELEGRAM_ALLOWED_USERS": " 1001 , 2002 "}) == {
        "1001",
        "2002",
    }


@pytest.mark.parametrize("raw", ["", "   ", ",,", "abc", "-5", "0", "1001abc"])
def test_unusable_entries_do_not_become_allowlist_members(raw: str):
    assert profile_allowed_users(env={"TELEGRAM_ALLOWED_USERS": raw}) == frozenset()


# ── Reading the configured allowlist, in every shape Hermes accepts ────────


@pytest.mark.parametrize("build", [extra_config, top_level_config, gateway_config])
def test_the_configured_allowlist_is_honoured_in_every_shape(build):
    """``gateway/config.py`` bridges the top-level key into ``extra``."""
    assert profile_allowed_users(env={}, host_config=build(allow_from=["1001"])) == {"1001"}


def test_the_two_gates_compose_across_the_whole_precedence_table():
    """Every row of the table in ``authorization``'s docstring, in one place."""
    cases = [
        # (allow_from, environment, expected upper bound)
        (["1001", "2002"], {"TELEGRAM_ALLOWED_USERS": "2002"}, {"2002"}),
        (["1001"], {}, {"1001"}),
        ([], {"TELEGRAM_ALLOWED_USERS": "1001"}, set()),
        (None, {"TELEGRAM_ALLOWED_USERS": "1001"}, {"1001"}),
        (None, {}, set()),
    ]

    for allow_from, env, expected in cases:
        host_config = {} if allow_from is None else top_level_config(allow_from=allow_from)
        assert profile_allowed_users(env=env, host_config=host_config) == expected, (
            f"allow_from={allow_from!r} env={env!r}"
        )


def test_a_comma_separated_scalar_allow_from_is_read():
    """Hermes' ``_coerce_allow_set`` accepts a scalar string; so does this."""
    assert profile_allowed_users(env={}, host_config=top_level_config(allow_from="1001,2002")) == {
        "1001",
        "2002",
    }


@pytest.mark.parametrize("host_config", [None, {}, {"platforms": None}, {"platforms": {}}, "text"])
def test_an_absent_or_unusable_configuration_authorises_nobody(host_config):
    assert profile_allowed_users(env={}, host_config=host_config) == frozenset()


# ── Grants this module deliberately does not honour ───────────────────────


@pytest.mark.parametrize("source", GROUP_ONLY_SOURCES)
def test_group_only_authorisations_grant_no_mini_app_access(source: str):
    """Being allowed to speak in a room is not being allowed into a record."""
    allowed = profile_allowed_users(
        env={source: "1001"}, host_config=top_level_config(**{source: ["1001"]})
    )

    assert allowed == frozenset()


@pytest.mark.parametrize("wildcard", ["*", "1001,*"])
def test_a_wildcard_never_opens_the_mini_app(wildcard: str):
    """A wildcard opens a chat bot, not one person's learning record."""
    allowed = profile_allowed_users(env={"TELEGRAM_ALLOWED_USERS": wildcard})

    assert "*" not in allowed
    assert not is_authorized("7777", allowed)


def test_a_wildcard_allow_from_grants_nothing_on_its_own():
    assert profile_allowed_users(env={}, host_config=top_level_config(allow_from=["*"])) == (
        frozenset()
    )


def test_a_wildcard_allow_from_does_not_deny_an_environment_listed_user():
    """A wildcard removes the intake bound, exactly as it does in Hermes.

    Treating it as an empty set instead would deny a user the operator named in
    ``TELEGRAM_ALLOWED_USERS`` because of shorthand written elsewhere — a
    denial with no diagnosable cause.
    """
    allowed = profile_allowed_users(
        env={"TELEGRAM_ALLOWED_USERS": "1001"}, host_config=top_level_config(allow_from=["*"])
    )

    assert allowed == {"1001"}
    assert not is_authorized("7777", allowed)


@pytest.mark.parametrize("flag", ["GATEWAY_ALLOW_ALL_USERS", "TELEGRAM_ALLOW_ALL_USERS"])
def test_an_allow_all_flag_authorises_nobody_here(flag: str):
    assert profile_allowed_users(env={flag: "true"}) == frozenset()


def test_no_environment_and_no_configuration_authorises_nobody():
    assert profile_allowed_users(env={}) == frozenset()
    assert any_env_allowlist_configured({}) is False


# ── The intersection ──────────────────────────────────────────────────────


def test_the_plugin_restriction_narrows_the_profile_allowlist():
    allowed = effective_allowed_users(
        plugin_restriction=("1001",), env={"TELEGRAM_ALLOWED_USERS": "1001,2002,3003"}
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


def test_the_restriction_cannot_resurrect_a_configured_user_the_environment_excludes():
    """The two fixes compose: precedence first, then narrowing."""
    allowed = effective_allowed_users(
        plugin_restriction=("9999",),
        env={"TELEGRAM_ALLOWED_USERS": "1001"},
        host_config=top_level_config(allow_from=["9999"], allow_admin_from=["9999"]),
    )

    assert allowed == frozenset()


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
