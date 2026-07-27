# Contributing

Thanks for helping out. This is a standalone Hermes plugin — it is developed
and distributed on its own, not merged into `NousResearch/hermes-agent`.

## Setup

Requires Python 3.11 or newer.

```bash
git clone https://github.com/victorbonnet/hermes-learning-studio.git
cd hermes-learning-studio
uv venv && uv pip install -e ".[dev]"
```

## Checks

All three must pass; CI runs them on Python 3.11 and 3.12.

```bash
uv run ruff format --check .   # formatting
uv run ruff check .            # lint
uv run pytest                  # tests
```

Run `uv run ruff format .` to fix formatting.

## Testing against the Hermes API

Hermes is not a dependency of this repository. Tests exercise the host
contract through `tests/fake_hermes.py`, a stand-in for `PluginContext` that
mirrors the real validation rules (skill names must match `[a-zA-Z0-9_-]+` and
must not contain `:`; the skill path must exist).

When you use a new part of the `ctx` API, add the corresponding method to the
fake and copy the host's validation with it — a permissive fake is worse than
no fake, because it turns a startup crash into a green test run.

Tests must not make real network calls. No Telegram, Cloudflare, or
image-provider requests, and nothing that reads a real credential.

## Ground rules

- **Use public plugin APIs only.** Never patch or vendor Hermes internals.
- **Keep `register(ctx)` cheap and total.** Hermes calls it at startup for
  every enabled plugin. It must not import optional dependencies at module
  scope, touch the network, or raise — a failing `register()` disables the
  plugin.
- **Put optional dependencies behind lazy imports** inside the function that
  needs them, and declare them as extras in `pyproject.toml`.
- **Resolve paths with the profile-safe helper.** Hermes supports multiple
  profiles via `HERMES_HOME`, so never hardcode `~/.hermes`. Use the host's
  `get_hermes_home()` — imported lazily, inside the function that needs a
  path, so the plugin stays importable outside a Hermes process. This release
  reads and writes no paths.
- **Behavioural settings go in `config.yaml`; secrets go in `.env`.** Never
  commit real tokens, user IDs, credentials, domains, or private paths — not
  even in examples. Use obvious placeholders.
- **Keep the bundled skill truthful.** Do not describe a tool that is not
  registered. An aspirational skill file sends the agent after something that
  does not exist.

## Pull requests

Branch from `main`, keep the change focused, and use
[Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
`docs:`, `test:`, `chore:`).

Describe what changed, how you verified it with real command output, and any
security or privacy implications. Do not force-push shared branches, and do not
commit directly to `main`.
