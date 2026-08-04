# Tables and grids

Two-dimensional cards: the learner fills, completes, or corrects a matrix where
rows and columns each carry meaning.

## When to use

- The objective is **systematic contrast** across several cases on several
  attributes. The grid makes the pattern visible in a way a list cannot.
- The material is genuinely paradigmatic: conjugation tables, property tables,
  comparison matrices, feature grids.
- Gaps in a partially filled table expose exactly which cell of a system the
  learner has not built.
- The learner needs to see that a system is regular, and where the exceptions
  sit.

## When not to use

- There is only one dimension. A list in a grid is a list with extra work; use
  [text-input-cards](text-input-cards.md).
- Cells are not comparable, and the grid implies a false symmetry between
  categories that do not correspond.
- The grid would be large. A 6 × 8 table is 48 items presented at once, which
  is an endurance test rather than an assessment.
- The relationship is one-to-one pairing — that is
  [ordering-and-matching](ordering-and-matching.md).
- The content is narrative or argumentative, which a grid flattens into
  bullet points and destroys.

## Required fields

- `payload.rows` and `payload.columns` — each with a clear, stated label.
- `payload.cells` — which are pre-filled and which the learner must supply.
- `answer` — per cell, with accepted variants where the cell is free text.
- `evaluation` — `exact` or `normalised`, scored per cell against that cell's
  own accepted forms, with per-cell partial credit if declared.
- `cell_type` — free text, selection, or numeric, per column if it varies.
- Cells that are **legitimately empty or not applicable**, marked as such so a
  blank is not scored as a miss.

## Evaluation

Score per cell, always. A single wrong cell in a twenty-cell grid is not a
failed exercise, and all-or-nothing scoring here is the fastest way to
demoralise a learner who has understood the system.

Read by row and by column, because the axis of failure is the diagnosis. All
errors in one column means one attribute is not understood; all errors in one
row means one case is not known; scattered errors mean the system has not been
internalised at all. Report which.

Apply the same normalisation rules as [text input](text-input-cards.md) and
state them once for the whole grid rather than per cell.

## Accessibility

- Tables must have **proper row and column headers** associated with each cell,
  or a screen reader reads a stream of unlabelled values.
- Every cell must be reachable and editable by keyboard in a predictable order.
- Never encode the correct/incorrect state by cell colour alone; add a mark or
  a word.
- Wide grids must scroll within their own container, not force the whole page
  sideways, and must stay usable when reflowed to one column.
- Announce cell position on focus — "row 3, column 2" — since visual position
  is unavailable.
- Consider offering the same content as a sequence of single-cell questions for
  learners who find grids overwhelming. See [accessibility](accessibility.md).

## Anti-patterns

- **The wall of cells.** Anything past about twenty blanks at once.
- **False symmetry.** Forcing dissimilar cases into shared columns because the
  grid demands it.
- **Colour-only correctness.**
- **Unlabelled axes**, leaving the learner guessing what the columns mean.
- **Hidden not-applicable cells** scored as errors.
- **Copy-detectable rows**, where one row is fully given and the next is
  identical in pattern, making it fillable without knowledge.

## Combinations

- Fill the grid, then explain one exception as
  [text input](text-input-cards.md) — the exceptions carry the understanding.
- Precede with [ordering-and-matching](ordering-and-matching.md) to establish
  the categories the grid then contrasts.
- Follow with [flashcards](flashcards-and-recall.md) on the individual cells
  once the system is understood, to make them automatic.
- Use alongside [timelines-and-processes](timelines-and-processes.md) to
  compare two processes that share a structure.

## Examples

**Language learning.** A Spanish conjugation grid: persons down, tenses across,
with the regular forms pre-filled and the irregular preterite cells blank —
the gaps land exactly where the difficulty is. A kanji grid contrasting
on-reading, kun-reading, and meaning across a set that shares a radical.

**Science.** Enzymes down, optimum pH, optimum temperature, and substrate
across. A grid of titration indicators against the pH ranges they signal, with
"not applicable" cells marked where an indicator is unsuitable. Contrast
mitosis and meiosis across chromosome number, crossing over, and daughter-cell
count.

**History.** Compare the Meiji and Ottoman reform programmes across military,
legal, and fiscal dimensions — the grid makes the divergence visible. Contrast
treaty terms across the parties who signed them, marking cells where a party
was simply absent rather than silent.

**Programming.** Operations down, time complexity and space complexity across,
for the common data structures. A regex metacharacter grid against what each
matches in different flavours, with cells marked not-applicable where a flavour
lacks the construct.
