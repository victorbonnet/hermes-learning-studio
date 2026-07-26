# Security Policy

## Supported versions

This project is pre-1.0. Only the latest release receives fixes.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Reporting a vulnerability

Please report privately — do not open a public issue.

Use [GitHub private vulnerability reporting](https://github.com/victorbonnet/hermes-learning-studio/security/advisories/new)
for this repository.

Include the affected version, reproduction steps, and the impact you believe it
has. Expect an acknowledgement within 7 days and an assessment within 30. Please
give us a chance to ship a fix before disclosing publicly.

Vulnerabilities in Hermes itself belong to
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).

## Security model

A Hermes plugin runs **in-process with the agent**, with the agent's full
privileges. It is not sandboxed. Installing a plugin is equivalent to running
its code, which is why Hermes keeps plugins opt-in behind
`hermes plugins enable`.

Constraints this project holds itself to:

- **No secrets in the repository.** Credentials belong in `.env`, which is
  gitignored. Behavioural settings belong in `config.yaml`. Committed examples
  use placeholders — never a real token, user ID, domain, or path.
- **No network calls in tests.** Tests never contact Telegram, Cloudflare, or
  an image provider, and never read a real credential.
- **Profile-safe paths.** Hermes supports multiple profiles via `HERMES_HOME`;
  hardcoding `~/.hermes` would read or corrupt another profile's data. Use the
  host's `get_hermes_home()`.
- **Least surface.** The plugin registers only what it needs. Today that is one
  read-only skill: no tools, no hooks, no filesystem access, no network
  requests, and no data collected, stored, or transmitted.
- **No runtime dependencies.** `dependencies` is empty, so installing the
  plugin adds no third-party code to a user's Hermes environment and brings no
  transitive supply-chain surface with it.

## Scope of this foundation

This is an early development foundation, not the feature-complete public
release. It ships one bundled skill and no executable tools. It has no storage
and does not persist progress, no Mini App or web surface, and makes no network
requests — so its attack surface is the skill text itself.

Skill content reaches the model, so treat it as untrusted input to the agent:
Hermes logs a warning when skill content resembles a prompt-injection attempt
but still serves it. Review changes to `SKILL.md` with that in mind.
