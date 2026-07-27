# Ordering and matching

Three related cards: put items in the right order, pair items across two sets,
or sort items into categories.

## When to use

- **Ordering** when the objective is sequence, precedence, or ranking, and the
  order itself carries the meaning.
- **Matching** when the objective is association across two sets — term to
  definition, function to output, cause to effect.
- **Categorising** when the objective is classification into groups, especially
  where boundary cases are the point.
- When you want to test **relations** rather than isolated facts. Knowing that
  five things exist is weaker than knowing how they relate.

## When not to use

- The relationship is one-to-one and obvious, making the whole set solvable by
  elimination once two pairs are placed.
- Order is genuinely disputed. If historians disagree about which cause came
  first, an ordering card manufactures a false answer.
- The objective is recall of a single item — use
  [text-input-cards](text-input-cards.md).
- The sequence is a process with state, where each step changes what is
  possible next. That is
  [timelines-and-processes](timelines-and-processes.md).
- Precise pointing is a barrier and no keyboard path exists.

## Required fields

- `payload.items` — the items to order, or the two sets to pair.
- `answer` — the correct sequence, the pair map, or the category map.
- `evaluation` — `ordered` for sequences, `set` for pairs and categories.
- A **partial-credit rule**, stated: adjacent transpositions, correct pairs, or
  correctly placed items.
- For matching: whether the sets are equal in size, and whether distractors
  exist on either side.
- For categorising: category definitions precise enough to defend every
  placement.

## Evaluation

All-or-nothing scoring on a ten-item sequence is nearly useless — one
transposition reads the same as total ignorance. Use a distance measure:
adjacent swaps cost little, a block moved wholesale costs more, and credit each
correctly placed item.

For matching, score per correct pair. Add unmatched distractors on one side to
defeat elimination; without them, an eight-pair item is really six pairs plus
two free ones.

For categorising, watch for items defensible in two categories. If the learner
argues for a placement you marked wrong and the argument is good, the item is
wrong. Fix it and say so.

## Accessibility

- **Every drag interaction needs a non-dragging path**: keyboard reordering,
  typing numbers against items, or select-source-then-target. Drag-and-drop is
  a motor-precision test the objective did not ask for.
- Announce position changes for screen-reader users — "moved to position 3 of
  6" — rather than relying on the visual layout.
- Never rely on colour to indicate group membership; label the categories.
- Keep sets to about seven items; longer sets test working memory rather than
  the relation you meant to assess.
- See [accessibility](accessibility.md).

## Anti-patterns

- **Elimination-solvable sets.** Equal-size one-to-one matching with no
  distractors.
- **Chronology as a proxy for causation.** Ordering events correctly is not
  understanding why one caused the next; ask for the mechanism too.
- **Categories that overlap.** If you cannot state a rule that decides every
  item, do not use the card.
- **Ordering by an unstated criterion.** "Order these" — by date? By
  importance? By magnitude?
- **Fifteen-item sequences.** Split them.
- **Ordering things with no inherent order**, which teaches a false structure.

## Combinations

- Order the steps first, then ask *why* step 3 precedes step 4 as a
  [text input](text-input-cards.md). The relation plus the mechanism.
- Match terms to definitions before a
  [flashcard](flashcards-and-recall.md) block on the same terms — matching
  builds recognition, flashcards convert it to recall.
- Categorise, then use [tables-and-grids](tables-and-grids.md) to contrast the
  categories systematically.
- Pair with [diagrams-and-hotspots](diagrams-and-hotspots.md) when the items
  being ordered are parts of a pictured whole.

## Examples

**History.** Order the events leading to the Meiji Restoration, with partial
credit for adjacent transpositions, followed by a question on which link in the
chain is causal rather than merely sequential. Matching: treaty to the
territorial change it produced, with two extra treaties as distractors.

**Programming.** Order the phases a compiler runs, from lexing to code
generation. Matching: each regex pattern to the one string it matches, with
distractor strings that fail on a subtle boundary. Categorising: sort operations
into O(1), O(log n), O(n), and O(n log n).

**Science.** Order the stages of mitosis. Matching: enzyme to the reaction it
catalyses, with two enzymes unmatched. Categorising: sort processes by whether
they consume or release energy — the boundary cases carry the learning.

**Language learning.** Order the words of a scrambled sentence to test syntax
rather than vocabulary. Matching: each Spanish preposition to its governing
case, or kanji to reading, with homophone distractors.
