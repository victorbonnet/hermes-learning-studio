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
    """The canonical check string for the **bot-token HMAC** algorithm.

    Only ``hash`` is removed. ``signature`` stays in, because Telegram's two
    validation algorithms exclude different fields and this is the bot-token
    one:

    - ``@telegram-apps/init-data-node`` — ``validate3rd`` (Ed25519) skips both
      ``hash`` and ``signature``; ``validate`` (HMAC) skips only ``hash`` and
      pushes everything else, ``signature`` included, into the signed pairs.
    - aiogram's ``check_webapp_signature`` pops only ``hash``.

    This fixture was originally written from a summary that conflated the two
    and therefore agreed with a verifier that had the same defect. It is now
    derived from those two independent implementations, so it can disagree
    with the code under test — which is the only reason a fixture is worth
    having.
    """
    payload = {k: v for k, v in fields.items() if k != "hash"}
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


#: A syntactically plausible stand-in for the Ed25519 proof current Telegram
#: clients attach. It is not a real signature and is never verified here — its
#: job is to be *present*, because a payload carrying it is the shape a real
#: launch has, and it must participate in the bot-token HMAC.
SIGNATURE = "K1xY2z_QVZ3d4e5F6g7H8i9J0kLmNoPqRsTuVwXyZ01aBcDeFgHiJkLmNoPqRsTuVw"


def build_init_data(
    *,
    user_id: str = USER_ID,
    auth_date: int,
    bot_token: str = BOT_TOKEN,
    signed: bool = True,
    user: str | None = None,
    extra: dict[str, str] | None = None,
    omit: tuple[str, ...] = (),
    signature: str | None = SIGNATURE,
) -> str:
    """Build a percent-encoded ``initData`` query string.

    ``signature`` is present by **default**, so the ordinary fixture is the
    payload a current Telegram client actually sends. Verification that
    wrongly excluded the field from the HMAC would then fail every test that
    uses this helper, rather than passing against a payload shape that has not
    existed since Telegram added third-party validation. Pass
    ``signature=None`` for the older shape.

    ``signed=False`` produces a well-formed payload carrying a wrong hash,
    which is the interesting negative case: structurally valid, cryptographically
    forged.
    """
    fields: dict[str, str] = {
        "auth_date": str(auth_date),
        "query_id": "AAF_test_query_id",
        "user": user if user is not None else user_field(user_id),
    }
    if signature is not None:
        fields["signature"] = signature
    fields.update(extra or {})
    for name in omit:
        fields.pop(name, None)
    fields["hash"] = sign(fields, bot_token=bot_token) if signed else "0" * 64
    return urlencode(fields)
