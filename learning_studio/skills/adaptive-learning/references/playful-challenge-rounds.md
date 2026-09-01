# Playful challenge rounds

A *round* is a short exercise — 4–6 cards in one prepared experience — whose
cards were chosen together rather than one at a time. The point is engagement
through shape: a beginning, a turn, and an end the learner can feel, instead of
a flat list of twelve items of the same type.

Nothing here is a feature. These blueprints add no new schema field, no new
tool, and no new runtime mode; they are ways of arranging the existing
component types described in [manifest-contract](manifest-contract.md). If a
round needs something the manifest does not already accept, the round is wrong,
not the manifest.

## Principles

**The objective's observable verb chooses the formats.** Read it first, then
open the one or two card references it points at. A round that opens with a
blueprint and looks for content to fill it has already inverted the design.

**Variety is a means, never a quota.** Reach for a second or third interaction
family only when the objective genuinely asks for distinct operations —
recognising *and* producing, ordering *and* justifying. Never insert an
ill-fitting card merely to reach a count, and never add a card just to make up
a count of families. Four well-aimed cards beat six that wander.

**A recognition card cannot assess a production objective.** This survives
every blueprint below. If the objective says the learner must produce the
preterite form, a `multiple_choice` card that offers four of them is a warm-up
at best; the assessment has to be `short_answer`, `translation`, or another
production type.

**Difficulty rises inside the round, not across it.** The last card should be
the one worth getting right.

## Blueprint: Quick Mix

**Shape.** 4–6 cards moving through the operations the objective actually
needs, in this order where all of them apply:

1. *Warm-up (optional).* A recognition card — `multiple_choice`, `true_false`,
   `image_choice` — only when the material is new enough that the learner needs
   to find their footing. Skip this card entirely when the objective is already
   a production objective and the learner knows the ground.
2. *Relation or sequence.* `matching`, `categorization`, `sequence_order`,
   `timeline` — the operation that shows structure rather than facts.
3. *Production.* `short_answer`, `fill_blank`, `translation`,
   `error_correction`, `code_response` — the learner makes the answer.
4. *Application.* `scenario_choice`, `case_study`, `self_explanation` — the
   same knowledge used somewhere it was not taught.

**Not a checklist.** Omit any stage the objective does not call for. An
objective about discriminating two enzyme mechanisms may be two recognition
cards and one production card and nothing else; an objective about writing a
regex needs no recognition stage at all. Repeating one family across the whole
round is a legitimate Quick Mix when the objective is genuinely one operation.

## Blueprint: Story Mission

4–6 sequential cards sharing one fictional or realistic setting — a
lab notebook, a code review, an archive of primary sources, or a conversation in
the target language — moving from evidence to a decision to an explanation, and
ending on a capstone where that is pedagogically valid.

A workable order, when the objective supports all four beats:

1. *Evidence or context.* `image_observation`, `table_grid`, or a passage in a
   `fill_blank` card that establishes the situation.
2. *Decision.* `scenario_choice` or `decision_path`.
3. *Explanation.* `self_explanation` or `short_answer` — why that decision.
4. *Capstone.* `case_study`, `free_response`, or a production card that puts
   the whole thing together.

**It is sequential, and only sequential.** The cards run in the fixed order you
write them. The Mini App session walks that fixed ordered plan: it does not
execute branch targets, it does not carry mutable simulated state between
cards, and it does not route the learner down a different path because of an
earlier answer. `evaluation.branching` validates, and the validator will reject
an unreachable target, but validating is not executing.

So write the setting as continuity, not as consequence. "The reaction has now
been running for ten minutes" is fine — you wrote both cards and you know the
order. "Because you chose to add the acid, the flask has overflowed" is not:
the next card is shown whatever was answered. A `decision_path` component is
one card that asks for a route, not proof that the session took it.

## Blueprint: Recall Sprint

**Shape.** 4–6 atomic retrieval items using the recall formats that already
exist — `flashcard` and `typed_recall`, or `short_answer` where you want the
answer marked rather than self-graded. One fact per card, minimal answers, and
tags that interleave rather than block by topic. See
[flashcards-and-recall](flashcards-and-recall.md).

**"Sprint" is the shape, not a stopwatch.** There is no timer and no
countdown; do not describe one, and do not add time pressure unless speed or
automatic recall is part of the stated objective. Where it genuinely is, say so
in the objective and treat elapsed time as data you discuss, never as a cut-off
that ends the card.

**What actually happens afterwards.** A completed tracked attempt updates
durable SM-2 objective review state, and `learning_studio_review_plan` reports
which objectives are due now and which are coming up. What does *not* happen:
nothing is requeued inside the current exercise — its plan is fixed at
preparation time — and this plugin never sends a reminder on its own. A missed
item comes back because you build it into a later round, not because the
session noticed. Promise the learner neither.

## Choosing between them

| The objective wants… | Blueprint |
| --- | --- |
| Several distinct operations on one piece of knowledge | Quick Mix |
| Judgement exercised in a setting, with reasons | Story Mission |
| Durable, automatic recall of atomic items | Recall Sprint |
| One operation, done thoroughly | No blueprint — a plain set of cards |

## What a round never adds

A round changes which cards you write, and nothing else. It adds no points
economy, no daily streak, no leaderboard, no countdown timer, no sound, no
per-card correctness interstitial, and no extra confirmation step.
The learner advances immediately after a confirmed submission, and scoring
arrives at the end of the block — do not design a round that depends on
interrupting either.

Honour the learner's assessment preference. If they have said they do not want
scores, respect that: run the round, discuss what happened, and leave the
fraction out of what you say back.

## Accessibility

Everything in [accessibility](accessibility.md) applies unchanged, and a
"playful" framing is the most common way it gets lost:

- Every card is keyboard-operable. A round that only works by dragging is a
  round somebody cannot take.
- Reduced motion is respected; no round depends on animation to be legible.
- Colour is never the only carrier of meaning — not for a setting, not for a
  progress cue, not for feedback.
- No time pressure unless the objective genuinely asks for speed.
- Text alternatives stay honest: alt text describes the image without leaking
  the answer, and audio carries a transcript after the attempt.
- A narrative setting must not become a comprehension tax. Keep the framing
  short, plain, and in the learner's language.

## Anti-patterns

- **Blueprint first, objective second.** Choosing Story Mission because it
  sounds engaging, then hunting for a decision to put in it.
- **The quota card.** A `matching` item wedged into a Quick Mix so the round
  has three families.
- **Fake consequences.** Story text that claims a later card changed because of
  an earlier answer.
- **The disguised stopwatch.** "See how fast you can go" on an objective that
  never mentioned speed.
- **The requeue promise.** "Anything you miss will come back later in this
  round." It will not.
- **Theme over content.** A setting so elaborate the learner is decoding the
  story instead of the subject.
- **Six cards because six is the maximum.** The count is a ceiling, not a
  target.

## Examples

**Language learning.** *Quick Mix* on the Spanish preterite: recognise the
irregular stem, match persons to endings, then produce three conjugated forms —
no application card, because the objective stops at production. *Story
Mission*: a traveller's day in five cards, each one a short exchange, ending
with the learner writing a reply themselves.

**Programming.** *Quick Mix* on recursion: order the lines of a base case,
write the recursive call, then explain what a missing base case does. *Recall
Sprint* on regex metacharacters, six `typed_recall` items, no timer. *Story
Mission* as a code review — read the diff, choose the defect, justify it, then
write the fix.

**History.** *Story Mission* in an archive: read two conflicting accounts of a
treaty negotiation, choose which to trust, explain the criterion used, then
write a short paragraph weighing both. *Quick Mix* on the Meiji reforms:
sequence four events, then explain the causal link between two of them.

**Science.** *Quick Mix* on titration: read the curve, order the procedure,
compute the concentration. *Recall Sprint* on the stages of mitosis, one stage
per card with a context line restoring the cell-cycle position. *Story Mission*
in a lab: observe a photosynthesis result, decide which variable explains it,
justify, then design the follow-up.

## After the round

Read the results with step 7 of the skill workflow, then change exactly one
thing and say why — the difficulty, the blueprint, the scope, or stopping. A
round that went well is not a reason to make the next one longer, and a round
that went badly is more often one ill-fitting card than a wrong blueprint. If
the objective is met, retire it and let the review plan bring it back.
