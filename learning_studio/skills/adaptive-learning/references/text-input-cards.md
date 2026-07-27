# Text input cards

The learner types the answer. Nothing to recognise, nothing to eliminate.

## When to use

- The objective says **produce, recall, derive, name, or compute.** Free recall
  is the strongest evidence of durable knowledge, and the only honest test of a
  production objective.
- The answer space is small and canonical: a term, a form, a number, a line.
- You want to expose partial knowledge. A near-miss spelling shows the trace is
  there; picking the right option out of four shows nothing of the kind.
- Cloze deletion suits the material — a sentence with one element removed.

## When not to use

- Many phrasings are equally correct and you cannot enumerate them. Ambiguous
  keys punish knowledge, not ignorance.
- The learner is at first exposure and would fail nearly everything. Start with
  [selection-cards](selection-cards.md) and graduate here.
- The answer is an essay or an argument — use
  [reflection-and-rubrics](reflection-and-rubrics.md).
- The objective is ordering or pairing, which typing flattens badly. Use
  [ordering-and-matching](ordering-and-matching.md).
- Typing is a barrier in itself: a long answer on a phone, or a script the
  learner cannot input. Offer a spoken or selected alternative.

## Required fields

- `prompt` — unambiguous about the *form* wanted: which tense, which units,
  how many words.
- `answer` — a list of every accepted form, not a single string.
- `evaluation` — `normalised` for most text, `numeric` with a tolerance and
  units for calculations, `exact` only where the exact string matters.
- Normalisation rules, stated explicitly: case, whitespace, punctuation,
  accents, leading articles.
- `hints` — progressive: nudge, then structure, then near-answer.
- `feedback` — distinguishing "wrong" from "right idea, wrong form".

## Evaluation

Declare normalisation per item, because it is genuinely subject-dependent.
Accents are significant in a Spanish orthography drill and pure noise in a
history date quiz. Case matters for `SELECT` versus a variable name in one
context and not at all in another.

Accept synonyms and spelling variants you can enumerate; a learner who wrote
"mitochondrial matrix" should not fail a key that says "matrix". For numbers,
always give a tolerance and decide whether units are required — then apply it
consistently across the set.

When a response is close but not accepted, say which it was: a typo, a
different valid term you had not listed, or a real error. If you find yourself
rejecting defensible answers repeatedly, the item is wrong, not the learner.
Add the form to the key and say you have done so.

## Accessibility

- Never impose a time limit unless speed is the objective; typing speed varies
  enormously and is rarely what you are measuring.
- Where the content language differs from the input method available — kanji,
  diacritics, mathematical notation — accept a romanised or ASCII fallback and
  say so in the prompt.
- Spelling should not silently gate a non-spelling objective. Accept the
  misspelling, mark the answer correct, and mention the spelling separately.
- Do not require exact whitespace or punctuation from a screen-reader user
  navigating a long field.

## Anti-patterns

- **The under-specified prompt.** "Conjugate `tener`" — in which tense, which
  person? The learner fails on your ambiguity.
- **The single-string key.** One accepted form where three are correct.
- **Trick normalisation.** Rejecting a trailing space.
- **Essay by stealth.** A field that invites three sentences but is graded by
  string match.
- **Hints that hand over the answer** at the first request.
- **Testing recall of something never taught**, then treating the blank as a
  gap rather than as your omission.

## Combinations

- **Two-stage items.** Text input first; on failure, re-ask the same fact as a
  [selection card](selection-cards.md) to distinguish "cannot recall" from
  "does not know".
- Pair with [media-cards](media-cards.md) for transcription and dictation,
  where an audio clip is the stem and typing is the response.
- Promote reliable text-input items into
  [flashcards](flashcards-and-recall.md) for long-term maintenance.
- Use as the free-response step inside a
  [scenario](scenarios-and-simulations.md).

## Examples

**Language learning.** Cloze: a Spanish sentence with the verb removed, prompt
naming tense and person, key listing both accepted spellings. Vocabulary recall
in the production direction — English prompt, Japanese answer — is far harder
than the reverse, and only the production direction tests production.

**Programming.** "Write the regex that matches an integer with an optional
sign." Enumerate the accepted equivalents rather than pretending one canonical
form exists. Or: "What does this recursive call return for n = 0?" — a numeric
key with no tolerance.

**Science.** "Calculate the concentration from this titration: 24.6 mL of
0.100 M NaOH neutralises 25.0 mL of HCl." Numeric evaluation, tolerance
±0.001, units required, and feedback that separates a method error from an
arithmetic slip.

**History.** "Name the treaty that ended the war" accepts the common and formal
names. Note that this is a recall objective; if the real objective was to
explain why it failed, this card is testing the wrong thing.
