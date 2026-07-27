# Diagrams and hotspots

The answer is a place: the learner points at a region of an image, or labels
parts of a whole.

## When to use

- The objective is **spatial or structural** — knowing where something sits
  within a system, not just that it exists.
- Naming a part is easier than locating it, and locating it is what matters.
  Reciting the layers of a leaf is not the same as finding the palisade
  mesophyll in a section.
- Relative position carries meaning: upstream and downstream, inside and
  outside, adjacent and distant.
- The learner must orient themselves in an unfamiliar representation.

## When not to use

- The image is decorative and the real question is verbal. Use
  [selection-cards](selection-cards.md).
- The regions are so large or so few that pointing is free.
- Precise pointing is a barrier for this learner and no equivalent path
  exists — then convert to labelled selection.
- The objective is sequence rather than location — see
  [timelines-and-processes](timelines-and-processes.md).
- You cannot obtain an accurate image. An inaccurate diagram teaches an
  inaccurate model, which is worse than no exercise.

## Required fields

- `media` — the image, with dimensions and a stated orientation.
- `payload.regions` — named regions with coordinates and, critically, a
  **text label for each**, so the item works without the image.
- `prompt` — what to locate, phrased without naming the region's position.
- `answer` — the region id, or the label-to-region map.
- `tolerance` — how close a click must be; generous by default.
- `accessibility.alt` — describing the image without revealing the answer.
- `accessibility.fallback` — the equivalent labelled-list item.

## Evaluation

Score by region, not by pixel. A hotspot with a tight tolerance measures mouse
control. Where regions are adjacent and easily confused, that confusion is the
learning — report *which* region was picked, because "clicked the cytosol
instead of the matrix" is a specific, actionable misconception.

For labelling, score per correct label with partial credit. Add unused labels
as distractors so the last few cannot be filled by elimination.

If the learner points at something defensible that you marked wrong — a
boundary, an overlapping structure — the item is under-specified. Fix it.

## Accessibility

This card excludes people most easily, so build the alternative first:

- **Every hotspot item needs a non-pointing path**: a numbered overlay, a
  keyboard-navigable region list, or a labelled-list selection version.
- Alt text must describe structure and orientation without naming the target.
  "Cross-section with four labelled layers, A at the top" — never "A is the
  palisade mesophyll".
- Regions must be large enough for imprecise pointing, and reachable by
  keyboard with a visible focus indicator.
- Never distinguish regions by colour alone; use labels or patterns.
- Ensure the image survives zooming — a learner may need it at 400%.
- See [accessibility](accessibility.md).

## Anti-patterns

- **Pixel-hunting.** Tiny targets and tight tolerances.
- **Alt text that answers the question.**
- **Colour-coded regions** with no other distinction.
- **Unlabelled generated images** presented as authoritative anatomy or
  circuitry — verify before use.
- **Invented assets.** Never reference an image ID or path a tool did not
  return.
- **Orientation assumptions.** "Click the part on the left" breaks the moment
  the image is mirrored or linearised.
- **Overloading one image** with twelve questions until it becomes a memory
  test of your own labelling.

## Combinations

- Locate the part, then name its function as
  [text input](text-input-cards.md) — position plus meaning.
- Label a diagram, then [order](ordering-and-matching.md) the parts by the
  sequence in which they act.
- Use a diagram as the shared context for a
  [table](tables-and-grids.md) contrasting the parts.
- Open a [scenario](scenarios-and-simulations.md) with a diagram the learner
  must read before deciding.

## Examples

**Science.** Locate the site of the Krebs cycle on a mitochondrion diagram,
with the cytosol as an adjacent distractor; label the stages of mitosis on a
micrograph series; identify where an enzyme's active site sits on a folded
protein; point to the equivalence point on a titration curve.

**History.** Click the territory transferred by a named treaty on a period map;
locate the trade routes that carried Renaissance wealth into Florence; identify
on a city plan where the Ottoman fortifications stood. Historical maps demand
accuracy — an approximate border teaches a false claim.

**Programming.** Point to the line where the recursion fails to reach its base
case in a screenshotted function; identify the phase in a compiler pipeline
diagram where type errors are caught; locate the join in a query plan.

**Language learning.** Point to the stressed syllable in a displayed word; on a
kanji, identify the radical that carries the meaning versus the one that
carries the sound.
