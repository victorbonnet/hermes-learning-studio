# Accessibility

Check every exercise against this before you finalise it. An exercise the
learner cannot operate does not measure their knowledge — it measures their
eyesight, their hearing, or the steadiness of their hand.

Design for the constraint by default rather than waiting to be asked. Most
learners will not volunteer a disability to an assistant, and a discovery
question about it is intrusive. Offering the accessible path as the normal path
costs nothing and asks no one to disclose anything.

## The rules

**Never encode meaning in colour alone.** Anything distinguished by colour must
also be distinguished by label, shape, position, or text. Red/green feedback is
the classic failure: pair it with a word.

**Every image needs alternative text** that conveys what the image
*contributes*. A decorative picture needs none. Crucially: if the answer is in
the image, the alt text must not give it away — describe what is shown, not
what it means. "Cross-section of a leaf with four tissue layers labelled A–D"
works; "leaf showing palisade mesophyll at B" hands over the answer.

**Audio needs a transcript; video needs captions.** The only exception is an
item whose objective *is* listening comprehension — and even then, offer the
transcript after the attempt, and provide unlimited replays.

**Anything draggable must have a non-dragging path.** Keyboard reordering,
numbering the items, or selecting a source then a target. Drag-and-drop is a
motor-precision test that most objectives never intended to include.

**Time limits are opt-in.** Unless speed is part of the objective — automatic
recall, exam simulation — do not impose one. Where speed genuinely matters,
record time as data rather than cutting the learner off.

**Everything must be reachable by keyboard**, in a sensible order, with the
focused element visibly marked.

**Text must survive resizing and reflow**, and must not be delivered as an
image of text.

**Nothing should flash or animate** without a way to stop it.

**Language must be marked** when content and explanation languages differ, so
that a screen reader pronounces each correctly instead of reading Japanese
through an English voice.

## Cognitive and linguistic load

- One question at a time. Long stems hide the actual question.
- Plain instructions in the explanation language, however advanced the content.
- Consistent structure across a set — changing the interaction each item taxes
  working memory for no pedagogical gain.
- Do not make reading speed a hidden variable in a non-reading objective.
- Dyslexia-friendly defaults: generous line spacing, no justified text, no
  long strings of italics.
- Numeracy is not a hidden requirement: do not express a history score as a
  ratio the learner has to convert.

## Feedback

Feedback must be readable by a screen reader in the order it matters: verdict,
then the correct answer, then the explanation. Do not signal correctness only
by a colour change, an icon, or an animation. Do not rely on position — "the
box on the right" means nothing in a linearised reading order.

## When a format cannot be made accessible

Change the format. A hotspot diagram that cannot be operated becomes a
labelled-list selection item; a listening drill for a learner who cannot use
audio becomes a transcript-based reading item, with the objective adjusted and
the change stated plainly. Never quietly downgrade the objective — say what
changed and why, and let the learner decide whether it still serves them.
