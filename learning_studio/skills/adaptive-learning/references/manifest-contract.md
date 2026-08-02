# Manifest contract

The shape every exercise shares. This is the vocabulary you think in when you
design an item, and — since `learning_studio_prepare` exists — it is also the
data the tool actually validates and stores.

## Exercises are declarative data, never renderer code

An exercise is a description of *what to ask and how to judge the answer*. It
is never an implementation of how to draw that on a screen.

**Never write, generate, or emit HTML, JavaScript, CSS, or any other frontend
code as the delivery mechanism for an exercise** — no hand-rolled widget, no
interactive page, not even "just to show what it would look like". Rendering
belongs to the trusted renderer that ships with this plugin, which is trusted
*because* the only thing it displays is validated, inert data from the registry.
Agent-authored UI code is unreviewable, unauditable, inaccessible by default, and
impossible to score consistently. If you are writing a `<div>` so the learner can
*interact* with it, you have left the contract.

The same goes for scoring: express the answer key as data. Do not emit a
snippet of code that computes the score.

### Markup is refused, including in code fields

The stored manifest accepts **inert text only**. Every string is checked, and
these are rejected wherever they appear:

- anything tag-shaped — `<p>`, `</p>`, `<!--`, `<script`, `<style`;
- event-handler attributes, `javascript:` and `data:` URLs, HTML entities;
- stylesheet syntax — `@import`, a `url` reference, or a declaration block,
  with or without its trailing semicolon;
- URLs and URIs of any scheme, including `mailto:`, `ftp:` and `file:`;
- bare web addresses such as `www.example.com` or `example.com/page`;
- filesystem paths — `/etc/hosts`, `/secret`, `./notes`, `../up`, `~/notes`,
  `C:\Users`, `\\server\share` — however they are wrapped, including
  `(/etc/passwd)` and `"/secret"`. A leading slash is a path, so a
  slash-command is written as "the help command", not `/help`;
- hosts and addresses — `example.fr/path`, `example.museum`,
  `sub.example.com`, `192.168.1.1/admin`, `localhost:8080/admin`,
  `[::1]:8080/admin`;
- any URI scheme, one letter upwards: `x:payload` as much as `mailto:`;
- credential-shaped values — `api_key=…`, `password: …`, `Authorization:
  Basic …`, bearer tokens, and current prefixed token shapes;
- invisible and bidirectional characters.

Ordinary prose is unaffected: `a < b`, `3/4`, `12/05/2026`, `Node.js`,
`/help`, "the .org suffix", `{x : x > 0}`, and "what makes a password strong"
all pass.

Write mathematical comparisons with spaces: `a < b`, not `a<b`.

**Code as subject matter is allowed, as long as it carries no tags.** A Python
function, a SQL query, or a regex goes in `content.starter_code`,
`answer.reference_solution`, or a prompt exactly as you would write it, and is
never executed — `code_response` compares code as text. But because markup is
refused everywhere, **you cannot teach HTML, CSS, or JavaScript syntax through a
stored manifest in this release**. Run those sessions in conversation instead.

The renderer does now escape everything it displays — it writes text with
`textContent` and builds elements one at a time, so a stored `<div>` would appear
as those five characters. The restriction stays anyway: it is enforced in the
*store*, which is the layer that cannot be replaced by a client with different
ideas, and relaxing it would mean trusting every future consumer of a manifest to
be as careful.

## Calling the tool

```
learning_studio_prepare({
  "track_id":  optional, only for sustained work on a confirmed track,
  "objective_id": optional, requires track_id,
  "manifest":  { … }
})
```

If you send `objective_id`, the manifest's `objective` must be the stored
objective's own wording — case and spacing aside. An experience that named one
objective while assessing another would read, later, as evidence of progress
against something it never tested.

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

- **Never reveal the key in the prompt, the media, or the alt text.** This is
  enforced for every component whose answer is text the learner must produce —
  cloze gaps, short answers, translations, corrections, recall cues, flashcard
  backs, reference solutions, grid cells. The whole visible half is compared,
  three ways: as a token sequence, so `H2O` in "Type H2O" is caught and `Na`
  does not match inside "national"; as a spelled-out form with any run of
  separators between the characters, so `P.a.r.i.s`, `P...a...r...i...s` and
  `P-----a-----r-----i-----s` are all `Paris`; and as a symbol string, so an
  answer of `+` or `===` cannot be printed in its own prompt. Selection
  components are exempt: their key is an option id, and the option text is
  meant to be read.
- **A refusal never quotes what it is protecting.** Errors name the field —
  `answer.accepted` — and never the value. That holds for whole-manifest
  checks too: an invalid branch target is reported as "component X has an
  incorrect branch whose go_to is not a component of this experience", never
  by naming the target. Nothing this tool returns will quote an answer, a
  rubric, a hint, a note, or a branch target.
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

A few types carry their own rules:

- **`fill_blank`** — every `{{placeholder}}` in the passage must be a declared
  blank, every declared blank must appear in the passage, and no placeholder
  may repeat.
- **`table_grid`** — every cell is either prefilled or has an expected answer,
  exactly once. An empty box nothing will mark is not a question.
- **`multi_select`** and the other set answers — no id twice.
- **`categorization`** — every item assigned exactly once, no category twice
  for one item, and one category per item unless `allow_multiple` is set.
  Several *items* may share a category; that is what grouping is.
- **`error_correction`** — each correction resolves to a *distinct place* in
  the passage. `error_count`, if given, equals the number of distinct places;
  each corrected phrase appears there; each changes something. `are` and
  `are.` are the same place and are refused; a word that genuinely occurs
  twice may be corrected twice.
- **`feedback.per_option`** — each entry must name an option this component
  has, and no option twice.

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

**Each component type accepts only the modes that can mark it.**
`multiple_choice` is `exact`; `multi_select`, `matching` and `labeling` are
`set`; ordering types are `ordered`; `short_answer` is `exact`, `normalised`
or `numeric`; a flashcard or a self-report is `self_check`; open work is
`rubric`. A mode outside its type's list is refused, and a self-report takes
no rubric at all — nobody marks how confident someone says they feel.

Always state whether partial credit applies. Silence produces inconsistent
scoring across a set, which reads to the learner as unfairness. Open work
(`free_response`, `case_study`, `self_explanation`, `rubric_response`,
`image_observation`) has no answer key and **requires** a rubric.

## Branching

`evaluation.branching` is a list of `{on, go_to}`, where `on` is `correct`,
`incorrect`, or `always`, and `go_to` names another component in this
experience. Rejected: a target that does not exist, a component branching to
itself, two branches for the same outcome, and an `always` branch mixed with
conditional ones.

Also rejected, and this is the one worth understanding: **any group of
components the learner can never leave.** An outcome you do not branch falls
through to the next component, and from the last one to the end — so as long
as one possible answer is unbranched, or leads forward, the exercise
finishes. Two components that send each other back on *both* `correct` and
`incorrect` do not, whatever the learner answers, and are refused. A retry
loop branching only on `incorrect` is fine: answering correctly falls
through.

## Source references

Provenance is **description only**. There is nothing to fetch and nothing may
look fetchable:

`title` (required), `author`, `published_on` (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`),
`citation`, `source_id`, `note`.

No URLs, no file paths, no credentials. Up to ten per experience.

## Accessibility

Two levels, and they are about different things.

**Manifest-level** describes what the *experience* must provide, as a closed
list — there is no free-text field:

`source` (required) and `accommodations` (required), drawn from: `captions`,
`transcript`, `text_alternatives`, `visual_description`, `keyboard_only`,
`reduced_motion`, `no_time_limit`, `extended_time`, `plain_language`.

`source` is `profile_config`, and **the claim is verified against the
operator's configuration**. Naming a source that says nothing is refused, and
there is no value meaning "I inferred it".

`explicit_request` and `confirmed_track` are both deliberately absent. Each was
checked against a row the *agent* had written — a temporary context in one
case, a confirmed track and its context in the other — so each amounted to the
model authorising itself, one turn apart or in the same call. Hermes exposes no
signal to check instead, so the sources are gone rather than faked. A session
need is honoured in conversation and not recorded on the exercise.

**The experience must be able to deliver what it declares.** `keyboard_only`
alongside a hotspot, labelling, matching or drag-ordering component is refused
unless that component supplies `accessibility.keyboard_alternative`.
`captions` needs an actual `caption`; `transcript` needs an actual
`transcript`; asking for both needs both. They are different accommodations
for different people, and a flag asserting one is needed is not the thing
itself. `visual_description` needs a long description. `no_time_limit` may not
sit beside a `delivery.time_limit_seconds`.

**Component-level** describes the *component*: `alt_text`, `caption`,
`transcript`, `long_description`, `keyboard_alternative`, `reduced_motion`,
`no_time_limit`.

Never write a diagnosis, a disability, or a sentence about the learner into
any of them. "A cross-section of the heart" is alt text; "the learner has
epilepsy" is a health record, and is refused.

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
