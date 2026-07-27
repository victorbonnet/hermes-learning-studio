# hermes-learning-studio

A standalone [Hermes](https://hermes-agent.nousresearch.com) plugin for adaptive
learning: structured study sessions built on active recall and spaced
repetition.

> **Status: early development.** This is not the feature-complete public
> release. What exists today is a bundled skill plus two tools that remember a
> learner's **context** — their goals, level, preferences, and confirmed
> learning tracks — in profile-scoped SQLite.
>
> There is still **no exercise runtime**: no card renderer, no manifest
> validator, no Mini App, no scoring, no scheduler, and no network requests.
> Exercises run as ordinary conversation, and attempts, answers, and scores
> are not persisted. See [Roadmap](#roadmap) for what is still to come.

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
│   ├── service.py              # Reads, writes, ownership, consent gates
│   ├── schemas.py              # JSON schemas for the two tools
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

**Migrations are all-or-nothing, and never destructive.** Each migration runs
in its own transaction and rolls back completely on failure, because a
half-applied schema is harder to recover from than a failed startup. A
database written by a newer version of the plugin is refused with an
explanation and left untouched — deleting or "resetting" it would destroy a
learner's record to make the code happy.

**`register()` opens no database.** It registers a skill and two tools and
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

  # Operator policy, not consent. Accessibility needs are session-only by
  # default regardless; this only decides whether they *may* be stored
  # durably when a learner explicitly asks. Set false on a shared or managed
  # profile to refuse even on request.
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

Two tools, both in the `plugin_learning_studio` toolset. Neither takes a
learner argument: identity is resolved from the Hermes session, so a call
always reads and writes the context of whoever sent the current message.

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

Accessibility needs are **session-only by default**, and session-only means
absent from the database rather than merely short-lived in it. Send them in
`current_request` to have them applied without being stored. Storing one
requires `accessibility_consent` naming that exact need and quoting what the
learner said — consent for one need is not consent for another, and a
sensitive candidate additionally requires `confirmation_state:
learner_confirmed` and an origin that is the learner stating it. Repeated
evidence can never produce a diagnosis or disability candidate.

## Roadmap

Deliberately **not** here yet: the exercise runtime and manifest validator,
card renderers, the FastAPI dashboard and Mini App, Telegram authentication,
frontend code, Cloudflare tunnels, slash commands, managed asset import,
image generation, progress dashboards, and any scheduler. Each lands in a
later PR. The skill describes how the agent will use those capabilities and
instructs it to fall back to chat until they exist.

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
