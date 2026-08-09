"""Contract tests over the frontend sources.

These are the checks that do not need a DOM, and several of them could not be
made from inside one: whether a *string* like ``innerHTML`` appears anywhere in
the shipped JavaScript, whether the paths the client calls are the paths the
server serves, whether every component type the registry can produce has a
renderer, and whether the three locale tables agree key for key.

The executing tests live in ``tests/js/`` and run through
``tests/test_frontend_js.py``. These are the ones that hold whether or not Node
is installed.
"""

from __future__ import annotations

import re

import pytest

from learning_studio.components import COMPONENT_TYPES
from learning_studio.web.static_files import STATIC_ASSETS, STATIC_DIR

SCRIPTS = ("i18n.js", "icons.js", "renderers.js", "app.js")

#: The XML namespace an ``<svg>`` has to be created in. It is an identifier
#: rather than an address — nothing fetches it — and ``icons.js`` is the only
#: file that names it.
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def source(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def code(name: str) -> str:
    """A script with its comments removed.

    Every check below asks what the code *does*. The comments in these files
    explain at length what they deliberately avoid — they name ``innerHTML``,
    ``eval``, and ``document.write`` in order to say why none of them is used —
    so a search that included them would find the warning and call it the
    offence.
    """
    text = source(name)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


# ── Nothing becomes markup, and nothing becomes code ──────────────────────

#: Every way a string can turn into markup or into a running program. The
#: Content-Security-Policy stops an inline ``<script>`` and a ``javascript:``
#: URL; it does *not* stop ``element.innerHTML = untrusted``, which is why this
#: list exists as well as the policy.
FORBIDDEN_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "createContextualFragment",
    "setHTMLUnsafe",
    "srcdoc",
    "eval(",
    "new Function",
    'setTimeout("',
    "javascript:",
    "dangerouslySet",
)


@pytest.mark.parametrize("name", SCRIPTS)
@pytest.mark.parametrize("sink", FORBIDDEN_SINKS)
def test_no_script_can_turn_a_string_into_markup_or_code(name: str, sink: str):
    assert sink not in code(name), f"{name} reaches for {sink}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_script_registers_an_event_handler_as_an_attribute(name: str):
    """``setAttribute("onclick", …)`` is a string that becomes code."""
    assert re.search(r"setAttribute\(\s*[\"']on", code(name)) is None


def test_the_only_html_the_frontend_produces_comes_from_createelement():
    """One helper, one call site, one way for an element to exist."""
    renderers = code("renderers.js")

    assert renderers.count("createElement") == 1
    assert "function el(tag, options)" in renderers


def test_svg_is_built_through_one_call_site_of_its_own():
    """The icons and the score ring, and nothing else, use the SVG namespace.

    ``createElementNS`` lives in ``icons.js`` alone, which is why the count
    above still holds: an ``<svg>`` cannot be created with ``createElement``,
    and mixing the two in one file would have made that check unreadable.
    """
    icons = code("icons.js")

    assert icons.count("createElementNS") == 1
    assert "createElement(" not in icons
    for name in ("renderers.js", "app.js", "i18n.js"):
        assert "createElementNS" not in code(name), f"{name} builds SVG of its own"


def test_the_icons_are_drawings_rather_than_markup():
    """A drawing is a list of shapes and numbers here, not a string of tags."""
    icons = code("icons.js")

    assert "<svg" not in icons
    assert "<path" not in icons


def test_readme_describes_the_score_ring_as_decoration_not_a_second_announcement():
    readme = (STATIC_DIR.parents[2] / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.lower().split())

    assert "named, for a screen reader" not in normalized
    assert "the ring is `aria-hidden` decoration" in normalized
    assert "single accessible text equivalent" in normalized


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_script_persists_anything_between_launches(name: str):
    """A stored token is a token that can be stolen later.

    The session is short-lived and lives in a closure; there is nothing to
    persist, and nowhere it could be persisted to.
    """
    for store in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert store not in code(name), f"{name} uses {store}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_script_names_an_external_origin(name: str):
    """The SDK is loaded by the document. The scripts talk to this server only.

    The SVG namespace is the single exception, and it is not an exception to the
    rule the check exists for: it is an XML identifier that a document must name
    in order to contain SVG at all, and nothing requests it. It is removed here
    rather than allowed through, so anything *else* in that file that looked
    like an address would still fail.
    """
    body = code(name).replace(f'"{SVG_NAMESPACE}"', '""')

    assert re.search(r"https?://", body) is None


def test_the_svg_namespace_is_named_once_and_never_fetched():
    icons = code("icons.js")

    assert icons.count(SVG_NAMESPACE) == 1
    assert f'var SVG_NS = "{SVG_NAMESPACE}";' in icons
    for reachable in ("fetch", "XMLHttpRequest", "src", "href"):
        assert reachable not in icons, f"icons.js reaches for {reachable}"


def test_the_stylesheet_requests_nothing_from_the_network():
    stylesheet = re.sub(r"/\*.*?\*/", "", source("app.css"), flags=re.DOTALL)

    assert "@import" not in stylesheet
    assert re.search(r"url\(", stylesheet) is None
    assert re.search(r"https?://", stylesheet) is None


# ── The client and the server agree ───────────────────────────────────────


def declared_api_paths() -> set[str]:
    """The paths named in ``app.js``'s single API table."""
    table = re.search(r"var API = \{(.*?)\};", code("app.js"), flags=re.DOTALL)
    assert table, "app.js no longer declares its API paths in one place"
    return set(re.findall(r"\"(/api/[^\"]*)\"", table.group(1)))


def test_the_client_calls_only_paths_the_server_serves():
    from learning_studio.web.app import create_app

    served = {
        getattr(route, "path", "")
        for route in create_app().routes
        if getattr(route, "path", "").startswith("/api/")
    }
    # The asset route takes an identifier; the client holds its prefix.
    served = {path.split("{")[0] for path in served}

    assert declared_api_paths() <= served, declared_api_paths() - served


def test_the_client_knows_about_every_api_route():
    """A route the frontend never calls is either dead or a missing feature.

    ``/api/health`` is the exception and stays out: it exists for an operator
    checking that the server is up, and a client that pinged it would be doing
    nothing with the answer.
    """
    from learning_studio.web.app import create_app

    served = {
        getattr(route, "path", "").split("{")[0]
        for route in create_app().routes
        if getattr(route, "path", "").startswith("/api/")
    }

    assert served - declared_api_paths() == {"/api/health"}


def test_the_client_sends_the_headers_the_server_reads():
    from learning_studio.web.app import INIT_DATA_HEADER, SESSION_HEADER

    app_source = code("app.js")

    assert f'"{INIT_DATA_HEADER}"' in app_source
    assert f'"{SESSION_HEADER}"' in app_source


def test_every_element_the_application_asks_for_exists_in_the_document():
    """A renamed id is a `null` and a broken app, three states deep."""
    document = re.sub(r"<!--.*?-->", "", source("index.html"), flags=re.DOTALL)
    declared = set(re.findall(r'\bid="([^"]+)"', document))

    # Two ways `app.js` names an element: the array of ids it caches at startup,
    # and any direct lookup. The array is located by the loop that consumes it
    # rather than by matching quoted words line by line — every localization key
    # and state name in the file is also a quoted word.
    app_source = code("app.js")
    cached = re.search(r"\[([^\]]*)\]\.forEach\(function \(id\) \{", app_source, flags=re.DOTALL)
    assert cached, "app.js no longer caches its elements from one list of ids"

    wanted = set(re.findall(r'"([^"]+)"', cached.group(1)))
    wanted |= set(re.findall(r'getElementById\("([^"]+)"\)', app_source))

    assert len(wanted) > 5, "too few element ids found; this check has stopped working"
    assert wanted <= declared, sorted(wanted - declared)


def test_the_document_references_only_assets_the_server_serves():
    document = source("index.html")
    served = {asset.url_path for asset in STATIC_ASSETS}

    for reference in re.findall(r'(?:src|href)="(/[^"]*)"', document):
        assert reference in served, f"{reference} is not in the static allowlist"


# ── Every component type can be shown ─────────────────────────────────────


def renderer_types() -> set[str]:
    """The types ``renderers.js`` registers, however each one is spelled."""
    body = code("renderers.js")
    return set(re.findall(r"RENDERERS\.([a-z_]+) =", body))


def test_every_component_type_in_the_registry_has_a_renderer():
    """The registry is the contract; a type with no renderer is a card a learner
    is shown an apology for."""
    assert renderer_types() == set(COMPONENT_TYPES), {
        "missing": sorted(set(COMPONENT_TYPES) - renderer_types()),
        "unknown": sorted(renderer_types() - set(COMPONENT_TYPES)),
    }


def icon_table() -> dict[str, str]:
    """The ``TYPE_ICONS`` table in ``icons.js``, parsed rather than executed."""
    body = code("icons.js")
    block = re.search(r"var TYPE_ICONS = \{(.*?)\n  \};", body, flags=re.DOTALL)
    assert block, "icons.js no longer declares its type table in one place"
    return dict(re.findall(r"([a-z_]+): \"([a-z]+)\",", block.group(1)))


def test_every_component_type_has_an_icon():
    """A type nobody drew renders a card that looks like nothing in particular.

    The registry is the contract here too: a component type added without an
    icon would still render, which is exactly why it needs a test rather than a
    reviewer noticing.
    """
    table = icon_table()

    assert set(table) == set(COMPONENT_TYPES), {
        "missing": sorted(set(COMPONENT_TYPES) - set(table)),
        "unknown": sorted(set(table) - set(COMPONENT_TYPES)),
    }


def test_every_icon_a_type_names_is_drawn():
    body = code("icons.js")
    drawn = set(re.findall(r"^    ([a-z]+): \[", body, flags=re.MULTILINE))

    assert set(icon_table().values()) <= drawn, sorted(set(icon_table().values()) - drawn)
    # The two icons no component type names: the generic card for a type this
    # build has never seen, and the tick on the recorded confirmation.
    assert {"card", "check"} <= drawn


def test_the_icons_state_their_colour_in_terms_of_the_theme():
    """An icon that named a colour would ignore the Telegram palette."""
    body = code("icons.js")

    assert 'stroke: "currentColor"' in body
    assert re.search(r"#[0-9a-fA-F]{3,8}", body) is None


def test_the_renderers_are_not_hardcoded_to_one_subject():
    """The same neutrality rule the skill corpus is held to.

    A renderer that mentioned a subject would be a renderer built for one
    syllabus. The type names are subject-neutral by construction; the code
    around them should be too.
    """
    body = code("renderers.js").lower()

    for subject in ("spanish", "french verb", "kanji", "python code", "chemistry", "mitosis"):
        assert subject not in body


# ── Localization ──────────────────────────────────────────────────────────


def locale_tables() -> dict[str, dict[str, str]]:
    """The string tables, parsed out of ``i18n.js``.

    Parsed rather than executed, so this test needs no JavaScript runtime and
    runs everywhere the Python suite does.
    """
    body = source("i18n.js")
    tables: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"^    ([a-z]{2}): \{$", body, flags=re.MULTILINE):
        locale = match.group(1)
        rest = body[match.end() :]
        block = rest[: rest.index("\n    },")]
        keys = re.findall(r'"((?:[a-z_]+\.)+[a-z_]+)":', block)
        tables[locale] = dict.fromkeys(keys, "")
    return tables


def test_the_string_tables_are_parsed_at_all():
    tables = locale_tables()

    assert set(tables) == {"en", "es", "fr"}
    assert len(tables["en"]) > 60


def test_every_locale_carries_every_key():
    """A partial table is worse than no table: it renders half a card in the
    reader's language and half in English, with no way to tell which."""
    tables = locale_tables()
    english = set(tables["en"])

    for locale, table in tables.items():
        assert set(table) == english, {
            "locale": locale,
            "missing": sorted(english - set(table)),
            "extra": sorted(set(table) - english),
        }


def test_every_key_the_interface_asks_for_exists():
    """Otherwise the interface renders a dotted identifier at somebody."""
    english = set(locale_tables()["en"])
    asked = set()
    for name in ("renderers.js", "app.js"):
        body = code(name)
        asked |= set(re.findall(r'\bt\(\s*"((?:[a-z_]+\.)+[a-z_]+)"', body))
        # `state(name)` composes `<name>.title` and `<name>.body` — except for
        # the loading state, which is a title and a spinner.
        for stem in re.findall(r'showState\(\s*"([a-z_]+)"', body):
            asked.add(f"{stem}.title")
            if stem != "loading":
                asked.add(f"{stem}.body")

    assert asked <= english, sorted(asked - english)
    assert "loading.title" in asked


def test_every_card_type_is_named_in_every_locale():
    """The badge's label is composed from the type, so the parts have to exist.

    A missing one is not a crash: the interface falls back to the key and the
    badge deliberately shows nothing rather than printing ``card.type.timeline``
    at somebody. That is the safety net, not the plan.
    """
    tables = locale_tables()

    for locale, table in tables.items():
        for component_type in COMPONENT_TYPES:
            assert f"card.type.{component_type}" in table, f"{locale}: {component_type}"


def test_composed_keys_resolve_for_every_difficulty_and_rating():
    """Two keys are built by concatenation, so the parts have to exist."""
    from learning_studio.manifest import DIFFICULTIES

    english = set(locale_tables()["en"])

    for difficulty in DIFFICULTIES:
        assert f"difficulty.{difficulty}" in english
    for rating in ("again", "hard", "good", "easy"):
        assert f"rate.{rating}" in english


def test_no_interface_string_contains_markup():
    """These strings are only ever assigned to `textContent`, and a string with a
    tag in it would be a sign that somebody expected otherwise."""
    body = source("i18n.js")

    for candidate in re.findall(r'": "([^"]*)"', body):
        assert "<" not in candidate
        assert "&" not in candidate


def test_the_fallback_locale_is_the_one_that_is_complete():
    body = code("i18n.js")

    assert 'var FALLBACK_LOCALE = "en";' in body
    assert set(locale_tables()["en"]) >= set(locale_tables()["fr"])


# ── Accessibility, at the level a source check can reach ──────────────────


def test_the_document_declares_the_things_a_webview_needs():
    document = source("index.html")

    assert 'name="viewport"' in document
    assert "viewport-fit=cover" in document
    assert 'name="color-scheme"' in document
    assert 'lang="en"' in document
    assert "<noscript>" in document


def test_the_document_provides_a_skip_link_and_live_regions():
    document = source("index.html")

    assert 'class="skip-link"' in document
    assert 'aria-live="polite"' in document
    assert 'role="status"' in document
    assert 'role="progressbar"' in document
    assert 'tabindex="-1"' in document


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_script_writes_a_style_attribute(name: str):
    """`style-src 'self'` has no `'unsafe-inline'`, so a `style` attribute would
    be dropped by the browser and the rule would only be visible as a bug.

    Custom properties set through ``element.style`` are how the Telegram palette
    and the two positioned markers work; those are DOM properties, not an
    attribute this code writes.
    """
    assert re.search(r"setAttribute\(\s*[\"']style", code(name)) is None


#: Classes the scripts attach and the stylesheet has to know about. A class that
#: exists in only one of the two places is either dead CSS or an unstyled
#: element, and both look fine in a diff.
STYLED_CLASSES = ("type-badge", "type-label", "recorded-pill", "score-ring", "card-enter", "icon")


@pytest.mark.parametrize("name", STYLED_CLASSES)
def test_every_class_the_frontend_attaches_is_styled(name: str):
    stylesheet = source("app.css")
    scripts = "".join(code(script) for script in SCRIPTS)

    assert f'"{name}"' in scripts, f"nothing attaches the {name} class"
    assert f".{name}" in stylesheet, f"{name} is attached but never styled"


def test_the_stylesheet_honours_reduced_motion_and_safe_areas():
    stylesheet = source("app.css")

    assert "prefers-reduced-motion" in stylesheet
    # The card's entrance is switched off two ways: the duration token goes to
    # zero, and the animation itself is dropped.
    assert stylesheet.count(".card-enter {\n  animation: none;\n}") == 1
    assert '[data-reduced-motion="true"] .card-enter' in stylesheet
    assert "env(safe-area-inset-bottom)" in stylesheet
    assert "env(safe-area-inset-top)" in stylesheet
    assert "--tg-viewport-stable-height" in stylesheet
    assert ":focus-visible" in stylesheet


def test_the_stylesheet_keeps_a_touch_target_size():
    """A 44px minimum is the smallest thing a thumb reliably hits."""
    stylesheet = source("app.css")

    assert "--tap: 44px" in stylesheet
    assert stylesheet.count("min-height: var(--tap)") >= 2


def test_the_stylesheet_styles_both_colour_schemes():
    stylesheet = source("app.css")

    assert "color-scheme: light dark" in stylesheet
    assert '[data-theme="dark"]' in stylesheet
    # Every colour is a custom property with a fallback, so a theme Telegram
    # does not send cannot leave text the colour of its background.
    assert stylesheet.count("var(--tg-") >= 12


def test_wide_content_scrolls_inside_itself():
    """A table wider than a phone must not make the page scroll sideways."""
    stylesheet = source("app.css")

    assert "overflow-x: auto" in stylesheet
    assert "overflow-wrap: anywhere" in stylesheet


# ── The response contract this PR introduces ──────────────────────────────

#: What each component type submits as its ``response``. This table is the
#: contract the evaluation runtime will consume, so it is written down here as
#: well as in the README, and a renderer that changed shape silently would have
#: to change this file too.
RESPONSE_KEYS = {
    "multiple_choice": "option_id",
    "multi_select": "option_ids",
    "true_false": "value",
    "classification": "assignments",
    "fill_blank": "blanks",
    "short_answer": "text",
    "free_response": "text",
    "translation": "text",
    "error_correction": "text",
    "code_response": "code",
    "sentence_order": "order",
    "sequence_order": "order",
    "matching": "pairs",
    "categorization": "assignments",
    "flashcard": "self_rating",
    "typed_recall": "text",
    "image_observation": "text",
    "image_choice": "option_id",
    "diagram": "text",
    "hotspot": "points",
    "labeling": "labels",
    "timeline": "order",
    "process_flow": "order",
    "table_grid": "cells",
    "scenario_choice": "option_id",
    "decision_path": "decisions",
    "case_study": "responses",
    "confidence_rating": "rating",
    "self_explanation": "responses",
    "reflection": "responses",
    "rubric_response": "text",
}


def test_the_response_contract_covers_every_component_type():
    assert set(RESPONSE_KEYS) == set(COMPONENT_TYPES)


def test_every_declared_response_key_appears_in_the_renderers():
    body = code("renderers.js")

    for component_type, key in RESPONSE_KEYS.items():
        assert f"{key}:" in body or f'"{key}"' in body, f"{component_type} → {key}"


# ── The review gallery ────────────────────────────────────────────────────


def test_the_preview_gallery_renders_every_type_without_a_server(tmp_path):
    """The screenshots in ``docs/screenshots`` come from this generator.

    It is a development tool, so nothing would notice it breaking — except a
    reviewer who wanted a picture and got a traceback. It also exercises
    ``build_component`` over every registry type, which is why it is cheap to
    keep honest.
    """
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    try:
        import preview_gallery
    finally:
        sys.path.remove(str(tools))

    page = preview_gallery.generate(tmp_path / "preview", locale="en", types=list(COMPONENT_TYPES))

    assert page.is_file()
    fixtures = (tmp_path / "preview" / "fixtures.js").read_text(encoding="utf-8")
    for component_type in COMPONENT_TYPES:
        assert f'"type": "{component_type}"' in fixtures
    for name in ("app.css", "i18n.js", "icons.js", "renderers.js", "gallery.js", "dark.html"):
        assert (tmp_path / "preview" / name).is_file()
    # It renders cards; it does not run the application, which is what would need
    # a server and a session.
    assert "app.js" not in page.read_text(encoding="utf-8")


def test_the_gallery_carries_no_hidden_field_and_no_real_asset(tmp_path):
    """A committed screenshot must not be a screenshot of an answer key."""
    import sys
    from pathlib import Path

    from tests.component_examples import CANARY

    tools = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    try:
        import preview_gallery
    finally:
        sys.path.remove(str(tools))

    preview_gallery.generate(tmp_path / "preview", locale="en", types=list(COMPONENT_TYPES))
    fixtures = (tmp_path / "preview" / "fixtures.js").read_text(encoding="utf-8")

    assert CANARY not in fixtures
    assert "/api/" not in fixtures
    assert preview_gallery.PLACEHOLDER.startswith("data:image/svg+xml")


def test_the_committed_screenshots_are_present_and_documented():
    from pathlib import Path

    screenshots = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
    index = (screenshots / "README.md").read_text(encoding="utf-8")

    images = sorted(path.name for path in screenshots.glob("*.png"))
    assert images, "the README references screenshots that are not committed"
    for name in images:
        assert name in index, f"{name} is committed but not explained"


def test_the_readme_documents_the_response_contract():
    """A wire format nobody wrote down is a wire format the next PR guesses at."""
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    text = readme.read_text(encoding="utf-8")

    for key in sorted(set(RESPONSE_KEYS.values())):
        assert f"`{key}`" in text, f"the README does not document the {key} response"


# ── The files themselves are what they claim to be ────────────────────────


@pytest.mark.parametrize("asset", STATIC_ASSETS, ids=lambda asset: asset.filename)
def test_no_static_file_contains_a_control_character(asset):
    """A stray NUL is invisible in a diff and breaks every text tool quietly.

    This is not hypothetical: a composite lookup key in `renderers.js` was written
    with a NUL byte where a space belonged. The JavaScript ran — a NUL is legal in
    a string literal — but `grep` classified the file as binary and silently
    stopped matching, so every shell-based review of that file had been returning
    nothing at all.
    """
    raw = (STATIC_DIR / asset.filename).read_bytes()

    offenders = {byte for byte in raw if byte < 0x09 or 0x0D < byte < 0x20 or byte == 0x7F}
    assert offenders == set(), f"{asset.filename} contains control bytes {sorted(offenders)}"


@pytest.mark.parametrize("asset", STATIC_ASSETS, ids=lambda asset: asset.filename)
def test_every_static_file_is_valid_utf8_text(asset):
    (STATIC_DIR / asset.filename).read_bytes().decode("utf-8")


# ── The hotspot's numbers agree with the answer schema ────────────────────


def test_the_hotspot_submits_coordinates_the_answer_schema_can_be_compared_with():
    """Normalised to 0–1, which is the unit the stored regions are declared in."""
    from learning_studio.components import SPEC_BY_TYPE, object_schema

    answer = object_schema(SPEC_BY_TYPE["hotspot"].answer)
    points = answer["properties"]["regions"]["items"]["properties"]["points"]["items"]

    assert points["minimum"] == 0.0
    assert points["maximum"] == 1.0

    body = code("renderers.js")
    assert "HOTSPOT_PRECISION" in body
    assert "clamp01" in body


def test_the_hotspot_step_sizes_are_stated_to_the_learner():
    """A keyboard affordance nobody is told about is not an affordance."""
    english = locale_tables()["en"]

    assert "{coarse}" in _string_for("card.hotspot_keyboard")
    assert "{fine}" in _string_for("card.hotspot_keyboard")
    assert "card.hotspot_keyboard" in english


def _string_for(key: str) -> str:
    body = source("i18n.js")
    match = re.search(rf'"{re.escape(key)}":\s*"((?:[^"\\]|\\.)*)"', body)
    assert match, f"{key} is not declared"
    return match.group(1)


# ── The reveal is wired end to end ────────────────────────────────────────


def test_the_frontend_knows_how_to_turn_a_card_over():
    body = code("renderers.js")

    assert "ctx.reveal(" in body, "the renderer has no way to ask for a reveal"
    assert "card.turn_over" in body


def test_only_the_application_talks_to_the_reveal_route():
    """Renderers reach the network through injected functions and no other way."""
    renderers = code("renderers.js")

    assert "/api/" not in renderers
    assert "fetch" not in renderers


def test_the_renderers_cannot_reach_the_network_at_all():
    renderers = code("renderers.js")

    for reachable in ("XMLHttpRequest", "WebSocket", "EventSource", "navigator.sendBeacon"):
        assert reachable not in renderers
