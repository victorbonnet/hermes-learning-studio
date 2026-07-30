# Screenshots

Both images below were captured at 414×896 (a phone viewport) from the
deterministic renderer gallery, which anyone can regenerate:

```bash
uv run python tools/preview_gallery.py --out dist/preview \
  --types multiple_choice fill_blank matching hotspot image_observation confidence_rating
uv run python tools/preview_gallery.py --out dist/preview-fr --locale fr \
  --types multiple_choice fill_blank matching flashcard timeline reflection
# then open dist/preview/index.html (light) or dist/preview/dark.html
```

The gallery renders the **shipped** `renderers.js`, `i18n.js`, and `app.css`
against payloads built by the **shipped** `build_component`, so what is pictured
is what a learner sees — not a mock-up of it.

Nothing in either image is real. The exercise content comes from
`tests/component_examples.py`, whose subjects are deliberately unrelated to one
another; images are a generated SVG placeholder, because a real managed asset
belongs to a real learner. No session, no server, no tunnel, and no Telegram
account is involved in producing these.

| File | What it shows |
| --- | --- |
| `cards-light.png` | Light theme, English UI: single choice, cloze, matching, hotspot, image observation, confidence rating |
| `cards-dark-fr.png` | Dark theme, French UI: the same interface localized, with flashcard, timeline ordering, and reflection |

The second image is the point of having two: the interface strings are French
while the exercise content stays in the language it was written in, which is the
whole distinction `i18n.js` exists to make.
