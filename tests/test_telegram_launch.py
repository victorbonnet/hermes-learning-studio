"""The one module that reaches a remote host, held to one rule: say nothing.

No test here makes a network request. The request opener is injected, and the
tests that matter most are about what happens to a bot token when things go
wrong — because the token lives in the request path, and an exception from
``urllib`` quotes the request path.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import pytest

from learning_studio import telegram_launch
from learning_studio.runtime.errors import LaunchRefused

TOKEN = "123456789:AAHfakeTokenForTestsOnly_notARealSecret"
URL = "https://calm-forest-1234.trycloudflare.com"


@dataclass
class Destination:
    chat_id: str = "1001"
    telegram_user_id: str = "1001"


class Opener:
    """A stand-in for ``urlopen``, recording what it was asked to send."""

    def __init__(self, response: bytes | None = None, *, raises: Exception | None = None) -> None:
        self.response = response if response is not None else json.dumps({"ok": True}).encode()
        self.raises = raises
        self.endpoint: str | None = None
        self.body: bytes | None = None

    def __call__(self, endpoint: str, body: bytes, timeout: int) -> bytes:
        self.endpoint = endpoint
        self.body = body
        if self.raises:
            raise self.raises
        return self.response


def send(opener, *, token: str = TOKEN, url: str = URL, label: str = "Open Learning Studio"):
    telegram_launch.deliver_web_app_button(
        destination=Destination(),
        url=url,
        label=label,
        title="Photosynthesis, five questions",
        bot_token=token,
        opener=opener,
    )


# ── What is sent ──────────────────────────────────────────────────────────


def test_a_web_app_button_is_sent_to_the_derived_chat():
    opener = Opener()

    send(opener)

    body = json.loads(opener.body)
    assert body["chat_id"] == "1001"
    button = body["reply_markup"]["inline_keyboard"][0][0]
    assert button["web_app"]["url"] == URL
    assert button["text"] == "Open Learning Studio"


def test_the_message_goes_to_the_bot_api_send_message_method():
    opener = Opener()

    send(opener)

    assert opener.endpoint.startswith("https://api.telegram.org/bot")
    assert opener.endpoint.endswith("/sendMessage")


def test_the_body_carries_no_session_token_identifier_or_query_string():
    opener = Opener()

    send(opener)

    body = json.loads(opener.body)
    flattened = json.dumps(body)
    assert "?" not in body["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
    for forbidden in ("session", "experience_id", "initData", "token", "learner"):
        assert forbidden not in flattened


def test_the_button_label_is_bounded_to_what_telegram_accepts():
    opener = Opener()

    send(opener, label="x" * 500)

    button = json.loads(opener.body)["reply_markup"]["inline_keyboard"][0][0]
    assert len(button["text"]) == 64


def test_the_title_is_bounded_and_never_empty():
    payload = telegram_launch.build_payload(chat_id="1", url=URL, label="Open", title="   ")

    assert payload["text"] == "Exercise"


def test_the_message_is_sent_as_plain_text():
    """Without this, a title containing an underscore renders wrongly or is refused."""
    opener = Opener()

    send(opener)

    assert "parse_mode" not in json.loads(opener.body)


# ── The address is validated one last time, here ──────────────────────────


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.test",
        "http://calm-forest.trycloudflare.com",
        "https://calm.trycloudflare.com@evil.test",
        "https://calm-forest.trycloudflare.com.evil.test",
        "",
    ],
)
def test_an_address_that_is_not_a_quick_tunnel_is_never_sent(hostile: str):
    opener = Opener()

    with pytest.raises(LaunchRefused):
        send(opener, url=hostile)

    assert opener.endpoint is None, "a request was made for an unusable address"


# ── The token ─────────────────────────────────────────────────────────────


def test_an_absent_token_refuses_rather_than_sending_anything():
    opener = Opener()

    with pytest.raises(LaunchRefused) as caught:
        send(opener, token="")

    assert caught.value.reason == "bot_token_absent"
    assert opener.endpoint is None


def test_the_token_never_appears_in_a_refusal_message():
    opener = Opener(raises=urllib.error.URLError(f"failed to reach {TOKEN}"))

    with pytest.raises(LaunchRefused) as caught:
        send(opener)

    assert TOKEN not in str(caught.value)
    assert TOKEN not in caught.value.reason


def test_an_http_error_does_not_carry_the_request_url_onward():
    """``HTTPError.__str__`` includes the URL, and the URL contains the token."""
    error = urllib.error.HTTPError(
        url=f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        code=403,
        msg="Forbidden",
        hdrs=None,
        fp=None,
    )
    opener = Opener(raises=error)

    with pytest.raises(LaunchRefused) as caught:
        send(opener)

    assert TOKEN not in str(caught.value)
    assert TOKEN not in caught.value.reason
    assert caught.value.reason == "telegram_http_403"


def test_no_exception_from_this_module_chains_to_one_holding_the_token():
    """`raise ... from None` throughout: a chained cause is printed too.

    A traceback that shows "during handling of the above exception" prints the
    original, and the original is a urllib error quoting the request URL.
    """
    opener = Opener(raises=urllib.error.URLError(f"nope {TOKEN}"))

    with pytest.raises(LaunchRefused) as caught:
        send(opener)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or TOKEN not in str(caught.value.__context__ or "")


def test_the_token_is_not_logged_when_a_send_fails(caplog):
    opener = Opener(raises=urllib.error.URLError(f"nope {TOKEN}"))

    with caplog.at_level("DEBUG"), pytest.raises(LaunchRefused):
        send(opener)

    assert TOKEN not in caplog.text
    assert "123456789" not in caplog.text


def test_redaction_removes_the_secret_and_the_bot_id():
    text = f"https://api.telegram.org/bot{TOKEN}/sendMessage failed"

    cleaned = telegram_launch.redact(text, TOKEN)

    assert TOKEN not in cleaned
    assert "123456789" not in cleaned


def test_the_token_is_read_from_the_environment_hermes_already_uses(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    opener = Opener()

    telegram_launch.deliver_web_app_button(
        destination=Destination(), url=URL, label="Open", title="Title", opener=opener
    )

    assert TOKEN in opener.endpoint


# ── Telegram's own refusals ───────────────────────────────────────────────


def test_a_two_hundred_that_says_ok_false_is_a_failure():
    """The one lie this whole feature is arranged to avoid.

    Telegram answers 200 with ``{"ok": false}`` for "chat not found". Reading
    the status alone would report a delivered button for a message that was
    never sent, and the agent would go on to tell the learner to tap something
    that is not there.
    """
    opener = Opener(json.dumps({"ok": False, "description": "chat not found"}).encode())

    with pytest.raises(LaunchRefused) as caught:
        send(opener)

    assert caught.value.reason == "telegram_refused"


def test_telegram_description_text_is_never_relayed():
    opener = Opener(
        json.dumps({"ok": False, "description": f"bad token {TOKEN} for chat 1001"}).encode()
    )

    with pytest.raises(LaunchRefused) as caught:
        send(opener)

    assert TOKEN not in str(caught.value)
    assert "chat not found" not in str(caught.value)


def test_an_unparseable_response_is_a_failure():
    with pytest.raises(LaunchRefused) as caught:
        send(Opener(b"<html>gateway timeout</html>"))

    assert caught.value.reason == "telegram_response_not_json"


def test_an_oversized_response_is_a_failure():
    with pytest.raises(LaunchRefused) as caught:
        send(Opener(b"x" * (telegram_launch.MAX_RESPONSE_BYTES + 10)))

    assert caught.value.reason == "telegram_response_too_large"


def test_every_refusal_tells_the_agent_not_to_claim_success():
    for opener in (
        Opener(json.dumps({"ok": False}).encode()),
        Opener(b"not json"),
        Opener(raises=urllib.error.URLError("down")),
    ):
        with pytest.raises(LaunchRefused) as caught:
            send(opener)
        assert "do not tell them to tap anything" in caught.value.message.lower()


# ── What this module is not allowed to become ─────────────────────────────


def test_the_endpoint_is_a_constant_and_not_a_setting():
    """An operator-supplied endpoint would point a bot token at another server."""
    from learning_studio.config import LearningStudioConfig

    assert telegram_launch.TELEGRAM_API_ORIGIN == "https://api.telegram.org"
    assert not any(
        "telegram_api" in name or "api_origin" in name
        for name in LearningStudioConfig.__dataclass_fields__
    )


def test_this_is_the_only_module_that_reaches_a_remote_host():
    package = Path(telegram_launch.__file__).parent
    reaching = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "urllib.request" in path.read_text(encoding="utf-8")
    )

    assert reaching == ["telegram_launch.py"], reaching


def test_the_request_is_a_post_that_does_not_become_a_get_on_redirect():
    source = Path(telegram_launch.__file__).read_text(encoding="utf-8")

    assert 'method="POST"' in source
