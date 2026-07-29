# hermes-learning-studio

A standalone [Hermes](https://hermes-agent.nousresearch.com) plugin for adaptive
learning: structured study sessions built on active recall and spaced
repetition.

> **Status: early development.** This is not the feature-complete public
> release. What exists today is a bundled skill plus four tools: two that
> remember a learner's **context** — their goals, level, preferences, and
> confirmed learning tracks — and one that validates and stores the
> **exercises** an agent designs, plus a secure managed-image importer, all in
> profile-scoped storage.
>
> There is now a **secure API** behind the optional `web` extra: a
> Telegram-authenticated FastAPI service that serves a stored exercise to its
> owner and collects their responses. See
> [Telegram Mini App API](#telegram-mini-app-api).
>
> There is still **no delivery runtime**: no card renderer, no Mini App
> frontend, no scoring engine, no scheduler, and nothing that starts a server
> or opens a tunnel. Responses collected by the API live in the session and are
> not marked or persisted. See [Roadmap](#roadmap) for what is still to come.

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

Managed image import uses Pillow, but plugin import, registration, context, and
exercise preparation do not. Package installs can opt into it with the
`media` extra:

```bash
pip install "hermes-learning-studio[media] @ git+https://github.com/victorbonnet/hermes-learning-studio.git"
```

For a directory-plugin install, install Pillow into the same Python environment
that runs Hermes. Without it, the plugin and its other three tools continue to
work; `learning_studio_import_asset` returns a safe, actionable error.

Managed publication also requires descriptor-relative filesystem operations
(`dir_fd`, `O_DIRECTORY`, and `O_NOFOLLOW`) so validation cannot be bypassed by
a directory-swap race. Platforms without those primitives, including Windows,
fail closed for `learning_studio_import_asset`; plugin registration, learning
context, and exercise preparation remain available. No pathname-only fallback
is used because it would weaken the managed-storage security boundary.

Numeric DPI/resolution metadata is accepted because it is structural and does
not carry an opaque payload. ICC profiles, EXIF/XMP, comments, text chunks, and
unrecognised application segments remain rejected because accepted source bytes
are preserved exactly and those containers can carry private arbitrary data.
If a database transaction fails after publication, retained live file and
managed-directory descriptors bind cleanup to the exact published inode: it is
truncated and locked to mode `000`; a zero-byte tombstone is safer than unlinking
a basename that a concurrent writer may have replaced.

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
│   ├── assets.py               # Lazy image validation and atomic managed copies
│   ├── service.py              # Reads, writes, ownership, consent gates
│   ├── schemas.py              # JSON schemas for the four tools
│   ├── tools.py                # Tool handlers
│   ├── telegram_auth.py        # Mini App initData verification (stdlib only)
│   ├── authorization.py        # Allowlist intersection — narrows, never widens
│   ├── sessions.py             # Expiring, opaque, in-memory Mini App sessions
│   ├── web/                    # Optional `web` extra — nothing else imports it
│   │   ├── dependencies.py     # The single injection point
│   │   ├── security.py         # Headers, body limits, rate limits, redaction
│   │   └── app.py              # The protected API
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
declares no `requires_env` — so enabling the plugin cannot break a session.
Pillow stays behind a lazy import inside the code path that needs it, and
FastAPI lives entirely inside `learning_studio/web/`, which nothing on the
plugin surface imports; a test blocks both modules at import time and asserts
registration still succeeds.

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

**`register()` opens no database.** It registers a skill and four tools and
returns. Initialising storage at startup would let a corrupt or
newer-versioned database take the whole plugin down, instead of failing one
tool call with a message the agent can act on.

**No mandatory runtime dependencies.** `dependencies` is empty, persistence
uses the standard library's `sqlite3`, and Pillow is isolated in the optional
`media` extra. Tests block FastAPI, Pillow, Telegram, HTTP clients, and PyYAML
at import time and still require the plugin and all four tool schemas to
register; only a real image import needs Pillow.

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

  # Managed-image safety limits, enforced before and during decoding.
  max_asset_bytes: 10485760
  max_asset_width: 8192
  max_asset_height: 8192
  max_asset_pixels: 40000000

  # ── Telegram Mini App API (optional `web` extra) ─────────────────────
  # Behaviour only. The bot token stays in .env; there is no setting for it.

  # How long a Mini App session stays usable, in seconds (60–86400).
  mini_app_session_ttl_seconds: 1800

  # How old signed initData may be when a session is opened (30–3600).
  mini_app_init_data_max_age_seconds: 300

  # Largest accepted request body, in bytes (512–1048576).
  mini_app_max_request_bytes: 16384

  # Sliding-window rate limit, per Telegram user and per session.
  mini_app_rate_limit_requests: 60
  mini_app_rate_limit_window_seconds: 60

  # Upper bound on concurrently held sessions (1–100000).
  mini_app_max_sessions: 500

  # Optional NARROWING of the profile's Telegram allowlist. Empty means no
  # additional restriction. It can never add a user the profile excludes.
  mini_app_allowed_telegram_users: []

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
every setting here governs retention, isolation, privacy, or resource safety,
and a misspelled
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

Validated images are copied to the sibling `assets/` directory with opaque
filenames and owner-only permissions. Original source paths are never stored.

## Tools

Four tools, all in the `plugin_learning_studio` toolset. None takes a learner
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

### `learning_studio_import_asset`

Imports PNG, JPEG, or single-frame WebP bytes from the active profile's trusted
Hermes image cache. It detects MIME from bytes, fully verifies the image,
enforces byte/dimension/pixel limits, requires meaningful alternative text (or
an explicit decorative declaration), rejects embedded private metadata, hashes
and deduplicates inside the exact learner/track scope, and atomically copies the
bytes into managed storage. On a duplicate, the first import's metadata remains
immutable and the response explicitly names any conflicting submitted fields.

The response contains an opaque `asset_id` and safe metadata. It never returns
or persists the original local path, and it never returns a stored generation
prompt. Visual manifest components accept only an asset owned by the current
learner in the experience's exact track scope, with the same alternative text
recorded at import. The tool does not generate images, serve files over HTTP,
or open a UI.

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

## Telegram Mini App API

An optional FastAPI service that serves a stored exercise to the person who
owns it, over a Telegram-authenticated, same-origin API. It is **not started by
the plugin**: nothing in this release launches a server, opens a tunnel, or
sends a Telegram button. This PR builds the boundary; process lifecycle and the
frontend land later.

```bash
uv pip install "hermes-learning-studio[web]"
```

Nothing outside `learning_studio/web/` imports FastAPI, and `register(ctx)`
never reaches that package — an install without the extra keeps a fully working
plugin, which tests assert by blocking the module at import time.

### Authentication

Every request carries the raw Telegram `initData` string in an
`X-Telegram-Init-Data` header, verified per Telegram's specification:

- the data-check string is every field except `hash` — sorted, `key=value`,
  joined with `\n`;
- the key is `HMAC-SHA256(key="WebAppData", data=<bot token>)`;
- the comparison is `hmac.compare_digest`, never `==`;
- `auth_date` must be recent (300 s to open a session) and no more than 60 s in
  the future, a stated tolerance for clients whose clocks run fast;
- the `user` object must name a real, non-bot numeric ID — and **only that ID
  is kept**. Names, usernames, language, and photo are discarded.

**`signature` is part of the signed data.** Telegram documents two validation
algorithms that exclude different fields, and mixing them up breaks
authentication outright:

| Algorithm | Used when | Excluded from the check string |
| --- | --- | --- |
| HMAC-SHA-256 with the bot token (**used here**) | the verifier holds the token | `hash` only |
| Ed25519 third-party signature | the verifier has no token | `hash` and `signature` |

Applying the Ed25519 exclusion to the HMAC path rejects every launch from a
client that sends `signature`, which current clients do. Both reference
implementations agree: `@telegram-apps/init-data-node` skips the field in
`validate3rd` and signs it in `validate`, and aiogram's
`check_webapp_signature` pops only `hash`. The test fixtures are built from
those implementations rather than from prose, and the default fixture carries a
`signature` field so the modern payload shape is what the suite exercises.

A launch from a group, supergroup, or channel is refused: a Mini App session
reads one person's learning record, which is not a room's business.

The bot token is read from `TELEGRAM_BOT_TOKEN` in `.env`, where Hermes already
keeps it. It is never copied into `config.yaml`, a database row, a response, a
log line, or a process argument, and the raw `initData` payload is never stored
anywhere.

### Authorisation is an intersection

```
effective access = profile Telegram DM allowlist  ∩  plugin restriction
```

`mini_app_allowed_telegram_users` can only **narrow** the profile side. There
is no setting that adds a user, because a plugin able to widen the host's
allowlist would be a privilege-escalation feature with a configuration file for
an interface.

A Telegram DM passes **two** gates in Hermes, in order, and a sender must clear
both. Mini App access is bounded by both, so it cannot exceed either.

1. **Adapter intake** —
   `plugins/platforms/telegram/adapter.py::_is_user_authorized_from_message`
   runs before batching, event construction, and the runner. Its own comment:
   *"Adapter-level allow_from / group_allow_from: when set, they are the sole
   authority."* The test is `if adapter_allow_from is not None`, so a present
   but **empty** `allow_from` authorises nobody, and a message from anyone
   outside it never reaches the rest of Hermes.
2. **Runner authorisation** —
   `gateway/authz_mixin.py::_is_user_authorized` then decides from
   `TELEGRAM_ALLOWED_USERS ∪ GATEWAY_ALLOWED_USERS` when any environment
   allowlist is configured, falling back to `allow_from` when none is.

`allow_from` is read from every shape Hermes accepts: `platforms.telegram` and
`gateway.platforms.telegram`, with the key written directly or inside `extra`
(`gateway/config.py` bridges the former into the latter).

The effective upper bound is therefore the intersection:

| `allow_from` | environment allowlist | Mini App upper bound |
| --- | --- | --- |
| present (ids) | configured | ids ∩ environment |
| present (ids) | absent | ids |
| present but empty | anything | nobody |
| absent | configured | environment |
| absent | absent | nobody |

Two earlier versions got this wrong in opposite directions, and both broadened
host access: unioning the two authorised anyone named in either, and letting
the environment *win* over a present `allow_from` authorised a user the adapter
drops at intake — with `allow_from: ["1001"]` and `TELEGRAM_ALLOWED_USERS=2002`,
Hermes never delivers a message from 2002, yet the Mini App would have admitted
2002. Intersecting is at least as strict as either gate alone, which is the only
property that makes "may narrow, never broaden" true rather than intended.

**`allow_admin_from` is not an authorisation source.** Hermes reads it only in
`gateway/slash_access.py`, to decide which *already authorised* users may run
privileged slash commands. Treating it as an access grant would let a user
excluded from Telegram entirely reach the Mini App.

### Deliberately not honoured

Each of these can only ever *deny* somebody Hermes would allow — never admit
somebody Hermes would deny — which is the safe direction for a plugin that has
promised never to broaden access:

| Not honoured | Why |
| --- | --- |
| `allow_from: ["*"]` and other wildcards | A wildcard opens a chat bot to everyone; it does not open one person's learning record to everyone. It grants nothing here — though, as in Hermes, it does remove the intake bound, so a wildcard beside an environment allowlist still authorises the users that allowlist names instead of denying them for the operator's choice of shorthand. Name the IDs. |
| `GATEWAY_ALLOW_ALL_USERS`, `TELEGRAM_ALLOW_ALL_USERS` | Same reason. |
| `TELEGRAM_GROUP_ALLOWED_USERS`, `TELEGRAM_GROUP_ALLOWED_CHATS`, `group_allow_from`, `group_allowed_chats` | Authorise participation in a room, not access to a personal record. Counted only when deciding whether *any* environment allowlist exists. |
| DM pairing approvals | A first-class grant in Hermes, stored outside configuration; reading it would mean reaching into host internals. Hermes writes approvals into the allowlist whenever one is configured, so the gap is narrow — the remedy is naming the user in `TELEGRAM_ALLOWED_USERS`. |

An empty allowlist authorises nobody, and a host configuration that cannot be
read raises rather than resolving to "empty".

### Sessions

Opening a session exchanges fresh `initData` and an `experience_id` for an
opaque random token, scoped to `(profile, Telegram user, learner, track,
experience)` and expiring after `mini_app_session_ttl_seconds`. Sessions live
in memory only — a bearer token is never written to the database or to disk,
and a restart ends every session. The store keeps the SHA-256 digest of the
token, so the token itself exists only in the response that minted it.

Both credentials are checked on **every** request: the Telegram payload and the
session token, and the token is refused if the Telegram account presenting it
is not the one it was minted for.

Telegram signs `initData` once, at launch, so the freshness bound for requests
*inside* a session is `session TTL + bootstrap window` — enough for a payload
that was already near the bootstrap limit to see the session out. Taking the
maximum of the two instead would quietly shorten every session opened with
slightly stale `initData`, returning 401 while the session store still
considered it live. The session's own hard expiry is what bounds its life, and
the advertised `expires_in_seconds` is the figure a caller actually gets. A
session may be continued with the launch that opened it or a newer one, never
with an older captured payload.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Authenticated liveness |
| `POST` | `/api/session` | Bootstrap a session for one experience |
| `GET` | `/api/session/component` | The component currently in view |
| `POST` | `/api/session/answer` | Record a response and advance |
| `GET` | `/api/session/result` | Progress summary for the session |
| `GET` | `/api/assets/{id}` | One managed image, verified on the way out |

There is no public route, and no interactive docs or OpenAPI schema is
published. Answers are recorded **in the session** and nothing is marked:
grading and durable attempt storage arrive with the evaluation runtime, and
every completion response says so rather than implying a score exists.

An asset is served only when the caller owns it *and* the session's own
experience references it, and its bytes are re-hashed against the recorded
digest on every delivery — an image swapped on disk after import is not served.

### What the API cannot leak

Responses are built from the stored **learner payloads**, which were
constructed from an allowlist when the experience was prepared and never
contained an answer key, rubric, hint, feedback string, or branch. The
evaluator-only tables are not queried anywhere in the web package. A test drives
the whole flow and asserts that no canary from any hidden field appears in any
response body.

Errors say as little as possible: an experience that does not exist, one owned
by another learner, and one in another profile are the same 404, and an
invalid, expired, or someone-else's session token are the same 401.

### Limits and headers

Per-user and per-session sliding-window rate limits, a bounded answer shape
(size, breadth, nesting), and a request body ceiling enforced **before**
buffering: an oversized or malformed `Content-Length` is refused having read
nothing at all, and a chunked body that declares no length is read chunk by
chunk and abandoned the moment the cumulative size crosses the limit. The peak
cost of an oversized request is one chunk, not the payload. Reading the body
and measuring afterwards would make the ceiling a formality — the memory is
already spent by the time the 413 is written.

On every response:

```
Content-Security-Policy: default-src 'none'; base-uri 'none'; form-action 'none';
  frame-ancestors https://web.telegram.org https://telegram.org;
  img-src 'self' data:; connect-src 'self'; script-src 'none'; style-src 'none';
  object-src 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cache-Control: no-store, no-cache, must-revalidate, private
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Permissions-Policy: accelerometer=(), camera=(), geolocation=(), …
Strict-Transport-Security: max-age=31536000; includeSubDomains
```

No CORS headers are ever sent, so a cross-origin page cannot read a response;
the required custom header means such a request needs a preflight this server
does not answer. `X-Frame-Options` is deliberately absent — it cannot express
"only Telegram", so `frame-ancestors` does that job alone.

Logs are structured JSON restricted to an allowlist of fields. A user appears
as a per-process pseudonym, a session as a digest prefix; the `initData`
payload, the bot token, and the session token never reach a log line.

That holds on the 500 path too, which is where it is easiest to lose. An
unexpected failure is recorded as `{"event": "unhandled_error", route, method,
status, reason}` where `reason` is the exception's *class name* — chosen by a
programmer — and **no exception message or traceback is logged at all**. A
failing query or service call can be holding a filesystem path, a SQL fragment,
a bot token, or an answer key, and `logger.exception` would write all of it past
every other redaction.

This trade costs something and is worth stating: diagnosing a 500 here means
reproducing it. A middle option — a second logger, silenced by default, that an
operator could opt into — was implemented and removed, because a `LogRecord`
carrying `exc_info` is a live object holding those values and anything that
captures records broadly renders it regardless of that logger's configuration.
pytest's own log capture does exactly that. A record never constructed is the
only one that cannot be captured, and an "off by default" switch is a switch
that gets flipped by accident.

### Testing without a bypass

There is no `DEV_MODE`, no `SKIP_AUTH`, and no flag that disables verification.
Tests inject a `Dependencies` object — a fake bot token, allowlist, and clock —
and then sign real payloads with that token, so the same HMAC path runs in tests
as in production. A test parses the API sources and fails if an identifier that
looks like a bypass switch ever appears.

## Roadmap

Secure managed image import **is** here: `learning_studio_import_asset`
validates and stores PNG, JPEG, and WebP images in profile-scoped managed
storage, and `learning_studio_prepare` authorises every referenced asset. See
[Managed assets](#learning_studio_import_asset).

Telegram authentication, session scoping, and the protected API **are** here,
behind the `web` extra — including managed asset delivery. See
[Telegram Mini App API](#telegram-mini-app-api).

Deliberately **not** here yet: image-generation providers, the card renderers
and frontend code, anything that starts or supervises the server process,
Cloudflare tunnels, slash commands, sending a Telegram launch button,
launch/status/stop tools, scoring, attempt and score storage, progress
dashboards, and any scheduler. Each lands in a later PR. The skill describes how
the agent will use those capabilities and instructs it to fall back to chat
until they exist.

An exercise can now be *served* over the API, but it is still not *rendered*:
there is no client, so the agent continues to deliver exercises in conversation.
Responses collected by the API are held in the session and are never marked or
stored.

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
