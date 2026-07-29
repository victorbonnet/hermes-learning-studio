"""Deterministic Telegram ``initData`` fixtures.

Every fixture is built here, offline, from a fake bot token. Nothing in the
test suite talks to Telegram, and no real token, user ID, or bot ID appears in
this repository — the token below is a syntactically valid placeholder whose
only job is to make the HMAC derivation deterministic.

The construction deliberately mirrors the documented algorithm rather than
calling the plugin's own verifier: a fixture built by the code under test
would agree with it even when both are wrong.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import urlencode

#: Shape-accurate placeholder: ``<bot_id>:<secret>``, neither of which exists.
BOT_TOKEN = "1234567890:TEST-ONLY-not-a-real-bot-token-000000000"

#: The learner used across the API tests. Matches ``conftest.principal``, so a
#: Mini App session resolves to the same stored learner as a chat session.
USER_ID = "1001"
OTHER_USER_ID = "2002"


def data_check_string(fields: dict[str, str]) -> str:
    """The canonical check string: sorted ``key=value`` pairs, LF-joined."""
    payload = {k: v for k, v in fields.items() if k not in ("hash", "signature")}
    return "\n".join(f"{key}={payload[key]}" for key in sorted(payload))


def sign(fields: dict[str, str], *, bot_token: str = BOT_TOKEN) -> str:
    """``hex(HMAC_SHA256(data_check_string, HMAC_SHA256(token, "WebAppData")))``."""
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string(fields).encode("utf-8"), hashlib.sha256).hexdigest()


def user_field(user_id: str = USER_ID, **overrides: Any) -> str:
    """The JSON ``user`` field exactly as Telegram encodes it."""
    user = {
        "id": int(user_id),
        "first_name": "Test",
        "last_name": "Learner",
        "username": "test_learner",
        "language_code": "en",
        "allows_write_to_pm": True,
    }
    user.update(overrides)
    return json.dumps(user, separators=(",", ":"))


def build_init_data(
    *,
    user_id: str = USER_ID,
    auth_date: int,
    bot_token: str = BOT_TOKEN,
    signed: bool = True,
    user: str | None = None,
    extra: dict[str, str] | None = None,
    omit: tuple[str, ...] = (),
) -> str:
    """Build a percent-encoded ``initData`` query string.

    ``signed=False`` produces a well-formed payload carrying a wrong hash,
    which is the interesting negative case: structurally valid, cryptographically
    forged.
    """
    fields: dict[str, str] = {
        "auth_date": str(auth_date),
        "query_id": "AAF_test_query_id",
        "user": user if user is not None else user_field(user_id),
    }
    fields.update(extra or {})
    for name in omit:
        fields.pop(name, None)
    fields["hash"] = sign(fields, bot_token=bot_token) if signed else "0" * 64
    return urlencode(fields)
