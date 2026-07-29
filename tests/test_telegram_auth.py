"""Telegram Mini App ``initData`` verification.

These tests are the whole authentication boundary for the Mini App: everything
downstream trusts the user ID that comes out of here. They therefore check the
*algorithm*, not just the happy path — a verifier that accepts a forged hash,
an expired payload, or a group launch is indistinguishable from one that works
until somebody tries it.
"""

from __future__ import annotations

import hmac
import json

import pytest

from learning_studio.telegram_auth import (
    MAX_INIT_DATA_CHARS,
    InitDataError,
    verify_init_data,
)
from tests.init_data import (
    BOT_TOKEN,
    USER_ID,
    build_init_data,
    data_check_string,
    sign,
)

NOW = 1_800_000_000
MAX_AGE = 300


def verify(raw: str, *, now: int = NOW, bot_token: str = BOT_TOKEN, max_age: int = MAX_AGE):
    return verify_init_data(raw, bot_token=bot_token, now=now, max_age_seconds=max_age)


# ── The path that must work ───────────────────────────────────────────────


def test_a_freshly_signed_payload_verifies():
    verified = verify(build_init_data(auth_date=NOW - 10))

    assert verified.user_id == USER_ID
    assert verified.auth_date == NOW - 10


def test_verification_keeps_only_the_user_id():
    """No display name, username, language, or photo reaches the plugin.

    Telegram sends them; retaining them would put personal data into session
    state, logs, and every later response for no functional gain.
    """
    verified = verify(build_init_data(auth_date=NOW))

    retained = json.dumps(verified.describe())
    assert "Test" not in retained
    assert "test_learner" not in retained
    assert not hasattr(verified, "first_name")


# ── Forged, malformed, and absent ─────────────────────────────────────────


def test_a_forged_hash_is_refused():
    with pytest.raises(InitDataError):
        verify(build_init_data(auth_date=NOW, signed=False))


def test_a_payload_signed_with_another_token_is_refused():
    raw = build_init_data(auth_date=NOW, bot_token="9999999999:some-other-bots-token")

    with pytest.raises(InitDataError):
        verify(raw)


def test_a_tampered_user_id_invalidates_the_hash():
    """The classic attack: keep the signature, swap the user."""
    raw = build_init_data(auth_date=NOW)
    tampered = raw.replace("1001", "2002")

    with pytest.raises(InitDataError):
        verify(tampered)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-a-query-string",
        "auth_date=1800000000",  # no hash
        "hash=" + "a" * 64,  # no auth_date, no user
        "auth_date=%zz&hash=" + "a" * 64,
        "user=%7B&auth_date=1800000000&hash=" + "a" * 64,
    ],
)
def test_malformed_payloads_are_refused(raw: str):
    with pytest.raises(InitDataError):
        verify(raw)


def test_an_oversized_payload_is_refused_before_any_parsing():
    with pytest.raises(InitDataError):
        verify("a=" + "b" * (MAX_INIT_DATA_CHARS + 1))


def test_a_duplicated_field_is_refused():
    """Two ``auth_date`` values are two different claims; neither is trusted."""
    raw = build_init_data(auth_date=NOW)

    with pytest.raises(InitDataError):
        verify(raw + "&auth_date=1")


def test_a_non_hex_hash_is_refused():
    raw = build_init_data(auth_date=NOW)
    broken = raw.replace(raw.split("hash=")[1], "z" * 64)

    with pytest.raises(InitDataError):
        verify(broken)


def test_an_absent_bot_token_refuses_rather_than_verifying():
    with pytest.raises(InitDataError):
        verify(build_init_data(auth_date=NOW), bot_token="")


# ── Freshness ─────────────────────────────────────────────────────────────


def test_an_expired_payload_is_refused():
    with pytest.raises(InitDataError):
        verify(build_init_data(auth_date=NOW - MAX_AGE - 1))


def test_a_payload_at_the_edge_of_the_window_still_verifies():
    assert verify(build_init_data(auth_date=NOW - MAX_AGE)).auth_date == NOW - MAX_AGE


def test_a_future_dated_payload_is_refused():
    with pytest.raises(InitDataError):
        verify(build_init_data(auth_date=NOW + 3600))


def test_a_small_clock_skew_is_tolerated():
    assert verify(build_init_data(auth_date=NOW + 5)).auth_date == NOW + 5


def test_a_non_numeric_auth_date_is_refused():
    fields = {"auth_date": "yesterday", "user": '{"id":1001}'}
    fields["hash"] = sign(fields)

    with pytest.raises(InitDataError):
        verify("&".join(f"{k}={v}" for k, v in fields.items()))


# ── Who the payload names ─────────────────────────────────────────────────


def test_a_bot_user_is_refused():
    raw = build_init_data(auth_date=NOW, user=_user('{"id":1001,"is_bot":true}'))

    with pytest.raises(InitDataError):
        verify(raw)


@pytest.mark.parametrize("user_json", ['{"id":0}', '{"id":-5}', '{"id":"1001"}', "{}", "[]", '"x"'])
def test_an_unusable_user_object_is_refused(user_json: str):
    with pytest.raises(InitDataError):
        verify(build_init_data(auth_date=NOW, user=user_json))


# ── Groups are not a Mini App surface ─────────────────────────────────────


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_a_group_launch_is_refused(chat_type: str):
    raw = build_init_data(auth_date=NOW, extra={"chat_type": chat_type})

    with pytest.raises(InitDataError):
        verify(raw)


def test_a_payload_carrying_a_chat_object_is_refused():
    raw = build_init_data(auth_date=NOW, extra={"chat": '{"id":-1001,"type":"supergroup"}'})

    with pytest.raises(InitDataError):
        verify(raw)


@pytest.mark.parametrize("chat_type", ["sender", "private"])
def test_a_direct_launch_verifies(chat_type: str):
    raw = build_init_data(auth_date=NOW, extra={"chat_type": chat_type})

    assert verify(raw).user_id == USER_ID


# ── Properties of the comparison itself ───────────────────────────────────


def test_the_hash_comparison_is_constant_time(monkeypatch):
    """A short-circuiting ``==`` leaks the expected hash a byte at a time."""
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    verify(build_init_data(auth_date=NOW))

    assert calls, "verification must compare hashes with hmac.compare_digest"


def test_a_signature_bearing_payload_verifies():
    """The shape every current Telegram client sends.

    Regression for a real defect: the first version of this module applied the
    third-party Ed25519 exclusion rule (``hash`` *and* ``signature``) to the
    bot-token HMAC, so genuine modern launches were rejected with
    ``init_data_hash_mismatch`` and no session could ever be opened.
    """
    raw = build_init_data(auth_date=NOW, signature="AbCdEf_modern_ed25519_signature")

    assert verify(raw).user_id == USER_ID


def test_the_signature_field_participates_in_the_hmac():
    """Tampering with ``signature`` alone must invalidate the payload.

    This is what distinguishes "included in the signed data" from "ignored".
    If the field were excluded, swapping it would change nothing and this
    payload would still verify.
    """
    raw = build_init_data(auth_date=NOW, signature="AbCdEf_original_signature")
    tampered = raw.replace("AbCdEf_original_signature", "AbCdEf_swapped_signature0")

    with pytest.raises(InitDataError):
        verify(tampered)


def test_a_payload_without_a_signature_still_verifies():
    """Older clients omit the field entirely; they must keep working."""
    assert verify(build_init_data(auth_date=NOW, signature=None)).user_id == USER_ID


def test_the_check_string_is_built_the_way_the_reference_implementations_build_it():
    """Pin the canonicalisation itself, independently of the HMAC.

    Derived from ``@telegram-apps/init-data-node`` (``validate`` pushes every
    pair but ``hash``) and aiogram (``parsed_data.pop("hash")``): sorted
    ``key=value`` pairs joined with a line feed, ``signature`` among them.
    """
    from learning_studio.telegram_auth import _UNSIGNED_FIELDS

    assert {"hash"} == _UNSIGNED_FIELDS

    fields = {
        "user": '{"id":1001}',
        "auth_date": str(NOW),
        "signature": "sig-value",
        "query_id": "q-value",
    }

    assert data_check_string(fields) == "\n".join(
        [
            f"auth_date={NOW}",
            "query_id=q-value",
            "signature=sig-value",
            'user={"id":1001}',
        ]
    )


# ── Failures never quote the payload ──────────────────────────────────────


def test_failure_messages_never_echo_the_payload_or_the_token():
    raw = build_init_data(auth_date=NOW, signed=False)

    with pytest.raises(InitDataError) as caught:
        verify(raw)

    rendered = str(caught.value)
    assert BOT_TOKEN not in rendered
    assert "hash=" not in rendered
    assert USER_ID not in rendered


def _user(json_text: str) -> str:
    return json_text


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
