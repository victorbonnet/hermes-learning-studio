"""The built-in palette has to be legible before Telegram sends a theme.

Every colour in the stylesheet is `var(--tg-something, fallback)`. The fallback is
what a learner sees in a client that sends a partial palette, in a webview opened
outside Telegram, and whenever a theme value is rejected as malformed — which is
to say, often enough that "we will get a theme" is not a plan.

The dark-theme button was white on `#3a86d6`: 3.78:1, below the 4.5:1 that WCAG
2.1 AA asks for normal text. It looked fine, which is exactly why this is
computed rather than eyeballed.
"""

from __future__ import annotations

import re

import pytest

from learning_studio.web.static_files import STATIC_DIR

#: WCAG 2.1 AA, normal text. Large text is allowed 3:1; nothing here is large.
MINIMUM_CONTRAST = 4.5


def relative_luminance(colour: str) -> float:
    """WCAG relative luminance of a `#rrggbb` colour."""
    digits = colour.lstrip("#")
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    channels = [int(digits[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(one: str, two: str) -> float:
    first, second = relative_luminance(one), relative_luminance(two)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def stylesheet() -> str:
    return (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def fallbacks(block: str) -> dict[str, str]:
    """The `--name: var(--tg-x, #value)` fallbacks declared in one CSS block."""
    found = {}
    for name, value in re.findall(r"--([\w-]+):\s*var\(--tg-[\w-]+,\s*(#[0-9a-fA-F]+)\)", block):
        found[name] = value
    return found


def theme_block(selector: str) -> dict[str, str]:
    body = stylesheet()
    start = body.index(selector + " {")
    return fallbacks(body[start : body.index("\n}", start)])


LIGHT = theme_block(":root")
DARK = theme_block('[data-theme="dark"]')


def test_both_themes_declare_the_colours_these_checks_need():
    """Guards against a rename quietly emptying the assertions below."""
    for palette in (LIGHT, DARK):
        assert {"bg", "fg", "muted", "accent", "accent-fg", "link"} <= set(palette)


@pytest.mark.parametrize(("theme", "palette"), [("light", LIGHT), ("dark", DARK)])
def test_the_primary_button_meets_aa(theme: str, palette: dict[str, str]):
    """The regression: this was 3.78:1 in the dark theme."""
    ratio = contrast_ratio(palette["accent-fg"], palette["accent"])

    assert ratio >= MINIMUM_CONTRAST, (
        f"{theme} button text is {ratio:.2f}:1 on its own background, below {MINIMUM_CONTRAST}"
    )


@pytest.mark.parametrize(("theme", "palette"), [("light", LIGHT), ("dark", DARK)])
def test_body_text_meets_aa(theme: str, palette: dict[str, str]):
    ratio = contrast_ratio(palette["fg"], palette["bg"])

    assert ratio >= MINIMUM_CONTRAST, f"{theme} body text is {ratio:.2f}:1"


@pytest.mark.parametrize(("theme", "palette"), [("light", LIGHT), ("dark", DARK)])
def test_hint_text_meets_aa(theme: str, palette: dict[str, str]):
    """Hints carry real instructions — step sizes, word counts — not decoration."""
    ratio = contrast_ratio(palette["muted"], palette["bg"])

    assert ratio >= MINIMUM_CONTRAST, f"{theme} hint text is {ratio:.2f}:1"


@pytest.mark.parametrize(("theme", "palette"), [("light", LIGHT), ("dark", DARK)])
def test_the_focus_ring_is_visible_against_the_page(theme: str, palette: dict[str, str]):
    """A focus indicator nobody can see is not a focus indicator.

    3:1 is the non-text contrast minimum, which is what a ring is judged by.
    """
    ratio = contrast_ratio(palette["link"], palette["bg"])

    assert ratio >= 3.0, f"{theme} focus ring is {ratio:.2f}:1"


def test_the_disabled_state_only_dims_and_never_recolours():
    """Opacity keeps the ratio proportional; a hardcoded grey would not."""
    body = stylesheet()
    start = body.index("button[disabled] {")
    block = body[start : body.index("}", start)]

    assert "opacity" in block
    assert "color:" not in block


def test_the_focus_ring_is_never_removed():
    body = stylesheet()

    assert ":focus-visible" in body
    # `outline: none` appears once, on the card container, which is focused
    # programmatically to move a screen reader rather than by tabbing.
    assert body.count("outline: none") == 1
    assert ".card:focus {\n  outline: none;\n}" in body


def test_the_measured_ratios_are_reported_for_review():
    """Prints the numbers, so a reviewer sees values rather than a green tick."""
    measured = {
        "light button": contrast_ratio(LIGHT["accent-fg"], LIGHT["accent"]),
        "dark button": contrast_ratio(DARK["accent-fg"], DARK["accent"]),
        "light body": contrast_ratio(LIGHT["fg"], LIGHT["bg"]),
        "dark body": contrast_ratio(DARK["fg"], DARK["bg"]),
        "light hint": contrast_ratio(LIGHT["muted"], LIGHT["bg"]),
        "dark hint": contrast_ratio(DARK["muted"], DARK["bg"]),
    }
    print("\n" + "\n".join(f"  {name}: {ratio:.2f}:1" for name, ratio in measured.items()))

    assert min(measured.values()) >= MINIMUM_CONTRAST
