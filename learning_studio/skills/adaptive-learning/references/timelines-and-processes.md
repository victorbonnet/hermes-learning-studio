# Timelines and processes

Sequences where the ordering carries causal or procedural meaning, and where
each step changes the state the next step acts on.

## When to use

- The objective is **chronology with consequence** — not just what happened
  when, but what each event made possible.
- The material is a **procedure with state**: each step transforms something,
  and skipping one invalidates the rest.
- Duration, overlap, or simultaneity matters. Two things happening at once is
  frequently the point.
- The learner needs to distinguish *sequence* from *causation*, which is the
  single most common confusion in any historical or diagnostic material.

## When not to use

- The order is arbitrary. Imposing a sequence on unordered material teaches a
  false structure.
- It is a flat list to sort with no state carried between items — plain
  [ordering](ordering-and-matching.md) is simpler and scores better.
- The sequence is disputed. Where the causal chain is genuinely contested,
  move to [reflection-and-rubrics](reflection-and-rubrics.md) and assess the
  argument instead of a key.
- The learner does not yet know the events. Sequencing unknown items is
  guessing; teach first.

## Required fields

- `payload.steps` — each with a label, and where relevant a date, a duration,
  and the state it produces.
- `answer` — the correct sequence, or the step-to-position map.
- `evaluation` — `ordered`, with a stated partial-credit rule.
- `dependencies` — which steps genuinely require which others, distinguished
  from steps that merely happen to follow.
- `granularity` — the level of detail expected, stated in the prompt.
- Where simultaneity exists: which positions are interchangeable, so parallel
  events are not marked wrong.

## Evaluation

Never score a ten-step chain all-or-nothing. Use a distance measure: adjacent
transpositions cost little, a block moved wholesale costs more. Credit each
step in a correct relative position even when the absolute positions shift.

Interchangeable steps must be declared. Marking a learner wrong for ordering
two simultaneous events differently is your error.

Read the failure pattern: a learner who has the sequence right but the
dependencies wrong has memorised chronology without mechanism. Follow up with
"what made step 4 possible?" as a [text input](text-input-cards.md) — that is
where the understanding lives.

## Accessibility

- Timelines must be readable as a **linear list**, not only as a graphic. A
  horizontal visual timeline is unusable with a screen reader unless the same
  data exists as text.
- Every reordering interaction needs a keyboard path, with position changes
  announced.
- Never encode phases by colour alone; label them.
- Dates need an unambiguous format, and eras or calendars must be stated —
  Meiji years and Gregorian years are not interchangeable.
- Do not require horizontal scrolling to compare two ends of a sequence.
- See [accessibility](accessibility.md).

## Anti-patterns

- **Sequence passed off as causation.** "B followed A" is not "A caused B",
  and an item that scores the first while claiming the second is teaching a
  fallacy.
- **False precision.** Demanding exact dates for gradual processes.
- **Undeclared simultaneity.**
- **Mixed granularity**, where one step is a decade and the next is an
  afternoon.
- **Eurocentric or single-calendar dating** presented as neutral.
- **Twenty-step procedures.** Chunk them into stages, then sequence within.
- **Procedures with no state**, which are just lists.

## Combinations

- Sequence the steps, then explain one dependency as
  [text input](text-input-cards.md).
- Follow with a [scenario](scenarios-and-simulations.md) that breaks the
  procedure at one step and asks what fails downstream.
- Pair with [diagrams-and-hotspots](diagrams-and-hotspots.md) when each step
  corresponds to a location in a pictured system.
- Use [tables-and-grids](tables-and-grids.md) to contrast two processes that
  share a structure.

## Examples

**History.** Build the chain from Perry's arrival through the unequal treaties
to the Meiji Restoration, then ask which links are causal and which merely
sequential. Place Cold War events on a timeline where several are simultaneous
and declared interchangeable. Order the phases of the Renaissance by region,
where the granularity has to be stated because the process is gradual.

**Science.** Order the stages of mitosis, where the state carried is
chromosome position and each stage genuinely enables the next. Run a titration
as a procedure: prepare, standardise, add indicator, titrate to endpoint,
calculate — then ask what fails if the indicator step is skipped. Sequence
enzyme-catalysed steps in a metabolic pathway.

**Programming.** Order the phases a compiler runs and identify which errors
surface in which phase. Trace a recursive call stack as a sequence of states,
where the returning half is the part learners routinely get wrong.

**Language learning.** Order the historical sound changes that produced a
modern Spanish form, or sequence the strokes of a kanji, where the procedure
has a genuine dependency structure rather than a conventional one.
