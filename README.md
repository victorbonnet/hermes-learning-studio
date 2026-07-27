# hermes-learning-studio

A standalone [Hermes](https://hermes-agent.nousresearch.com) plugin for adaptive
learning: structured study sessions built on active recall and spaced
repetition.

> **Status: early development foundation.** This is not the feature-complete
> public release. What exists today is a single bundled skill — agent guidance
> and nothing else: **no tools, no storage, no progress persistence, no Mini
> App, and no network requests.** Study sessions run as ordinary conversation,
> and nothing carries over between them. See [Roadmap](#roadmap) for what is
> still to come.

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
skill_view("learning-studio:adaptive-learning", "references/selection-cards.md")
```

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
│   └── skills/
│       └── adaptive-learning/
│           ├── SKILL.md        # The orchestration workflow
│           └── references/     # Loaded on demand via skill_view(name, path)
└── tests/                      # Unit tests against a fake plugin context
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
beside it. The catalogue is *not* registered as fourteen skills: Hermes'
progressive disclosure means `skill_view(name)` returns SKILL.md and
`skill_view(name, "references/selection-cards.md")` returns one reference, so
the agent pays for only what the current decision needs. Tests assert that
every reference is linked by a valid relative path, that none is orphaned, and
that the registered surface stays at exactly one skill.

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

**No runtime dependencies.** `dependencies` is empty, so installing the plugin
adds nothing to a user's Hermes environment. PyYAML is a test-only dependency
in the `dev` extra — the plugin code itself imports only the standard library.

Reserved for later PRs: the toolset name `plugin_learning_studio`.

## Configuration

Behavioural settings belong in Hermes' `config.yaml`. Secrets belong in `.env`
and are never committed. This foundation reads neither.

## Roadmap

Deliberately **not** here yet: runtime tools, SQLite persistence, a manifest
renderer or validator, the FastAPI dashboard and Mini App, Telegram
authentication, frontend code, Cloudflare tunnels, slash commands, managed
asset import, and any scheduler. Each lands in a later PR. The skill describes
how the agent will use those capabilities and instructs it to fall back to
chat until they exist.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
