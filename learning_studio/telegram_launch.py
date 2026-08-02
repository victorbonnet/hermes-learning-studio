"""Sending the one message this plugin sends, and nothing else.

This is the only module in the package that reaches a remote host. It makes one
kind of request, to one endpoint, with one shape of body, and it is small on
purpose: it holds a bot token, and the amount of code that touches a credential
should be the amount a person can read in one sitting.

Why a direct Bot API call
-------------------------

Hermes has no ``web_app`` button. Its Telegram adapter builds inline keyboards
for its own flows — approvals, model pickers, clarifications — and none of them
constructs a ``WebAppInfo``, so there is no host API to call for this. Rather
than reach into the adapter's internals from a plugin, this module makes the
one documented Bot API request that produces a Web App button, using the
standard library.

What the message contains
-------------------------

A short line of text and a single inline button whose ``web_app.url`` is the
validated tunnel address. There is no session token, no experience identifier,
no learner name, and no query string. The address behind the button is not a
credential on its own: opening it still requires the learner's Telegram account
to verify, to be on the profile's allowlist, and to hold an unexpired launch
grant.

The token
---------

Read on each send through :mod:`learning_studio.secrets`, which asks Hermes for
the *active profile's* value rather than reading the process environment — in a
multiplexed host those are not the same credential. It is never copied into
configuration, a record, a log line, a response, an exception
message, or a process argument. It appears in exactly one place: the request
path, which is how the Bot API authenticates, and which is why :func:`redact`
exists and why nothing in this module ever puts a URL it built into an error.

Failure
-------

Every failure raises :class:`~learning_studio.runtime.errors.LaunchRefused`
with a fixed, agent-safe message and a reason a programmer chose. Telegram's
own error text is never relayed: it can quote the request, and the request
contains the token.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .runtime.errors import DELIVERY_FAILED, LaunchRefused

logger = logging.getLogger(__name__)

#: The Bot API. A module constant rather than a setting: an operator-supplied
#: endpoint would be a way to point a bot token at somebody else's server.
TELEGRAM_API_ORIGIN = "https://api.telegram.org"

SEND_MESSAGE_METHOD = "sendMessage"

#: A send either works quickly or is not worth waiting for: the learner is
#: watching a conversation, and a tool call that hangs is worse than one that
#: says it could not send.
TIMEOUT_SECONDS = 15

#: Telegram's own ceiling for a message is far higher; this is what this
#: message needs. A response larger than this is not one from the Bot API.
MAX_RESPONSE_BYTES = 64 * 1024

#: The line above the button. Deliberately plain: it names the exercise and
#: nothing about the learner, because a notification preview is visible on a
#: locked screen.
MESSAGE_TEMPLATE = "{title}"


def deliver_web_app_button(
    *,
    destination,
    url: str,
    label: str,
    title: str,
    bot_token: str | None = None,
    opener=None,
) -> None:
    """Send the learner a button that opens their exercise.

    ``opener`` is injected so the tests exercise the whole body-building and
    error-handling path without a network. There is no configuration that
    changes the endpoint, and no argument that changes the destination: the
    chat comes from :class:`~learning_studio.destination.TelegramDestination`,
    which the caller derived from the authenticated session.
    """
    from .runtime.tunnel import TunnelError, validate_quick_tunnel_url

    token = bot_token if bot_token is not None else _token_from_environment()
    if not token:
        # Absent means refuse. It must never mean "send it somewhere else" or
        # "pretend it went".
        raise LaunchRefused(DELIVERY_FAILED, reason="bot_token_absent")

    try:
        # Validated once more, here, at the last moment before it is sent. The
        # runtime checked it and the control plane checked it; this is the
        # check that is in the same function as the send, which is the one that
        # cannot be bypassed by a future caller.
        safe_url = validate_quick_tunnel_url(url)
    except TunnelError as exc:
        raise LaunchRefused(DELIVERY_FAILED, reason=f"delivery_{exc.reason}") from exc

    payload = build_payload(chat_id=destination.chat_id, url=safe_url, label=label, title=title)
    _post(token, payload, opener=opener)


def build_payload(*, chat_id: str, url: str, label: str, title: str) -> dict[str, Any]:
    """The Bot API request body, built field by field.

    Nothing is passed through from anywhere: the title is bounded here, the
    label is bounded here, and the only identifier in it is the chat the caller
    derived from the session.
    """
    # ``parse_mode`` is deliberately absent rather than set: omitting it is how
    # the Bot API selects plain text. Sending Markdown would make an exercise
    # title containing an underscore either render wrongly or be rejected
    # outright, and the title is written by a language model.
    return {
        "chat_id": str(chat_id),
        "text": MESSAGE_TEMPLATE.format(title=_bounded(title, 200)),
        "disable_notification": False,
        "reply_markup": {
            "inline_keyboard": [[{"text": _bounded(label, 64), "web_app": {"url": url}}]]
        },
    }


def _bounded(text: object, limit: int) -> str:
    value = str(text or "").strip()
    return value[:limit] or "Exercise"


def _token_from_environment() -> str:
    """The active profile's token, through Hermes' own secret scope.

    Not ``os.environ``. Hermes can multiplex several profiles through one
    process, and the token in the process environment there may belong to a
    different profile — so a launch for profile B would be delivered by
    profile A's bot, to a chat id that means somebody else on that bot.
    """
    from .secrets import telegram_bot_token

    return telegram_bot_token()


def _post(token: str, payload: dict[str, Any], *, opener=None) -> None:
    """One request, with every failure turned into the same safe refusal.

    The refusal is raised **outside** the ``except`` blocks, and that is not a
    style choice. Raising inside one sets ``__context__`` on the new exception
    even with ``from None`` — ``from None`` only stops the default traceback
    printer from *rendering* it. The original is a ``urllib`` error whose text
    quotes the request URL, and the request URL is where the bot token lives,
    so anything that walks ``__context__`` — a structured error reporter, a
    crash handler, a well-meaning log line — would print the token. An
    exception raised where no exception is being handled has no context to
    walk.
    """
    send = opener or _urlopen
    body = json.dumps(payload).encode("utf-8")
    endpoint = f"{TELEGRAM_API_ORIGIN}/bot{token}/{SEND_MESSAGE_METHOD}"

    raw = b""
    failure: str | None = None
    try:
        raw = send(endpoint, body, TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        # The status code, and not `str(exc)`: an HTTPError renders as its
        # message *and its URL*.
        logger.warning("the Learning Studio button could not be sent (HTTP %s)", exc.code)
        failure = f"telegram_http_{exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The class name, chosen by a programmer. Never the message: a URLError
        # wraps whatever the transport said, which may include the request.
        logger.warning("the Learning Studio button could not be sent (%s)", type(exc).__name__)
        failure = f"telegram_unreachable_{type(exc).__name__}"

    if failure is not None:
        raise LaunchRefused(DELIVERY_FAILED, reason=failure)

    _require_success(raw)


def _require_success(raw: bytes) -> None:
    """Telegram answers 200 for a refusal too, so the body decides.

    ``{"ok": false, "description": "chat not found"}`` arrives with a 200
    status. Treating the status as the answer would report a delivered button
    for a message that was never sent — which is the one lie this whole feature
    is arranged to avoid, because the agent would go on to tell the learner to
    tap something that is not there.
    """
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LaunchRefused(DELIVERY_FAILED, reason="telegram_response_too_large")

    parsed: Any = None
    malformed = False
    try:
        parsed = json.loads(raw)
    except ValueError:
        malformed = True
    if malformed:
        raise LaunchRefused(DELIVERY_FAILED, reason="telegram_response_not_json")
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        # The description is Telegram's text about our request. It is not
        # relayed, and not logged, for the same reason the URL is not.
        raise LaunchRefused(DELIVERY_FAILED, reason="telegram_refused")


def _urlopen(endpoint: str, body: bytes, timeout: int) -> bytes:
    """The real request. One host, one method, HTTPS, no redirects followed.

    ``Request`` is constructed with the method named explicitly so that a
    redirect cannot turn this into a GET of a URL that contains the token.
    """
    request = urllib.request.Request(  # noqa: S310 - a fixed https:// constant
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read(MAX_RESPONSE_BYTES + 1)


def redact(text: str, token: str) -> str:
    """Remove a bot token from a string that is about to be shown or logged.

    Used by the tests to prove the property they are asserting is real rather
    than accidental, and available to any future caller that has to render text
    this module produced. The bot id — the part before the colon — is removed
    with the secret, because on its own it identifies the bot.
    """
    if not token:
        return text
    cleaned = text.replace(token, "[redacted]")
    bot_id = token.split(":", 1)[0]
    if bot_id and bot_id.isdigit():
        cleaned = cleaned.replace(bot_id, "[redacted]")
    return cleaned
