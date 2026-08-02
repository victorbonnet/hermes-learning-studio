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
validated tunnel origin plus ``#launch=<launch id>``.

The selector is in the fragment on purpose: browsers never send a fragment to
the server, so it appears in no request line, no access log, and no ``Referer``
— and the page strips it from history as soon as it has read it. There is no
session token, no experience identifier, no learner name, and no query string.

**The URL is not a credential.** The selector says only which launch is meant;
opening it still requires the learner's Telegram account to verify, to be on
the profile's allowlist, to own the exercise, and to hold that unexpired grant.

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

#: The fragment key the frontend reads the launch selector from. One name, used
#: by the sender and by ``app.js``, so the two cannot drift apart.
LAUNCH_FRAGMENT_KEY = "launch"

#: Failure reasons that *prove* nothing reached Telegram.
#:
#: The distinction matters to the caller and to the learner. A failure in this
#: set means no message exists, so the learner's sentence may safely authorise
#: another attempt. Anything else — a connection that dropped mid-request, a
#: response that could not be read — means a message may be sitting in their
#: chat, and a retry would put a second one beside it.
NOTHING_WAS_SENT = frozenset(
    {
        "bot_token_absent",
        "launch_selector_malformed",
        "telegram_endpoint_unexpected",
        # Telegram received the request and declined it, so there is no message.
        "telegram_refused",
    }
)


def proves_nothing_was_sent(reason: str) -> bool:
    """True when this failure is evidence that no message exists."""
    return reason in NOTHING_WAS_SENT or reason.startswith("delivery_tunnel_url_")


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


def button_url(origin: str, launch_id: str) -> str:
    """The address behind the button: a validated origin and a selector.

    The selector goes in the **fragment**. Browsers do not send a fragment to
    the server — it is absent from the request line and from ``Referer`` — so
    the launch id stays out of the tunnel operator's logs and out of any
    intermediary's, while still reaching the page that needs it.

    The origin is validated *without* the fragment, by the same rule that
    refuses a fragment in a tunnel address: what cloudflared printed must be a
    bare Quick Tunnel origin, and the only thing allowed to add anything to it
    is this function.
    """
    from .runtime.grants import LAUNCH_ID_PATTERN

    if not LAUNCH_ID_PATTERN.match(str(launch_id or "")):
        raise LaunchRefused(DELIVERY_FAILED, reason="launch_selector_malformed")
    return f"{origin}/#{LAUNCH_FRAGMENT_KEY}={launch_id}"


def deliver_web_app_button(
    *,
    destination,
    url: str,
    label: str,
    title: str,
    launch_id: str,
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

    payload = build_payload(
        chat_id=destination.chat_id,
        url=button_url(safe_url, launch_id),
        label=label,
        title=title,
    )
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
    except Exception as exc:
        # The arm that closes the hole. The three above name the failures the
        # standard library documents; anything else — an `http.client`
        # exception, something raised by an injected opener, a bug in this
        # module — used to propagate out of here untouched and land in the tool
        # layer's `logger.exception`, which renders an exception's own text and
        # every `__context__` behind it. The text of a transport exception
        # routinely quotes the request, and the request is a URL with the bot
        # token in its path, so "unexpected" was a way for the credential to
        # reach the log.
        #
        # `Exception` and not `BaseException`: a cancellation or a Ctrl-C is
        # not a delivery failure and must keep unwinding. And, like the others,
        # only the class name is logged — the message is the thing that might
        # be quoting the request.
        logger.warning(
            "the Learning Studio button could not be sent (unexpected %s)", type(exc).__name__
        )
        failure = "telegram_endpoint_unexpected"

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


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, before a second request is made.

    This is the fix for a real leak, not a hardening flourish. ``urllib``
    follows redirects by default, and the bot token is *in the request path* —
    so a ``Location`` pointing anywhere else would have sent the token to
    whoever supplied it. Telegram does not redirect ``sendMessage``; anything
    that does is not Telegram, or is something between us and Telegram.

    Raising rather than returning ``None``: returning ``None`` from a redirect
    handler means "do not follow", but leaves the 3xx to be handled as an
    ordinary response, and this must be a failure.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            # The *original* URL is not repeated here; ``_post`` reports the
            # status code and never renders this exception.
            "",
            code,
            "redirect refused",
            headers,
            fp,
        )


#: One opener, built once, with no redirect handling and no proxy handling.
#:
#: ``ProxyHandler({})`` is an empty proxy map rather than the default, which
#: reads ``http_proxy``/``https_proxy`` from the environment. An operator's
#: proxy is a reasonable thing to want, but it is also a way for a variable
#: nobody audited to become the destination of a request carrying a bot token —
#: so it is not honoured until somebody asks for it explicitly.
_OPENER = urllib.request.build_opener(
    _NoRedirects(),
    urllib.request.ProxyHandler({}),
)


def _urlopen(endpoint: str, body: bytes, timeout: int) -> bytes:
    """The real request. One host, one method, HTTPS, no redirects, no proxy.

    ``Request`` is constructed with the method named explicitly, and the opener
    refuses redirects outright, so nothing can turn this into a second request
    to a URL somebody else chose.
    """
    if not endpoint.startswith(TELEGRAM_API_ORIGIN + "/"):  # pragma: no cover - constant
        raise LaunchRefused(DELIVERY_FAILED, reason="telegram_endpoint_unexpected")

    request = urllib.request.Request(  # noqa: S310 - a fixed https:// constant
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with _OPENER.open(request, timeout=timeout) as response:
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
