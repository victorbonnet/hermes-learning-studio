# hermes-learning-studio

A standalone [Hermes](https://hermes-agent.nousresearch.com) plugin for adaptive
learning: structured study sessions built on active recall and spaced
repetition.

> **Status: early development.** This is not the feature-complete public
> release. What exists today is a bundled skill plus three tools: two that
> remember a learner's **context** — their goals, level, preferences, and
> confirmed learning tracks — and one that validates and stores the
> **exercises** an agent designs, all in profile-scoped SQLite.
>
> There is still **no delivery runtime**: no card renderer, no Mini App, no
> scoring engine, no scheduler, and no network requests. A prepared exercise is
> stored data, not a running session; exercises are delivered as ordinary
> conversation, and attempts and scores are not persisted. See
> [Roadmap](#roadmap) for what is still to come.

## Install

The supported way to install is Hermes' own plugin installer:

```bash
hermes plugins install victorbonnet/hermes-learning-studio --enable
```

For a specific Hermes profile:

```bash
hermes --profile <profile> plugins install victorbonnet/hermes-learning-studio --enable
```

Plugins are opt-in. `--enable` turns the plugin on as part of the install; drop
it and enable later with `hermes plugins enable learning-studio`.

Verify with `hermes plugins list`, or
`HERMES_PLUGINS_DEBUG=1 hermes plugins list` if the plugin does not appear.

### Alternative: clone into the plugins directory

```bash
git clone https://github.com/victorbonnet/hermes-learning-studio.git \
  ~/.hermes/plugins/learning-studio
hermes plugins enable learning-studio
```

### Alternative: pip install

```bash
pip install git+https://github.com/victorbonnet/hermes-learning-studio.git
hermes plugins enable learning-studio
```

This path registers the plugin through the `hermes_agent.plugins` entry point.
The entry point resolves to the `learning_studio` **module** (not
`learning_studio:register`), because Hermes calls `ep.load()` and then looks up
`.register` on the result — a `module:attribute` value returns the function
itself and fails with `Plugin 'learning-studio' has no register() function`.
`tests/test_entry_point.py` verifies the load path behaviourally, and the wheel
is installed into a clean virtualenv during verification to confirm it.

## Usage

Ask the agent to load the skill:

```
skill_view("learning-studio:adaptive-learning")
```

Plugin skills are namespaced by the manifest name and are deliberately absent
from the system prompt's `<available_skills>` index — they are explicit,
opt-in loads.

The skill links a catalogue of exercise-format references, which the agent
opens one at a time rather than loading wholesale:

```
read_file("${HERMES_SKILL_DIR}/references/selection-cards.md")
```

`${HERMES_SKILL_DIR}` is substituted for the skill's real directory before the
content reaches the agent, so the path is concrete by the time it is read.

## Architecture

The repository is simultaneously a Hermes **directory plugin** and an
installable **Python package**, which drives the layout:

```
.
├── plugin.yaml                 # Hermes manifest: identity, kind, declared surface
├── __init__.py                 # Hermes entry point — thin shim exposing register()
├── learning_studio/            # Implementation package
│   ├── __init__.py             # Re-exports register(), owns __version__
│   ├── plugin.py               # register(ctx) — the whole host contract
│   ├── paths.py                # Profile-safe path resolution
│   ├── config.py               # Validated `learning_studio` config.yaml section
│   ├── models.py               # Context fields, provenance, validation
│   ├── storage.py              # SQLite connections and versioned migrations
│   ├── context.py              # Precedence resolution
│   ├── candidates.py           # Memory-candidate rules
│   ├── safety.py               # Content rules — inert text or nothing
│   ├── components.py           # The trusted component registry (31 types)
│   ├── manifest.py             # The experience envelope and its validation
│   ├── service.py              # Reads, writes, ownership, consent gates
│   ├── schemas.py              # JSON schemas for the three tools
│   ├── tools.py                # Tool handlers
│   └── skills/
│       └── adaptive-learning/
│           ├── SKILL.md        # The orchestration workflow
│           └── references/     # Loaded on demand via read_file + HERMES_SKILL_DIR
└── tests/                      # Unit tests (fake context) + opt-in Hermes integration
```

The decisions that shape this foundation:

**One identity, two install paths.** Hermes derives a plugin's skill namespace
from its manifest `name` for directory installs, and from the entry-point name
for pip installs. `plugin.yaml` and the `hermes_agent.plugins` entry point in
`pyproject.toml` therefore both say `learning-studio`, so the skill resolves as
`learning-studio:adaptive-learning` however it was installed. A test asserts the
manifest name and the registered namespace agree.

**The entry point names a module, never `module:attribute`.** Hermes' loader is
`module = ep.load()` followed by `getattr(module, "register", None)`. A value of
`learning_studio:register` makes `ep.load()` return the function, which has no
`.register` attribute, so the plugin silently fails to load with a warning
rather than a traceback. The value is `learning_studio`, and
`tests/test_entry_point.py` loads it through `importlib.metadata` and asserts a
module comes back — including a test that the old value would *not* satisfy the
contract, so the guard cannot rot.

**The root shim uses a relative import.** Hermes loads directory plugins with
`spec_from_file_location(..., submodule_search_locations=[plugin_dir])` under
the `hermes_plugins` namespace — the plugin directory is never placed on
`sys.path`. An absolute `import learning_studio` would fail at runtime. The
shim also carries an absolute fallback for importers that load it as a
top-level module (pytest imports the ancestor `__init__.py` of any collected
test), and a test reproduces Hermes' exact loading mechanism to keep the live
path honest.

**Registration cannot fail.** `register(ctx)` is called at every Hermes startup
for every enabled plugin. It registers one skill, imports nothing optional, and
declares no `requires_env` — so enabling the plugin cannot break a session. The
FastAPI and Pillow dependencies that later PRs introduce must stay behind lazy
imports inside the code paths that need them; a test blocks those modules at
import time and asserts registration still succeeds.

**One skill, many references.** The skill is a single orchestration workflow —
discovery, objectives, pedagogy, format selection, verification, activation,
interpretation, adaptation — with a catalogue of exercise-format references
beside it. The catalogue is *not* registered as fourteen skills; the agent
opens one reference at a time, so it pays for only what the current decision
needs. Tests assert that every reference is linked by a valid relative path,
that none is orphaned, and that the registered surface stays at exactly one
skill.

**References are opened with `read_file`, not `skill_view`.** This is a
correctness constraint, not a preference. Hermes' `skill_view` accepts a
`file_path` argument, but qualified `plugin:skill` names are dispatched to
`_serve_plugin_skill()`, which has no such parameter — the argument is dropped
and SKILL.md is returned again *with `success: true`*. An agent following that
idiom would silently re-read the same file instead of the reference it asked
for. So SKILL.md addresses references as
`read_file("${HERMES_SKILL_DIR}/references/<file>.md")`, the same token Hermes'
own bundled skills use for sibling files, and warns explicitly against the
`skill_view` route. `tests/test_hermes_integration.py` verifies both halves
against a real Hermes checkout, and fails if Hermes ever fixes the plugin path
so the workaround can be removed.

**The skill tells the truth about its own scope.** `SKILL.md` states plainly
that this foundation has no tools and no persistence, because a skill that
implies otherwise sends the agent after tools that do not exist. A test fails if
the skill body references tool names while `register()` registers no tools, and
textual contract tests pin the load-bearing rules: what may launch without
asking, that a missing tool falls back to chat, that exercises are declarative
data rather than generated frontend code, that image assets come from real tool
results, and who owns which memory store.

**Subject-agnostic by construction.** No subject is the plugin's default.
Examples span language learning, programming, history, and science, and a test
fails if any one domain accounts for more than 40% of the examples in the
skill corpus or if a format reference illustrates fewer than three unrelated
subjects.

**Authorisation lives in the storage layer, not the handler.** Every
learner-owned query carries `profile_id` and `learner_id` in its `WHERE`
clause, so a handler that forgot an ownership check still could not read
another learner's track — there is no query that can. Foreign keys enforce
referential integrity; they do not decide who may read a row. Not-found and
not-yours return the same message, because distinguishing them would turn a
track ID into an oracle for whether another learner exists. Adversarial tests
try exactly that, with valid IDs belonging to someone else.

**Learner identifiers are not stored in the clear.** The principal is
converted to a salted HMAC digest with a per-database salt, so the platform ID
stays out of logs, backups, and a glance at the file, and precomputed tables
are useless against it.

This is *not* a claim that identity is unrecoverable. The salt lives in the
same database as the digests, so anyone holding the file can brute-force the
low-entropy space of platform user IDs offline. Resisting that needs a pepper
stored separately, with its own lifecycle and rotation story, and this plugin
does not mint secrets on a user's behalf. The digest is a lookup key; it is
never an authorisation check.

All primary keys are opaque generated tokens, so nothing keys on a label a
learner can change.

**Migrations are all-or-nothing.** All pending migrations run in one shared
transaction and roll back together on failure, because a half-applied schema
is harder to recover from than a failed startup. Explicit privacy and retention
migrations may purge rows that the current policy forbids keeping; unrelated
data is preserved and the cleanup rolls back with the upgrade batch on failure. A
database written by a newer version of the plugin is refused with an
explanation and left byte-for-byte untouched — deleting or "resetting" an
unfamiliar database would destroy a learner's record to make the code happy.

**`register()` opens no database.** It registers a skill and three tools and
returns. Initialising storage at startup would let a corrupt or
newer-versioned database take the whole plugin down, instead of failing one
tool call with a message the agent can act on.

**No runtime dependencies.** `dependencies` is empty, so installing the plugin
adds nothing to a user's Hermes environment. Persistence uses the standard
library's `sqlite3`. PyYAML is a test-only dependency in the `dev` extra, and
a test blocks FastAPI, Pillow, Telegram, HTTP clients, and PyYAML at import
time then asserts the tools still register *and run*.

## Configuration

Behavioural settings belong in Hermes' `config.yaml`, under a single
`learning_studio` section. Secrets belong in `.env` — this plugin has none and
reads none. Every setting below is optional; the defaults shown are what
applies when the section is absent.

```yaml
learning_studio:
  # How long unconfirmed temporary context stays readable, in hours (1–8760).
  temporary_context_ttl_hours: 72

  # Upper bound on active tracks per learner (1–200).
  max_tracks_per_learner: 20

  # SQLite lock wait, in milliseconds (100–60000).
  busy_timeout_ms: 5000

  # wal | delete | truncate. WAL falls back automatically on filesystems
  # that cannot support it.
  journal_mode: wal

  # Independent observations before repeated evidence may be proposed as a
  # memory candidate (2–50).
  memory_candidate_min_evidence: 3

  # Deprecated compatibility switch. Accessibility is never stored. True
  # accepts and validates the old accessibility_consent audit payload for the
  # response only; false rejects that payload. Neither value permits storage.
  allow_durable_accessibility_needs: true

  # Longest single context value, in characters (80–20000).
  max_context_value_chars: 2000

  # Context values that apply to everyone on this profile.
  profile_context:
    explanation_language: English

  # Last-resort values used only where nothing else is known. They never
  # overwrite anything stored or explicit.
  defaults:
    session_duration: 20 minutes
```

`profile_context` and `defaults` accept any of the context fields listed in
[Learning context](#learning-context).

**The section fails closed.** A malformed value raises rather than falling
back to a default, and an unknown key is an error rather than being ignored —
every setting here governs retention, isolation, or consent, and a misspelled
`allow_durable_accessibility_needs` that silently degraded to "off" would look
exactly like the setting working.

### Storage

The database lives at:

```
$HERMES_HOME/workspace/learning-studio/learning-studio.sqlite3
```

resolved through the host's `get_hermes_home()`, so it follows the active
profile. Directories are created `0700` and the database `0600` where the
filesystem supports it. Each Hermes profile gets its own database; nothing is
shared between them.

## Tools

Three tools, all in the `plugin_learning_studio` toolset. None takes a learner
argument: identity is resolved from the Hermes session, so a call always reads
and writes the record of whoever sent the current message.

### `learning_studio_get_context`

Returns the learner's context in three distinct parts, and never guesses:

- `temporary_context` — unconfirmed conversational evidence, which expires.
- `confirmed_context` — the durable context of a confirmed track.
- `resolved_context` — one value per field after precedence, each carrying its
  `provenance`, whether it is `confirmed`, and the `superseded` candidates it
  beat.

If a learner has several active tracks and the call names none,
`track_selection.mode` is `ambiguous` and the tracks are listed, so the agent
asks instead of studying the wrong thing.

### `learning_studio_save_context`

Saves temporary context, evidence, explicit corrections, confirmed tracks,
objectives, and memory candidates, all in one transaction. The response
reports exactly what became durable, what stayed temporary, and what was
refused and why.

**Creating a track requires `track.confirmed: true`.** Absence of the flag
means no durable track is created — the context is kept as temporary instead.
Repetition, agent confidence, and prior sessions are not confirmation, and no
code path treats them as such.

`outcome.not_stored` lists anything deliberately dropped, with a reason.

### `learning_studio_prepare`

Validates a complete **experience manifest** — an ordered set of exercise
components with their answer keys — and stores it transactionally. Returns an
opaque `experience_id` and a learner-safe summary.

The manifest is the UI contract. It is data, never renderer code: no tool here
generates HTML, and every string is checked to be inert text. See
[Exercise manifests](#exercise-manifests).

## Exercise manifests

An agent designs an exercise; this plugin decides whether it is storable. The
contract has three parts.

**A discriminated component registry.** Thirty-one component types across nine
families — selection, text input, ordering and matching, recall, visual,
timeline and process, structured, scenarios, and reflection. A component's
`type` selects exactly one specification; an unknown type is refused, and so is
any field that specification does not declare, at every level of nesting. The
registry is subject-neutral by design: `fill_blank` serves a chemistry equation
and a Spanish conjugation equally, and nothing privileges one discipline.

`code_response` collects and compares code **as text**. Nothing in this plugin
compiles, imports, or runs it, and a test parses every source file to prove
there is no `eval`, `exec`, `compile`, or `subprocess` anywhere that could.

**Visible and hidden are different places, not different names.** Each
component splits in two:

| Half | Fields | Where it goes |
| --- | --- | --- |
| Learner-visible | `id`, `type`, `prompt`, `content`, `accessibility` | `experience_components` |
| Evaluator-only | `answer`, `evaluation` (rubric, scoring, hints, feedback, branching, notes) | `experience_component_evaluations` |

The learner payload is **constructed from an allowlist**, not filtered. A field
that is not named in `LEARNER_VISIBLE_KEYS` cannot reach a learner, whatever a
future caller does, and nothing has to remember to delete anything. The split
runs all the way into the schema: the learner-facing table contains no answer
column, so a projection that forgets to exclude one still cannot leak a key.
The tool's own response is built from the visible half, so no answer travels
back through the model either. Tests put a distinctive canary in every hidden
field and assert recursively that none appears in the payload, the projection,
or the tool response.

**Inert text or nothing.** Every string — learner-visible and evaluator-only
alike — is refused if it contains markup, event-handler attributes, `javascript:`
or `data:` URLs, HTML entities, stylesheet syntax, any URL, a filesystem path,
`../` traversal, credential-shaped values, or invisible and bidirectional
characters. Hidden fields are held to the same rule because hidden today is not
hidden forever, and a store that already holds markup has to be sanitised on
the way out forever.

*A known limitation:* this makes it impossible to ship HTML, CSS, or JavaScript
**as subject matter** through a stored manifest — a lesson on the box model
cannot put `<div>` in a prompt. That is a deliberate trade while there is no
renderer, and those subjects are still teachable in conversation. Lifting it
needs a reviewed, explicitly escaped inert-code channel and the renderer that
would have to honour it.

**The schema and the runtime agree wherever a schema can say so.** Identifier,
locale and date patterns are published as strings and compiled from those same
strings; identifier lists reference the shared definition and advertise
uniqueness; bounded text advertises a pattern that refuses blank strings,
markup, and scheme-qualified URLs, generated from the same two declarations the
validator compiles; each component type advertises exactly the scoring modes it
accepts.

The rest of the content rules — paths, hosts, credential shapes, stylesheet
syntax — need alternation the two regex dialects disagree about, so they stay
runtime-only and the field description says so. Cross-field rules (an answer
that references an option, an objective that must match a stored one) are the
runtime's job too. `tests/test_schema_parity.py` runs representative invalid
values through a real JSON Schema implementation, the plugin's own validator,
and the manifest builder, so the agreement is *verified* rather than asserted —
and where the runtime is deliberately stricter, a test says which cases and
why.

The union of all 31 types is large — about 55 KB, of which the shapes every
type shares are emitted once under `$defs` rather than 31 times, cutting it
from over 140 KB. That is the price of a schema that refuses what the runtime
refuses; a future PR could trade it for a per-type lookup tool if the prompt
cost proves too high in practice.

**Accessibility metadata has one authoritative source: the operator.** An
experience declares `accommodations` from a closed vocabulary — captions,
transcript, text alternatives, visual description, keyboard-only, reduced
motion, no time limit, extended time, plain language — with
`source: profile_config`, checked against the profile's `config.yaml`. Matching
is exact on the canonical form, with no fuzzy, substring, or semantic step.

Two other sources existed and were removed, both for the same reason.
`explicit_request` was checked against the learner's temporary context — a row
the model had written in an earlier call. `confirmed_track` was checked against
a track's context, and one `save_context` call can create the track, set
`confirmed: true`, write the context, and supply the consent that supposedly
authorises it. A source a model can populate is not a source, so what remains
is a file a person edits.

**No accessibility need about a person is stored.** An accessibility need is
never written to storage, whatever consent accompanies it, and an `accessibility`
memory candidate is refused however it is presented. The consent statement, the
need, the track flag, the origin and the confirmation all arrive in one tool
call, written by one model, and Hermes exposes no confirmation event to check
them against. A gate whose every key is held by the party being checked is not
a gate. The need is honoured for the request that carries it, and the response
says plainly that nothing was kept.

**What the model asserts about the learner is recorded as the model's
proposal.** An `origin` claiming the learner stated, confirmed, corrected or
withdrew something is stored as `model_proposed`, and
`learner_confirmed`/`learner_declined` as `unconfirmed`. Hermes currently exposes
no host-backed confirmation event that could establish those claims. An owned
track proves scope, not that the learner spoke. `repeated_evidence` is
stored as sent, because it reports the agent's own observation. Every downgrade
is reported in the response. A replacement or removal must name a proposal
already stored for that learner.

**Candidate durability describes what actually happens.** `session` is returned
and never written, because this plugin has only durable SQLite and no
session-scoped store to be honest about. `short_term` is stored with an expiry
and swept on the next read. `durable` is kept.

There is deliberately **no free-text accessibility field** on the manifest. A
box to type in is a box a diagnosis eventually gets typed into, and this
metadata is neither consented to nor an appropriate place for one. Component
alt text and captions are free text, and are refused if they describe a person
rather than the component: a diagnosis, a disability label, or a sentence about
the learner cannot be stored. The same vocabulary stays legal in prompts and
content, because an exercise about glaucoma is an exercise about glaucoma.

**A declared accommodation must be deliverable.** `keyboard_only` alongside a
hotspot, labelling, matching, or drag-ordering component is refused unless that
component supplies a keyboard alternative; `captions` and `visual_description`
require the corresponding metadata on every asset-bearing component. Telling a
learner an exercise is usable when it is not is worse than claiming nothing.

Preparing an exercise writes no context, creates no track, and proposes no
memory candidate; exercise metadata never becomes a durable fact about a
person.

**Source references are described, not linked.** Title, author, publication
date, citation label, an approved source identifier, and a note. No URLs, no
paths, no credentials — this PR fetches nothing, so nothing may look
fetchable.

## Identity

The tools take no `learner_key`, `user_id`, or any other argument naming a
person, and a request containing one is refused. Identity comes from Hermes'
own session context:

```python
from gateway.session_context import get_session_env

get_session_env("HERMES_SESSION_PLATFORM")  # e.g. "telegram"
get_session_env("HERMES_SESSION_USER_ID")  # the platform's sender ID
```

The gateway binds those from the platform payload — for Telegram, the
message's `from.id` — before the agent runs, in a `contextvars.ContextVar`
that model output cannot reach. Hermes core trusts the same mechanism for
approval decisions.

The learner scope is `(profile, platform, sender ID)`, so the same numeric ID
on two platforms is two people, and one person keeps one record across their
DM and a group chat.

Those two variables are the plugin's entire dependency on host session state.
Conversation scope is deliberately not read: it played no part in
authorisation or storage, and `HERMES_SESSION_CHAT_TYPE` is not defined by
every Hermes version, so depending on it made behaviour vary by host for no
benefit.

**Anonymous multi-user sessions are refused.** If a gateway session is active
but carries no sender ID, the tools store nothing and say why, rather than
pooling strangers into a shared record. With no gateway at all — CLI, cron —
the profile is the principal, which is Hermes' own single-operator model.

A tool argument cannot override any of this, because there is no such
argument. That is the point: the previous design accepted a caller-supplied
`learner_key`, which meant anyone who could persuade the agent to pass a
guessed platform ID could read that person's record.

## Learning context

The context fields are deliberately subject-neutral — they describe *how*
someone is learning, never *what*, and no subject, language, or discipline is
the default:

`track_name`, `subject`, `goal`, `success_criteria`, `current_level`,
`target_level`, `prior_knowledge`, `knowledge_gaps`, `interests`,
`preferred_modalities`, `explanation_language`, `content_language`,
`session_duration`, `learning_horizon`, `assessment_preferences`,
`feedback_preferences`, `accessibility_needs`, `source_material`,
`constraints`.

### Precedence

Values disagree routinely. They resolve in this order, highest first:

```
current explicit request
  > explicit correction
  > active confirmed track
  > profile configuration
  > confirmed durable preferences
  > recent evidence
  > safe defaults
  > unconfirmed inference
```

Two consequences carry most of the weight. **What the learner says now wins** —
saved context never overrides someone who has just said something different.
And **defaults never overwrite anything**; they fill gaps. Resolution is
deterministic, and the losing candidates are returned rather than discarded so
a caller can explain why a value was chosen.

### Memory candidates are proposals, not writes

The plugin never imports, calls, or writes Hermes memory. It returns validated
*candidates*; only the agent decides whether any of them becomes a memory. The
save response says `hermes_memory_updated: false` every time.

A candidate may come only from an explicit durable preference, a confirmed
long-term goal, an explicit correction, an explicit withdrawal, or evidence
repeated often enough to be worth asking about. It may never come from one
error, one slow response, a single inference, momentary frustration, a raw
score, raw attempts, or session state — and it may never carry raw answers,
transcripts, session identifiers, tokens, credentials, or an inferred
disability or diagnosis.

Accessibility needs are **always session-only**, and session-only means absent
from the database rather than merely short-lived in it. Send them in
`current_request` to guide context resolution without storing them.

`accessibility_consent` is retained only as a compatibility/audit input. It is
validated and reported back, but cannot authorise persistence because the
statement and the need are both model-controlled:

```json
{
  "accessibility_consent": {
    "consent_statement": "please remember I need captions",
    "needs": ["captions on audio"]
  },
  "memory_candidates": [{
    "category": "accessibility",
    "statement": "captions on audio",
    "consented_need": "captions on audio",
    "origin": "explicit_durable_preference",
    "confirmation_state": "learner_confirmed",
    "evidence_summary": "Asked for this to be remembered"
  }]
}
```

The candidate in that example is returned as rejected and no accessibility
row is written. There is no model argument, consent quotation, confirmation
flag, or track record that changes this. Manifest accessibility metadata is
authorised only by operator profile configuration, which a tool call cannot
create or modify.

## Roadmap

Deliberately **not** here yet: the exercise delivery runtime, card renderers,
attempt and score storage, the FastAPI dashboard and Mini App, Telegram
authentication, frontend code, Cloudflare tunnels, slash commands, managed
asset import, image generation, progress dashboards, and any scheduler. Each
lands in a later PR. The skill describes how the agent will use those
capabilities and instructs it to fall back to chat until they exist.

Manifests are validated and stored, but nothing reads them back to a learner
yet: `learning_studio_prepare` is the data contract a renderer will later
consume, written down now so that the wire format is settled before there is
data in it.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Hermes is not on PyPI and is not a dependency, so the tests that exercise the
host's real skill machinery are opt-in. Point them at a checkout of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent):

```bash
HERMES_AGENT_SRC=/path/to/hermes-agent uv run pytest tests/test_hermes_integration.py
```

They skip when the variable is unset, so CI stays self-contained. Run them
before changing how the skill loads its references.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
