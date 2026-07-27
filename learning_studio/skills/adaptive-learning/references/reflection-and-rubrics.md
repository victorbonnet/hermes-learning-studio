# Reflection and rubrics

Open work judged against named criteria: explanations, arguments, designs,
critiques, and self-assessment.

## When to use

- The objective is **explanation, argument, synthesis, or critique** — where
  the reasoning is the thing being assessed and no key can capture it.
- Several answers are defensible and the quality lies in the justification.
- Self-explanation is the pedagogy: articulating a model exposes gaps that
  recognition items hide entirely.
- The learner needs to internalise the standard, not just meet it. A rubric
  handed over in advance teaches what good looks like.
- Metacognition is the goal: what do you find hard, and why?

## When not to use

- A determinate answer exists. Never dress a factual question as an open one;
  it wastes the learner's effort and yields a vague verdict where a clean one
  was available.
- The learner is at first exposure and has nothing to reason with yet.
- Volume matters. One rubric-scored item costs more than twenty
  [flashcards](flashcards-and-recall.md), and both have their place.
- You cannot state the criteria. If you cannot say what distinguishes a strong
  answer from an adequate one, you cannot grade fairly and should not pretend
  to.

## Required fields

- `prompt` — the task, with scope: length, audience, and what to assume known.
- `rubric.criteria` — each named, with what it assesses.
- `rubric.levels` — observable descriptors per level, not adjectives. "Names
  two causes and links each to an outcome" beats "good understanding".
- `evaluation` — `rubric`, with the weighting stated.
- `exemplar` — optional, revealed after the attempt.
- `self_assessment` — whether the learner scores themselves first.

## Evaluation

Judge against the criteria and nothing else. Drifting into what you would have
written is the standard failure mode, and it punishes learners for making
different defensible choices.

Score each criterion separately. A single overall number tells the learner
nothing about what to change, which is the only reason to run this format.

Quote the learner's own words when you justify a level — "you named the cause
but not the mechanism connecting it" is actionable; "needs more depth" is not.

Have the learner self-assess before you respond, then compare. The gap between
their judgement and yours is often more informative than the score, and closing
it is a genuine objective in its own right.

Separate content from expression, and be explicit about which you are judging.
Marking a second-language learner down for phrasing when the objective was
historical reasoning measures the wrong thing entirely.

Where you are uncertain, say so. A confident grade on a contested claim is
worse than an honest "this is defensible; here is the counter-argument".

## Accessibility

- Provide the rubric **before** the attempt, in the explanation language, in
  plain terms. A hidden standard is a trick.
- Accept the response in whatever modality works — typed, dictated, bullet
  points — unless the form is itself the objective.
- Never impose a time limit on open work without a stated reason.
- Feedback must be readable in a linear order: criterion, level, evidence,
  next step.
- Do not require length for its own sake; verbosity is not a disability-neutral
  requirement. See [accessibility](accessibility.md).

## Anti-patterns

- **The invisible rubric**, applied only after the fact.
- **Adjective levels.** "Excellent / good / fair" with no observable
  descriptors means each grade is a mood.
- **Grading style as substance.**
- **The single number**, which cannot be acted on.
- **Rewriting instead of assessing.** Producing a model answer and grading the
  distance from it teaches conformity.
- **Praise inflation.** "Great job!" on weak work is a disservice; name what
  worked and what did not.
- **Contested claims graded as settled.**
- **Open prompts on factual objectives**, which is a determinate question in
  disguise.

## Combinations

- Follow a [scenario](scenarios-and-simulations.md) with a rubric-scored
  debrief on the reasoning.
- Follow a wrong [selection](selection-cards.md) with "explain why the option
  you chose is wrong" — self-explanation converts an error into understanding.
- Use after [tables-and-grids](tables-and-grids.md) to ask what the pattern in
  the grid implies.
- Precede [flashcards](flashcards-and-recall.md) with a reflection on which
  items keep failing and why, which is usually more useful than more reviews.

## Examples

**History.** "Assess how far the unequal treaties caused the Meiji
Restoration." Criteria: uses evidence, weighs alternative causes, distinguishes
sequence from causation, reaches a supported judgement. There is no key here —
the argument is the assessed object, and a well-defended minority position
should score highly.

**Programming.** "Critique this recursive implementation: correctness,
termination, complexity, readability." Named criteria, and explicit that a
different valid design is not an error. Or: explain in your own words why this
regex is exponential on a crafted input.

**Language learning.** A short written response in Spanish, graded on
communicative success, register, and grammatical control as **separate**
criteria — so a learner who communicated well with imperfect grammar sees both
results rather than one muddied verdict.

**Science.** "Explain why the reaction rate falls above 60 °C." Criteria:
identifies denaturation, links structure to function, uses the enzyme
terminology correctly. Self-assessment first, then comparison — the gap is
where the metacognitive learning happens.
