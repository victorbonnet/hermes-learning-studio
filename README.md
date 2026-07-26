# hermes-learning-studio

A standalone [Hermes](https://hermes-agent.nousresearch.com) plugin for adaptive
learning: structured study sessions built on active recall and spaced
repetition.

> **Status: foundation release (v0.1.0).** This release registers a single
> bundled skill and nothing else — no tools, no storage, no dashboard. Sessions
> run as ordinary conversation and nothing is persisted between them. See
> [Roadmap](#roadmap) for what is deferred.

## Install

As a directory plugin:

```bash
git clone https://github.com/victorbonnet/hermes-learning-studio.git \
  ~/.hermes/plugins/learning-studio
hermes plugins enable learning-studio
```

Or from a checkout, as a pip-installed plugin:

```bash
pip install .
hermes plugins enable learning-studio
```

Plugins are opt-in, so the `enable` step is required either way. Verify with
`hermes plugins list`, or `HERMES_PLUGINS_DEBUG=1 hermes plugins list` if the
plugin does not appear.

## Usage

Ask the agent to load the skill:

```
skill_view("learning-studio:adaptive-learning")
```

Plugin skills are namespaced by the manifest name and are deliberately absent
from the system prompt's `<available_skills>` index — they are explicit,
opt-in loads.

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
│           └── SKILL.md        # Bundled, read-only skill
└── tests/                      # Unit tests against a fake plugin context
```

Four decisions shape this foundation:

**One identity, two install paths.** Hermes derives a plugin's skill namespace
from its manifest `name` for directory installs, and from the entry-point name
for pip installs. `plugin.yaml` and the `hermes_agent.plugins` entry point in
`pyproject.toml` therefore both say `learning-studio`, so the skill resolves as
`learning-studio:adaptive-learning` however it was installed. A test asserts the
manifest name and the registered namespace agree.

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

**The skill tells the truth about its own scope.** `SKILL.md` states plainly
that this release has no tools and no persistence, because a skill that implies
otherwise sends the agent after tools that do not exist. A test fails if the
skill body references tool names while `register()` registers no tools.

Reserved for later PRs: the toolset name `plugin_learning_studio`.

## Configuration

Behavioural settings belong in Hermes' `config.yaml`. Secrets belong in `.env`
and are never committed. This release reads neither.

## Roadmap

Deliberately **not** in this release: runtime tools, SQLite persistence, the
FastAPI dashboard, Telegram authentication, frontend code, tunnels, and image
handling. Each lands in a later PR.

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
