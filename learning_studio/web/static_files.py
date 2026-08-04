"""The trusted frontend, served from a closed allowlist.

This module is the one place in the plugin that answers an unauthenticated
request, so it is worth being explicit about what that concession is and what
it is not.

**What is public.** Six files: a document, a stylesheet, and four scripts.
They are the same bytes for every caller, they are checked into this repository,
and none of them contains a learner's name, an experience identifier, a session
token, or a configured value. The shell has to be public because a webview
cannot attach a header to the navigation that loads it — there is no request in
which the page could prove who is asking for it. Everything the page then goes
on to *fetch* is protected exactly as before.

**What is not public.** Nothing else. There is no filename in any URL: the
routes are literal paths bound to literal files at import time, so path
traversal has nothing to traverse. A file added to ``static/`` that no entry
below names is not reachable, and a test fails if the two ever disagree.

**The policy is widened by one origin, on one response.** The document needs to
run its own scripts and load Telegram's SDK, which the API's
``script-src 'none'`` forbids. Rather than relax the policy everywhere, the
document carries :data:`DOCUMENT_CONTENT_SECURITY_POLICY` and every other
response — the JSON routes, the images, and the stylesheet and scripts
themselves — keeps the strict one. The distinction is not decorative: the policy
that decides whether a script may execute is the *document's*, so a stylesheet
served under ``script-src 'none'`` costs nothing and a mistake in this file
cannot loosen the API.

There is no ``'unsafe-inline'`` and no ``'unsafe-eval'``, which is a constraint
on the frontend rather than a claim about it: the document carries no inline
script and no ``style`` attribute, and the scripts build the DOM instead of
evaluating strings. Tests assert both, because a policy is only as good as the
code that never needed to ask for more.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

#: Telegram's SDK, loaded from Telegram. It is deliberately *not* vendored: a
#: copy in this repository would be a stale fork of a file whose whole purpose
#: is to match the client currently running the webview, and it is the one
#: script this page cannot serve itself.
TELEGRAM_SDK_ORIGIN = "https://telegram.org"
TELEGRAM_SDK_URL = f"{TELEGRAM_SDK_ORIGIN}/js/telegram-web-app.js"

#: The document's policy: the API's, plus the SDK's origin in ``script-src``,
#: plus ``'self'`` for the stylesheet and ``blob:`` for images.
#:
#: ``blob:`` is what makes a managed image displayable at all. ``/api/assets``
#: requires two request headers, and an ``<img src>`` cannot send them, so the
#: page fetches the bytes and shows the object URL. That is narrower than it
#: looks: a blob URL can only name data this page already holds.
DOCUMENT_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors https://web.telegram.org https://telegram.org; "
    f"script-src 'self' {TELEGRAM_SDK_ORIGIN}; "
    "style-src 'self'; "
    "img-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'none'; "
    "media-src 'none'; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "worker-src 'none'; "
    "manifest-src 'none'"
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass(frozen=True)
class StaticAsset:
    """One literal URL bound to one literal file."""

    url_path: str
    filename: str
    media_type: str
    #: True for the HTML document, which is the only response that needs the
    #: widened policy — and the only one that could be made to execute
    #: anything.
    document: bool = False


#: The allowlist. Order is load order for the scripts, which is also the
#: dependency order: strings, then the icon set, then the renderers that draw
#: with it, then the application that uses all three.
STATIC_ASSETS: tuple[StaticAsset, ...] = (
    StaticAsset("/index.html", "index.html", "text/html; charset=utf-8", document=True),
    StaticAsset("/static/app.css", "app.css", "text/css; charset=utf-8"),
    StaticAsset("/static/i18n.js", "i18n.js", "text/javascript; charset=utf-8"),
    StaticAsset("/static/icons.js", "icons.js", "text/javascript; charset=utf-8"),
    StaticAsset("/static/renderers.js", "renderers.js", "text/javascript; charset=utf-8"),
    StaticAsset("/static/app.js", "app.js", "text/javascript; charset=utf-8"),
)

#: The document, reachable at ``/`` as well, because that is the URL a Mini App
#: button opens.
DOCUMENT = STATIC_ASSETS[0]


@cache
def asset_bytes(filename: str) -> bytes:
    """The file's contents, read once per process.

    Cached on the *filename*, and only ever called with one from
    :data:`STATIC_ASSETS`, so the cache cannot be made to hold arbitrary files.
    Reading eagerly at import time would make a packaging mistake break plugin
    import rather than one request.
    """
    for asset in STATIC_ASSETS:
        if asset.filename == filename:
            return (STATIC_DIR / filename).read_bytes()
    raise KeyError(filename)
