# Flashcards and recall

Retrieval practice over time: a prompt, an attempt, a reveal, and a self-graded
verdict that decides when the item comes back.

## When to use

- The objective needs **durable, automatic recall** — facts that must be
  available without deliberation.
- The volume is large and the material is atomic: vocabulary, terminology,
  symbols, formulae, dates.
- The learner has a long horizon. Spacing pays off over weeks, not in one
  evening.
- Speed matters. An answer that takes fifteen seconds to reconstruct is not
  yet fluent, even when it is correct.

## When not to use

- The material is **understanding, not retrieval.** Flashcarding "why did the
  revolution succeed" produces a memorised paragraph and no comprehension.
- The item only makes sense in context. Isolated cards strip the context that
  gave the fact meaning.
- The horizon is tonight. Cramming with spaced repetition wastes the mechanism;
  use massed practice and say that is what you are doing.
- The learner needs to *apply* rather than recall — use
  [scenarios-and-simulations](scenarios-and-simulations.md).
- The answer is long. If it does not fit on one side of a card, it is two cards
  or the wrong format.

## Required fields

- `prompt` — the front, testing one thing.
- `answer` — the back, minimal and unambiguous.
- `direction` — recognition or production, and whether the card is reversible.
- `scheduling` — the interval state, when a runtime carries it.
- `context` — optional, revealed with the answer, restoring what isolation
  removed.
- `tags` — for interleaving related cards and for reporting.

## Evaluation

Flashcards are **self-graded**, and the grade drives the schedule:

| Verdict | Meaning | Next interval |
| --- | --- | --- |
| Failed | Could not retrieve | Reset — same session |
| Hard | Retrieved slowly or partially | Shorten |
| Good | Retrieved correctly | Expand |
| Easy | Immediate and automatic | Expand sharply |

Self-grading is only honest if the learner attempted retrieval *before* the
reveal. Say this explicitly: showing the answer first destroys the effect that
makes the technique work.

A rough default: one day, three days, a week, two weeks, a month; halve after a
failure. Interleave tags rather than blocking by topic — mixing enzyme cards
with membrane-transport cards teaches discrimination that blocked practice
never does.

**Today there is no scheduler.** Nothing persists between sessions. Give the
learner the schedule as advice and tell them plainly they must keep it
themselves. Never imply a review will be delivered to them.

## Accessibility

- No time pressure by default. Where speed is the objective, record elapsed
  time as data rather than cutting the learner off.
- The reveal must be explicit and keyboard-operable, never a hover.
- Mark the language of each side when they differ, so a screen reader
  pronounces each correctly.
- Cards carrying audio need a transcript after the attempt; cards carrying
  images need alt text that does not leak the answer. See
  [accessibility](accessibility.md).
- Offer a spoken or selected response path where typing the script is
  impractical.

## Anti-patterns

- **The paragraph card.** A back the learner reads instead of retrieves.
- **Two facts on one card.** Which one failed?
- **Answer-first review**, which converts retrieval into recognition.
- **Orphan cards.** A term with no context, learned as a sound rather than a
  meaning.
- **One-directional vocabulary.** Recognition without production leaves the
  learner able to read and unable to speak.
- **Ever-growing decks.** Adding faster than reviewing guarantees collapse; cap
  new items per session.
- **Cramming disguised as spacing.** Five reviews in one evening is one review.

## Combinations

- Convert failed [selection-card](selection-cards.md) items into flashcards for
  the rest of the session.
- Promote [text-input](text-input-cards.md) items that the learner answers
  reliably into the maintenance deck.
- Pair with [media-cards](media-cards.md) for audio-front vocabulary, which
  trains listening and recall together.
- Precede with [ordering-and-matching](ordering-and-matching.md) to build the
  recognition that recall then converts.

## Examples

**Language learning.** Vocabulary in both directions, with the production
direction scheduled more aggressively because it is harder. Kanji cards
splitting reading and meaning into separate items, since failing one is not
failing the other. Conjugation cards, one form per card.

**Science.** Enzyme to its substrate; the stages of mitosis as separate cards
with a context line restoring the cell-cycle position; Newton's laws as
statements to reproduce rather than paragraphs to reread. Unit conversions for
titration arithmetic, where automaticity genuinely matters.

**History.** Dates and actors as cards — but only where the objective really is
recall. If the objective is explaining why the Ottoman reforms stalled, this is
the wrong card and a [reflection](reflection-and-rubrics.md) item is right.

**Programming.** Language built-ins, regex metacharacters, SQL clause order.
Not "how does recursion work", which is understanding and belongs elsewhere.
