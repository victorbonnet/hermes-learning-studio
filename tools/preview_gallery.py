"""Generate a deterministic renderer gallery, for review and screenshots.

A development tool. It is not part of the plugin, it is not packaged, and nothing
at runtime imports it.

What it produces is a self-contained directory holding one card for every
component type in the registry, rendered by the *shipped* ``renderers.js`` and
``i18n.js`` against payloads built by the *shipped* ``build_component`` — so it
shows what a learner would actually see rather than a hand-written mock-up of it.
There is no server, no session, and no network: the page loads no application
code, makes no request, and gets its images from a generated placeholder.

That last point is why this exists in this form. A screenshot of a real session
would be a screenshot of somebody's exercise, taken through a tunnel, with a
Telegram account behind it; this is the same rendering with none of that
attached, which makes it safe to commit and reproducible by anyone.

Usage::

    uv run python tools/preview_gallery.py --out dist/preview
    # then open dist/preview/index.html, or point a browser tool at it

The exercise content comes from ``tests/component_examples.py``, whose subjects
are deliberately unrelated to each other. Nothing here reads a profile, a
database, or an environment variable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning_studio.components import COMPONENT_TYPES, build_component  # noqa: E402
from learning_studio.web.static_files import STATIC_DIR  # noqa: E402
from tests.component_examples import example  # noqa: E402

#: Copied beside the generated page. Note the absence of ``app.js``: the gallery
#: exercises the renderers, and the application is the part that needs a server,
#: a session, and Telegram.
COPIED = ("app.css", "i18n.js", "renderers.js")

#: A placeholder for every managed asset, so a visual card has something to show.
#: An SVG data URL rather than a file: it needs no image library, it is the same
#: bytes on every machine, and it is obviously not real content.
PLACEHOLDER = "data:image/svg+xml;utf8," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200" role="img">'
    '<rect width="320" height="200" fill="#dfe5ec"/>'
    '<path d="M0 0h320v200H0z" fill="none" stroke="#9aa8b6" stroke-width="2"/>'
    '<circle cx="96" cy="76" r="26" fill="#9aa8b6"/>'
    '<path d="M40 168l64-58 40 34 44-40 92 64z" fill="#b6c1cc"/>'
    '<text x="160" y="192" font-family="sans-serif" font-size="13" fill="#5b6773" '
    'text-anchor="middle">placeholder image</text>'
    "</svg>"
)

PAGE = """<!doctype html>
<html lang="en" data-theme="{theme}">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>Learning Studio — renderer gallery ({theme})</title>
    <link rel="stylesheet" href="app.css" />
    <link rel="stylesheet" href="gallery.css" />
    <script src="i18n.js" defer></script>
    <script src="renderers.js" defer></script>
    <script src="fixtures.js" defer></script>
    <script src="gallery.js" defer></script>
  </head>
  <body>
    <main id="gallery"></main>
  </body>
</html>
"""

GALLERY_CSS = """/* Gallery chrome only. Every card below is styled by the shipped app.css. */
#gallery {
  display: flex;
  flex-direction: column;
  gap: 24px;
  padding: 16px;
  max-width: 420px;
  margin: 0 auto;
}

.preview-card {
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  background: var(--bg);
}

.preview-label {
  margin: 0;
  padding: 8px 12px;
  background: var(--surface);
  color: var(--muted);
  font: 600 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
}

.preview-body {
  padding: 12px;
}

.preview-actions {
  padding: 0 12px 12px;
}
"""

GALLERY_JS = """/* Render one card per fixture, with the real renderers and no application. */
(function () {
  "use strict";

  var t = window.LearningStudioI18n.translator(window.PREVIEW_LOCALE || "en");
  var gallery = document.getElementById("gallery");

  var context = {
    t: t,
    loadImage: function () {
      return Promise.resolve(window.PREVIEW_PLACEHOLDER);
    },
  };

  window.PREVIEW_FIXTURES.forEach(function (component) {
    var card = window.LearningStudioRenderers.render(component, context);

    var label = document.createElement("p");
    label.className = "preview-label";
    label.textContent = component.type;

    var body = document.createElement("div");
    body.className = "preview-body";
    body.appendChild(card.element);

    var actions = document.createElement("div");
    actions.className = "preview-actions";
    var submit = document.createElement("button");
    submit.className = "primary";
    submit.type = "button";
    submit.textContent = t("action.submit");
    actions.appendChild(submit);

    var wrapper = document.createElement("section");
    wrapper.className = "preview-card";
    wrapper.id = "preview-" + component.type;
    wrapper.appendChild(label);
    wrapper.appendChild(body);
    wrapper.appendChild(actions);
    gallery.appendChild(wrapper);
  });
})();
"""


def fixtures(types: list[str]) -> list[dict]:
    """API-shaped components, built through the real validator."""
    built = []
    for position, component_type in enumerate(types):
        component = build_component(example(component_type), f"components[{position}]")
        built.append(
            {
                "position": position,
                "component_id": f"preview-{position}",
                "type": component_type,
                "payload": component.learner_payload(),
            }
        )
    return built


def generate(out: Path, *, locale: str, types: list[str]) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    for name in COPIED:
        shutil.copyfile(STATIC_DIR / name, out / name)
    (out / "gallery.css").write_text(GALLERY_CSS, encoding="utf-8")
    (out / "gallery.js").write_text(GALLERY_JS, encoding="utf-8")
    (out / "fixtures.js").write_text(
        "window.PREVIEW_FIXTURES = "
        + json.dumps(fixtures(types), indent=2, ensure_ascii=False)
        + ";\nwindow.PREVIEW_LOCALE = "
        + json.dumps(locale)
        + ";\nwindow.PREVIEW_PLACEHOLDER = "
        + json.dumps(PLACEHOLDER)
        + ";\n",
        encoding="utf-8",
    )
    for theme in ("light", "dark"):
        name = "index.html" if theme == "light" else "dark.html"
        (out / name).write_text(PAGE.format(theme=theme), encoding="utf-8")
    return out / "index.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "dist" / "preview")
    parser.add_argument("--locale", default="en", help="UI locale for the interface strings")
    parser.add_argument(
        "--types",
        nargs="*",
        default=list(COMPONENT_TYPES),
        help="component types to render (default: all of them)",
    )
    args = parser.parse_args()

    unknown = [name for name in args.types if name not in COMPONENT_TYPES]
    if unknown:
        parser.error(f"unknown component type(s): {', '.join(unknown)}")

    page = generate(args.out, locale=args.locale, types=list(args.types))
    print(f"wrote {page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
