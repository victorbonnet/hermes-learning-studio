# Screenshots

The images below were captured at 414×896 (a phone viewport) from the
deterministic renderer gallery, which anyone can regenerate:

```bash
uv run python tools/preview_gallery.py --out dist/preview \
  --types multiple_choice fill_blank matching hotspot image_observation confidence_rating
uv run python tools/preview_gallery.py --out dist/preview-fr --locale fr --content-locale ja \
  --types flashcard timeline sequence_order reflection multiple_choice
# then open dist/preview/index.html (light) or dist/preview/dark.html
```

The gallery renders the **shipped** `renderers.js`, `i18n.js`, and `app.css`
against payloads built by the **shipped** `build_component`, so what is pictured
is what a learner sees — not a mock-up of it.

Nothing in any of them is real. The exercise content comes from
`tests/component_examples.py`, whose subjects are deliberately unrelated to one
another; images are a generated SVG placeholder, because a real managed asset
belongs to a real learner. No session, no server, no tunnel, and no Telegram
account is involved in producing these.

| File | What it shows |
| --- | --- |
| `cards-light.png` | Light theme, English UI: single choice, cloze, matching, hotspot, image observation, confidence rating |
| `cards-dark-fr.png` | Dark theme, **French UI with English content**: the flashcard's *Turn the card over* control, and two ordering cards visibly *not* in their correct order |
| `image-fallback.png` | What a learner sees when a managed image cannot be loaded: the exercise's own `alt_text`, shown as text rather than promised and withheld |

The second image carries three claims worth checking by eye:

- the interface is French while the exercise stays in the language it was written
  in — that distinction is the whole reason `i18n.js` exists;
- the flashcard has an explicit, keyboard-operable **Retourner la carte**. It
  uncovers nothing that was already in the page: the back of the card is not
  there, and pressing it asks the server;
- the `timeline` and `sequence_order` cards are in an arrangement that is *not*
  the answer, and the timeline shows no dates because its `show_dates` is false.
  Both are done while building the learner projection, server-side.
