# Scenarios and simulations

Multi-step situations where each decision changes the state, and the learner
lives with the consequences of earlier choices.

## When to use

- The objective is **applied judgement** — choosing well under constraints,
  not recalling what is true.
- Order and consequence matter: an early decision should narrow or open what
  comes later.
- The learner already knows the components and needs to integrate them. This is
  a synthesis format, not an introduction.
- Realistic constraints are part of the skill: incomplete information, time
  pressure, competing priorities, irreversible actions.
- You want to surface *reasoning*, since a scenario reveals a flawed model that
  isolated items hide.

## When not to use

- The learner lacks the components. A scenario built on knowledge they do not
  have teaches helplessness — build the pieces first with
  [selection](selection-cards.md) and [text input](text-input-cards.md).
- The situation has one obviously right path, which makes it a procedure. Use
  [timelines-and-processes](timelines-and-processes.md).
- The objective is factual recall.
- Time is short. Scenarios are expensive to build and expensive to complete.
- The realistic version would require simulating something you cannot simulate
  honestly. Do not fake system output the learner might believe.

## Required fields

- `payload.situation` — the opening state, including what the learner does
  **not** know.
- `payload.steps` — each with the available actions and the state each produces.
- `payload.state` — the variables that change: resources, time, trust, risk.
- `answer` — per step, or a set of acceptable paths where several are
  defensible.
- `evaluation` — path-based or `rubric`; state which.
- `feedback` — consequence-based, delivered when the consequence lands rather
  than at the moment of choice.
- `terminal_states` — how it can end, including the ways it can end badly.

## Evaluation

Score the **path**, not just the destination. A learner who reaches a good
outcome by luck after two poor decisions has not demonstrated the objective,
and one who reasons well into a bad outcome may have.

Where several paths are defensible, say so and evaluate against a rubric — see
[reflection-and-rubrics](reflection-and-rubrics.md). Manufacturing a single
"correct" path through a genuinely ambiguous situation teaches compliance
rather than judgement.

Let consequences do the teaching. Interrupting each choice with a verdict
converts the scenario into a series of selection items and destroys the format.
Debrief at the end: what happened, why, and what the alternative branch would
have produced.

Ask for the reasoning at least once. The choice alone under-determines whether
the model behind it is sound.

## Accessibility

- State the current state in **text** at every step; never rely on a visual
  dashboard alone.
- Keep the cognitive load in the decision, not in tracking what has happened —
  provide a running summary of prior choices.
- Do not impose time pressure unless it is genuinely part of the objective;
  where it is, say so up front and offer an untimed variant.
- Allow the learner to pause and resume without losing state, and to abandon
  without penalty.
- Never encode risk or status by colour alone. See
  [accessibility](accessibility.md).

## Anti-patterns

- **The railroad.** Branches that reconverge immediately, so choices are
  cosmetic.
- **Gotcha branches.** A catastrophic outcome from a decision no reasonable
  learner could have evaluated with the information given.
- **Hidden state** that makes the outcome unpredictable rather than uncertain.
- **Fabricated system output.** Never invent a command result, a log, or a data
  value and present it as real; label simulated output as simulated.
- **Ten-step scenarios** where the learner has forgotten step 2 by step 8.
- **Moralised endings** that lecture rather than show the consequence.
- **Simulated authority.** Do not put words in the mouth of a real named
  person or institution.

## Combinations

- Open with a [media card](media-cards.md) as the presenting evidence.
- Use [selection cards](selection-cards.md) for the individual decision points
  and [text input](text-input-cards.md) for the reasoning behind one of them.
- Debrief with [reflection-and-rubrics](reflection-and-rubrics.md).
- Follow a failed branch with
  [timelines-and-processes](timelines-and-processes.md) to rebuild the
  procedure that would have worked.

## Examples

**Programming.** A production incident: latency has tripled, and the learner
chooses what to inspect first. Reading the query plan early opens a path;
restarting the service early destroys the evidence and closes it. State
includes elapsed time and remaining diagnostic options. The debrief compares
branches rather than declaring one right. A second scenario: a recursive
function overflows the stack in production — reproduce, bound, or rewrite?

**Science.** Run a titration where the learner chooses indicator, aliquot size,
and endpoint criterion; an early wrong indicator makes a later reading
ambiguous, and that ambiguity is the lesson. Or: design an experiment to test
whether an enzyme is denatured or inhibited, with limited reagent.

**History.** A source-evaluation scenario: three documents about the same
Ottoman reform, with provenance revealed progressively. The learner decides
what to trust and revises as provenance emerges. Judgement under incomplete
information is exactly the historian's skill, and there is no single key.

**Language learning.** A service encounter conducted in Spanish where register
choices affect how the interlocutor responds — an over-familiar opening changes
the rest of the conversation, which is precisely the point.
