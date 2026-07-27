# Manifest contract

The shape every exercise card shares. This is the vocabulary you think in when
you design an item, whether it ends up rendered by a runtime or typed into chat.

## Exercises are declarative data, never code

An exercise is a description of *what to ask and how to judge the answer*. It
is never an implementation of how to draw that on a screen.

**Never write, generate, or emit HTML, JavaScript, CSS, or any other frontend
code to deliver an exercise.** Not as a file, not in a message, not "just to
show what it would look like". Renderers are the runtime's job; generated
markup is unreviewable, unauditable, inaccessible by default, and impossible to
evaluate consistently. If you find yourself writing a `<div>`, you have left
the contract.

The same goes for scoring: express the answer key as data. Do not emit a
snippet of code that computes the score.

## Status

No tool consumes these manifests yet — nothing in this release reads one. The
contract exists so the items you design are already in the right shape, and so
today's chat-delivered exercise is the same object as tomorrow's rendered one.
Today: build the manifest in your head, render it as text, evaluate it
yourself. Do not tell the learner a manifest was submitted anywhere.

## The common envelope

Every card, regardless of type, carries:

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier for this item, unique within the set |
| `type` | Which card — `single_select`, `short_text`, `ordering`, … |
| `objective` | The measurable objective this item assesses |
| `prompt` | The question, in the content language |
| `content_language` | Language of the material itself |
| `explanation_language` | Language of feedback and instructions |
| `payload` | The type-specific body (options, pairs, steps, …) |
| `answer` | The key — shape depends on `type` |
| `evaluation` | How a response is judged (see below) |
| `feedback` | What to say when right, when wrong, and why |
| `hints` | Ordered, progressive, optional |
| `accessibility` | Alt text, captions, transcript, no-time-limit flags |
| `tags` | Topic labels for scheduling and reporting |

Only `id`, `type`, `prompt`, `payload`, and `answer` are always required.
Everything else is optional but nearly always worth filling in.

## Rules that hold for every card

- **One objective per item.** An item that tests two things tells you nothing
  when it is failed.
- **The key must be defensible.** You must be able to state why the answer is
  right and why each wrong option is wrong.
- **Evaluation must be deterministic where it can be.** Exact match, set
  match, normalised match, ordered match. Reserve judgement-based evaluation
  for genuinely open work, and then use an explicit rubric — see
  [reflection-and-rubrics](reflection-and-rubrics.md).
- **Normalisation is declared, not improvised.** Case, whitespace, accents,
  and punctuation are either significant or not; say which. Accents are
  significant in a Spanish orthography drill and noise in a history date quiz.
- **Feedback explains, it does not just verdict.** "Not quite — the enzyme is
  denatured above 60 °C, so the rate falls rather than plateaus" teaches;
  "Incorrect" does not.
- **Never reveal the key in the prompt, the media, the alt text, or the hint
  order.** This is the most common way a well-built item becomes worthless.
- **No item without a source of truth.** If you cannot verify it, do not ship
  it; mark uncertainty in the item rather than inventing certainty.

## Evaluation modes

| Mode | Use for |
| --- | --- |
| `exact` | Canonical strings, symbols, numbers with fixed format |
| `normalised` | Free text where case, spacing, or accents should not count |
| `numeric` | Numbers with a tolerance and, where relevant, units |
| `set` | Unordered multi-answer; declare whether partial credit applies |
| `ordered` | Sequences; declare whether adjacent transpositions cost less |
| `pattern` | A declared set of accepted forms — as data, not executable code |
| `rubric` | Open work judged against named criteria |

Always state whether partial credit applies. Silence here produces
inconsistent scoring across a set, which reads to the learner as unfairness.

## Worked shapes

A single-select item on cellular respiration:

```
id: bio-resp-04
type: single_select
objective: identify where the Krebs cycle occurs
prompt: In eukaryotes, where does the Krebs cycle take place?
payload.options: [mitochondrial matrix, cytosol, thylakoid membrane, nucleus]
answer: mitochondrial matrix
evaluation: exact
feedback.wrong.cytosol: That is glycolysis — it precedes the Krebs cycle.
```

A short-text item in a Spanish conjugation drill, where the content language
and the explanation language differ:

```
id: es-pret-11
type: short_text
objective: produce preterite forms of irregular verbs
content_language: es
explanation_language: en
prompt: "tener" — third person singular, preterite
answer: [tuvo]
evaluation: normalised   # accents significant, case not
hints: ["The stem changes to tuv-", "Irregular preterites take -o, not -ó"]
```

An ordering item on the causes of a historical event:

```
id: hist-meiji-02
type: ordering
objective: sequence the events leading to the Meiji Restoration
payload.items: [Perry's arrival, unequal treaties, Chōshū–Satsuma alliance,
                Boshin War, imperial restoration]
answer: [1, 2, 3, 4, 5]
evaluation: ordered      # partial credit for adjacent transpositions
```

The same envelope carries a regex-tracing item, a titration calculation, and a
kanji recall card. If a subject seems not to fit, the item is usually testing
two objectives at once.

## Extending

Add fields when a card type genuinely needs them, and name them for what they
mean rather than how they look. Never add a field that carries presentation —
colours, widths, positions. Presentation belongs to the renderer, and an item
that depends on it will break the first time it is delivered as plain text or
read aloud.
