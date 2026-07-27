# Selection cards

The learner chooses from options you provide: one answer, several answers, or
a binary judgement.

## When to use

- The objective is **recognition or discrimination** — telling apart things
  that are easy to confuse.
- Plausible wrong answers exist and are themselves informative. A distractor
  the learner picks tells you which misconception they hold.
- You need fast, unambiguous scoring over many items.
- The learner is at first exposure, where producing an answer unaided would
  fail so often it teaches nothing.
- The answer space is genuinely closed: a compiler either warns or it does not.

## When not to use

- The objective says **produce, derive, recall, or explain.** Recognising the
  correct preterite form among four is a different skill from producing it, and
  passing the first says little about the second. Use
  [text-input-cards](text-input-cards.md).
- There is no honest distractor. Padding with obviously wrong options makes the
  item free.
- The set is a sequence, a pairing, or a classification — those are
  [ordering-and-matching](ordering-and-matching.md).
- The material is contested and every option is arguable. Move to
  [reflection-and-rubrics](reflection-and-rubrics.md).
- True/false on a nuanced historical claim. Binary items on interpretive
  material teach that history has yes/no answers.

## Required fields

- `prompt` — a complete question, answerable before the options are read.
- `payload.options` — 3–5 for single select; each independently plausible.
- `answer` — the option or set of options.
- `evaluation` — `exact` for single select; `set` for multi-select, with a
  stated partial-credit rule.
- `feedback` — per distractor wherever the distractor encodes a misconception.
- For multi-select: state **how many** are correct, or explicitly that the
  number is not given.

## Evaluation

Single select is exact match. Multi-select needs a declared rule: all-or-
nothing is harsh and common; per-option credit with a penalty for wrong
selections is fairer but must be stated up front. Never leave it implicit —
inconsistent scoring across a set reads as unfairness.

Guessing is real: four options give 25% for free. Read a 60% score on
four-option items as barely above chance, not as partial mastery. Follow a
correct selection with "why?" when it matters.

## Accessibility

- Options must be selectable and navigable by keyboard, and labelled by text
  rather than by colour or position.
- Never write "all of the above" or "none of the above" — they break under
  shuffling and under linearised screen-reader reading.
- Keep option lengths similar; the longest option is a well-known unintended
  tell.
- If an option carries an image, it needs alt text that does not give the
  answer away. See [accessibility](accessibility.md).

## Anti-patterns

- **The giveaway.** The correct option is longer, more hedged, or more
  technical than the others.
- **Grammatical leakage.** The stem ends "an…" and only one option starts with
  a vowel.
- **Negative stems.** "Which is NOT true?" tests reading care, not knowledge.
- **Absolutes as distractors.** "Always" and "never" options are reflexively
  discarded by test-wise learners.
- **Stacked options.** Overlapping options where two are defensible.
- **Fixed answer position.** Always placing the key second is learnable.
- **Trivia dressed as assessment.** Testing the year of a treaty when the
  objective is why it failed.

## Combinations

- Use as a **diagnostic opener**, then switch to text input once the learner is
  reliably above chance.
- Pair with [media-cards](media-cards.md) when the stem is an image or an
  audio clip and the response is a choice.
- Follow a wrong selection with a [flashcard](flashcards-and-recall.md) on the
  same fact to force retrieval later in the session.
- Use as the **step** inside a [scenario](scenarios-and-simulations.md), where
  each choice changes the state.

## Examples

**Programming.** "Given this function, what does `f(4)` return?" with options
covering the off-by-one, the missing base case, and the correct value — each
distractor is a specific bug the learner might hold. Or: which of these regex
patterns matches an empty string?

**Science.** "A titration overshoots the endpoint by 0.5 mL. What happens to
the calculated concentration?" Options: too high, too low, unchanged, depends
on the indicator — each is a real misconception about the arithmetic.

**Language learning.** Which sentence uses the subjunctive correctly? Four
Spanish sentences differing only in the verb form. Discrimination is exactly
the objective here, so a selection card is the right instrument.

**History.** Multi-select: which of these were consequences of the Ottoman land
reforms? Three of six, with the number given, and feedback naming why each
rejected option belongs to a different period.
