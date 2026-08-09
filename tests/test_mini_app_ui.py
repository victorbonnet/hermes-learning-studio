"""The static Mini App shell: what is served, and under which policy.

The shell is the one part of this server that answers an unauthenticated
request, so these tests pin exactly what that means: a closed allowlist of
files, no path parameter anywhere near the filesystem, learner data in none of
them, and a Content-Security-Policy that is wider than the API's by precisely
one origin.
"""

from __future__ import annotations

import pytest

from learning_studio.web.static_files import (
    DOCUMENT_CONTENT_SECURITY_POLICY,
    STATIC_ASSETS,
    STATIC_DIR,
    TELEGRAM_SDK_ORIGIN,
    TELEGRAM_SDK_URL,
)


def markup() -> str:
    """The document with its comments removed.

    Every check below is about what the browser is asked to *do*, and the
    comments in that file explain what it deliberately does not do — they name
    inline scripts and style attributes in order to warn against them, which a
    substring search reads as the offence itself.
    """
    import re

    source = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL)


@pytest.fixture
def client():
    """The shell needs no dependencies, so it is served by a bare app.

    ``create_app()`` with nothing injected has no bot token and no allowlist,
    which makes this fixture a second assertion in disguise: every page below
    is served by a deployment where no protected route would answer at all.
    """
    from fastapi.testclient import TestClient

    from learning_studio.web.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# ── What is served ────────────────────────────────────────────────────────


def test_the_document_is_served_at_the_root(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in response.text.lower()


@pytest.mark.parametrize("asset", STATIC_ASSETS, ids=lambda asset: asset.url_path)
def test_every_declared_asset_is_served_with_its_own_type(client, asset):
    response = client.get(asset.url_path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(asset.media_type.split(";")[0])
    assert response.content == (STATIC_DIR / asset.filename).read_bytes()


def test_the_asset_list_is_exactly_the_static_directory():
    """A file in the directory that no route serves is dead weight; a route
    naming a file that is not there is a 500 waiting to happen."""
    declared = {asset.filename for asset in STATIC_ASSETS}
    on_disk = {path.name for path in STATIC_DIR.iterdir() if path.is_file()}

    assert declared == on_disk


def test_there_is_no_path_parameter_on_any_static_route():
    """The only defence against traversal that cannot be got wrong is having
    no filename in the URL to traverse with."""
    from learning_studio.web.app import create_app

    app = create_app()
    static_paths = {asset.url_path for asset in STATIC_ASSETS} | {"/"}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path in static_paths:
            assert "{" not in path


@pytest.mark.parametrize(
    "path",
    [
        "/static/../app.py",
        "/static/%2e%2e/app.py",
        "/static/",
        "/static/app.js/",
        "/static/nope.js",
        "/app.py",
        "/../pyproject.toml",
    ],
)
def test_nothing_outside_the_allowlist_is_served(client, path):
    assert client.get(path).status_code in (404, 405)


def test_the_shell_carries_no_learner_data(client):
    """The document is served before anybody has authenticated, so it must be
    the same bytes for everyone.

    Every element that will hold exercise text ships empty, so what the check
    looks for is the *absence* of any field name that could only be there
    because a value had been baked in.
    """
    assert client.get("/").text == (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    body = markup()
    for absent in ("experience_id", "learner", "session_token", "initData", "start_param"):
        assert absent not in body


def test_the_frontend_ships_inside_the_package():
    """A wheel without these files serves a 500 per asset and no interface.

    They are data files rather than modules, so nothing imports them and no test
    would otherwise notice their absence from a build. What makes them ship is
    living under the packaged directory — asserted here rather than assumed,
    because moving them somewhere tidier-looking would break an install and
    nothing else.
    """
    from pathlib import Path

    package_root = Path(__file__).resolve().parent.parent / "learning_studio"

    assert STATIC_DIR.is_relative_to(package_root)
    for asset in STATIC_ASSETS:
        assert (STATIC_DIR / asset.filename).is_file()


# ── The policy the document runs under ────────────────────────────────────


def test_the_document_policy_allows_the_telegram_sdk_and_nothing_else(client):
    policy = client.get("/").headers["content-security-policy"]

    assert policy == DOCUMENT_CONTENT_SECURITY_POLICY
    assert f"script-src 'self' {TELEGRAM_SDK_ORIGIN}" in policy
    assert TELEGRAM_SDK_URL.startswith(f"{TELEGRAM_SDK_ORIGIN}/")


@pytest.mark.parametrize("forbidden", ["unsafe-inline", "unsafe-eval", "unsafe-hashes", "*"])
def test_the_document_policy_grants_no_blanket_allowance(forbidden: str):
    assert forbidden not in DOCUMENT_CONTENT_SECURITY_POLICY


@pytest.mark.parametrize(
    "directive",
    [
        "default-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "object-src 'none'",
        "frame-src 'none'",
        "style-src 'self'",
        "connect-src 'self'",
        "frame-ancestors https://web.telegram.org https://telegram.org",
    ],
)
def test_the_document_policy_keeps_the_strict_directives(directive: str):
    assert directive in DOCUMENT_CONTENT_SECURITY_POLICY


def test_only_the_document_is_widened(client):
    """A stylesheet and a script do not need a script-src of their own: the
    policy that decides whether they run is the document's."""
    from learning_studio.web.security import CONTENT_SECURITY_POLICY

    assert client.get("/static/app.js").headers["content-security-policy"] == (
        CONTENT_SECURITY_POLICY
    )
    assert "script-src 'none'" in CONTENT_SECURITY_POLICY


def test_the_api_policy_was_not_widened_for_the_frontend():
    """The JSON API gained nothing from this PR."""
    from learning_studio.web.security import CONTENT_SECURITY_POLICY

    assert "telegram.org/js" not in CONTENT_SECURITY_POLICY
    assert "script-src 'none'" in CONTENT_SECURITY_POLICY
    assert "style-src 'none'" in CONTENT_SECURITY_POLICY


def test_the_shell_still_gets_every_other_security_header(client):
    from learning_studio.web.security import SECURITY_HEADERS

    headers = client.get("/").headers
    for header, value in SECURITY_HEADERS.items():
        if header == "Content-Security-Policy":
            continue
        assert headers[header] == value


def test_the_shell_is_not_cached(client):
    """A cached shell outlives a deployment; the tunnel URL does not."""
    assert "no-store" in client.get("/").headers["cache-control"]


# ── The document only loads what the policy permits ───────────────────────


def test_every_script_the_document_loads_is_same_origin_or_the_sdk():
    import re

    sources = re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", markup())

    assert sources, "the document loads no scripts at all"
    for source in sources:
        assert source.startswith("/static/") or source == TELEGRAM_SDK_URL


def test_the_document_has_no_inline_script_or_style():
    import re

    document = markup()

    assert re.search(r"<script(?![^>]*\bsrc=)", document) is None
    assert "<style" not in document
    assert re.search(r"\sstyle=\"", document) is None


def test_the_document_loads_no_third_party_stylesheet_or_font():
    document = markup()

    for source in ("http://", "//fonts.", "cdn."):
        assert source not in document
    assert document.count("https://") == document.count(TELEGRAM_SDK_URL)


def test_the_scripts_load_in_dependency_order():
    """`renderers.js` draws with the icon set and `app.js` uses all three at
    load time, so the order is the dependency order."""
    import re

    sources = [
        source
        for source in re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", markup())
        if source.startswith("/static/")
    ]

    assert sources == [
        "/static/i18n.js",
        "/static/icons.js",
        "/static/renderers.js",
        "/static/app.js",
    ]
