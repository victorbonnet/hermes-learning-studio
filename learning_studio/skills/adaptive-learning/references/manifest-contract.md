# Manifest contract

The shape every exercise shares. This is the vocabulary you think in when you
design an item, and — since `learning_studio_prepare` exists — it is also the
data the tool actually validates and stores.

## Exercises are declarative data, never renderer code

An exercise is a description of *what to ask and how to judge the answer*. It
is never an implementation of how to draw that on a screen.

**Never write, generate, or emit HTML, JavaScript, CSS, or any other frontend
code as the delivery mechanism for an exercise** — no renderer, no widget, no
interactive page, not even "just to show what it would look like". Rendering is
the runtime's job. Agent-authored UI code is unreviewable, unauditable,
inaccessible by default, and impossible to score consistently. If you are
writing a `<div>` so the learner can *interact* with it, you have left the
contract.

The same goes for scoring: express the answer key as data. Do not emit a
snippet of code that computes the score.

### Markup is refused, including in code fields

The stored manifest accepts **inert text only**. Every string is checked, and
these are rejected wherever they appear:

- anything tag-shaped — `<p>`, `</p>`, `<!--`, `<script`, `<style`;
- event-handler attributes, `javascript:` and `data:` URLs, HTML entities;
- stylesheet syntax — `@import`, a `url` reference, a `{ property: value; }`
  block;
- URLs of any scheme, filesystem paths, `../` traversal;
- credential-shaped values — `api_key=…`, `password: …`, bearer tokens, keys;
- invisible and bidirectional characters.

Write mathematical comparisons with spaces: `a < b`, not `a<b`.

**Code as subject matter is allowed, as long as it carries no tags.** A Python
function, a SQL query, or a regex goes in `content.starter_code`,
`answer.reference_solution`, or a prompt exactly as you would write it, and is
never executed — `code_response` compares code as text. But because markup is
refused everywhere, **you cannot teach HTML, CSS, or JavaScript syntax through
a stored manifest in this release**. Run those sessions in conversation
instead. Lifting the restriction needs a renderer that can prove it escapes
what it displays, and there is no renderer yet.

## Calling the tool

```
learning_studio_prepare({
  "track_id":  optional, only for sustained work on a confirmed track,
  "objective_id": optional, requires track_id,
  "manifest":  { … }
})
```

You never pass a learner. Identity comes from the Hermes session, and the
experience id is generated for you — there is no field for either, so there is
nothing to impersonate with and nothing of anyone else's to overwrite.

## The manifest envelope

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Always `1` |
| `title` | yes | What this exercise is, in a line |
| `objective` | yes | `behavior` + `condition` + `standard` — the measurable objective |
| `instructions` | yes | What the learner is told before they start |
| `ui_locale` | yes | Language of instructions and feedback (`en`, `pt-BR`, …) |
| `content_locale` | no | Language of the material, when it differs |
| `expected_duration_minutes` | yes | 1–240 |
| `difficulty` | yes | `introductory` \| `intermediate` \| `advanced` \| `expert` |
| `source_references` | no | Provenance, as description — see below |
| `accessibility` | no | What the exercise needs to be usable |
| `delivery` | no | `mode`, `allow_back`, `allow_skip`, `time_limit_seconds` |
| `components` | yes | The items, in the order the learner works through them |

Up to 40 components, and 128 KB of manifest. Beyond that, it is two
experiences.

## The common component envelope

Every component, whatever its type, carries:

| Field | Meaning |
| --- | --- |
| `id` | Unique within this experience |
| `type` | One of the 31 types below |
| `prompt` | The question, in the content language |
| `content` | The type-specific body the learner sees — options, items, steps |
| `accessibility` | Alt text, captions, keyboard alternative, no-time-limit |
| `answer` | The key. Shape depends on the type. **Never shown to the learner** |
| `evaluation` | `rubric`, `scoring`, `hints`, `feedback`, `branching`, `notes`. **Never shown** |

## The private/public boundary

**The split is a security boundary, not a formatting choice.**

**Public (client-side).** `id`, `type`, `prompt`, `content` and
`accessibility` are the learner's half — safe to send to a renderer, and safe
to read out in chat.

**Private (server-side only).** `answer` and `evaluation` — accepted variants,
tolerances, rubric levels, per-option feedback, unreleased hints, scoring
weights, branch targets, evaluator notes. The tool stores these in a separate
table and returns none of them to you.

That is enforced, not advisory: a field that is not in the public half is never
copied into the learner payload, so you cannot leak the key by putting it in
the wrong place. The rules that follow are the ones the code cannot enforce
for you:

- **Grade on the server.** A client that knows the key can be read; a client
  that reports its own score can be edited. Neither is hypothetical. The
  **answer must never be sent** to a client before the attempt is graded.
- **There is no such thing as a hidden field on the client.** Collapsed,
  hidden, and off-screen are all fully readable by the learner. Never rely on
  an interface to conceal anything.
- Where an exercise genuinely cannot be graded without the key on the client —
  an offline flashcard self-check — that is a legitimate design, but say so
  and never present its score as an assessment result.

- **Never reveal the key in the prompt, the media, or the alt text.** The tool
  catches this for cloze passages, flashcards, and recall cues, where hiding
  the answer *is* the format. Everywhere else it is your judgement.
- **Release hints one at a time**, and send feedback in response to an attempt
  rather than alongside the question.

## Component types

Pick from the type that matches the objective's verb. Each family has a
reference with worked examples.

| Family | Types | Reference |
| --- | --- | --- |
| Selection | `multiple_choice`, `multi_select`, `true_false`, `classification` | [selection-cards](selection-cards.md) |
| Text input | `fill_blank`, `short_answer`, `free_response`, `translation`, `error_correction`, `code_response` | [text-input-cards](text-input-cards.md) |
| Ordering and matching | `sentence_order`, `sequence_order`, `matching`, `categorization` | [ordering-and-matching](ordering-and-matching.md) |
| Recall | `flashcard`, `typed_recall` | [flashcards-and-recall](flashcards-and-recall.md) |
| Visual | `image_observation`, `image_choice`, `diagram`, `hotspot`, `labeling` | [media-cards](media-cards.md), [diagrams-and-hotspots](diagrams-and-hotspots.md) |
| Timeline and process | `timeline`, `process_flow` | [timelines-and-processes](timelines-and-processes.md) |
| Structured | `table_grid` | [tables-and-grids](tables-and-grids.md) |
| Scenarios | `scenario_choice`, `decision_path`, `case_study` | [scenarios-and-simulations](scenarios-and-simulations.md) |
| Reflection | `confidence_rating`, `self_explanation`, `reflection`, `rubric_response` | [reflection-and-rubrics](reflection-and-rubrics.md) |

An unknown type is rejected, and so is any field the type does not declare —
at every level. If a rejection surprises you, the type is probably not the one
you want.

## Evaluation modes

`evaluation.scoring.mode` is one of:

| Mode | Use for |
| --- | --- |
| `exact` | Canonical strings, symbols, numbers with fixed format |
| `normalised` | Free text where case, spacing, or accents should not count |
| `numeric` | Numbers with a tolerance and, where relevant, units |
| `set` | Unordered multi-answer; declare whether partial credit applies |
| `ordered` | Sequences |
| `rubric` | Open work judged against named criteria |
| `self_check` | The learner grades their own recall, or reports on themselves |

Always state whether partial credit applies. Silence produces inconsistent
scoring across a set, which reads to the learner as unfairness. Open work
(`free_response`, `case_study`, `self_explanation`, `rubric_response`,
`image_observation`) has no answer key and **requires** a rubric.

## Branching

`evaluation.branching` is a list of `{on, go_to}`, where `on` is `correct`,
`incorrect`, or `always`, and `go_to` names another component in this
experience. Rejected: a target that does not exist, a component branching to
itself, two `always` branches, an `always` branch mixed with conditional ones,
and a loop made only of `always` edges. A retry loop on `incorrect` is fine —
the learner's own answer is the way out.

## Source references

Provenance is **description only**. There is nothing to fetch and nothing may
look fetchable:

`title` (required), `author`, `published_on` (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`),
`citation`, `source_id`, `note`.

No URLs, no file paths, no credentials. Up to ten per experience.

## Accessibility

Manifest-level: `source` (required), `text_alternatives_required`,
`captions_required`, `visual_description_required`, `keyboard_only`,
`reduced_motion`, `no_time_limit`, `reading_level`, `notes`.

Component-level: `alt_text`, `caption`, `long_description`,
`keyboard_alternative`, `transcript_required`, `captions_required`,
`reduced_motion`, `no_time_limit`.

`source` must be `explicit_request`, `confirmed_track`, or `profile_config` —
the three sources the Studio treats as authoritative. There is no value for an
inference, because you must not infer someone's needs. Metadata on an exercise
is **not** a durable fact about the learner and creates no memory candidate;
storing an accessibility need still needs the explicit consent described in
SKILL.md.

## Assets

Images are referenced by opaque identifier: `{"asset_ref": "…", "alt_text":
"…"}`. `alt_text` is required — an image whose alternative text is optional is
an image that ships without one.

**There is no asset import tool yet.** Never invent an `asset_ref`; use only
an identifier a tool actually returned to you, and until one exists, describe
the image in text instead. A described diagram is a working exercise; a
fabricated reference is a broken one.

## Rules that hold for every component

- **One objective per item.** An item that tests two things tells you nothing
  when it is failed.
- **The key must be defensible.** You must be able to state why the answer is
  right and why each wrong option is wrong.
- **Normalisation is declared, not improvised.** Accents are significant in a
  Spanish orthography drill and noise in a history date quiz; say which.
- **Feedback explains, it does not just verdict.** "Not quite — the enzyme is
  denatured above 60 °C, so the rate falls rather than plateaus" teaches;
  "Incorrect" does not.
- **No item without a source of truth.** If you cannot verify it, do not ship
  it; mark uncertainty in the item rather than inventing certainty.

## Worked shapes

A single-answer item on cellular respiration:

```
id: bio-resp-04
type: multiple_choice
prompt: In eukaryotes, where does the Krebs cycle take place?
content.options: [{id: matrix, text: The mitochondrial matrix},
                  {id: cytosol, text: The cytosol}, …]
answer: {option_id: matrix}
evaluation.scoring: {mode: exact, points: 1}
evaluation.feedback.per_option: [{option_id: cytosol,
                                  text: That is glycolysis — it precedes the Krebs cycle.}]
```

A produced-answer item in a conjugation drill, where the content language and
the explanation language differ:

```
manifest.ui_locale: en
manifest.content_locale: es
id: es-pret-11
type: short_answer
prompt: "tener" — third person singular, preterite
answer: {accepted: [tuvo], accent_sensitive: true, case_sensitive: false}
evaluation.scoring: {mode: normalised}
evaluation.hints: [The stem changes to tuv-, Irregular preterites take -o, not -ó]
```

An ordering item on the causes of a historical event:

```
id: hist-meiji-02
type: timeline
prompt: Put these events in the order they happened.
content.events: [{id: perry, text: Perry's squadron arrives}, …]
answer: {order: [perry, treaties, alliance, boshin, restoration]}
evaluation.scoring: {mode: ordered, partial_credit: true}
```

The same envelope carries a titration calculation and a kanji recall card. If a
subject seems not to fit, the item is usually testing two objectives at once.
