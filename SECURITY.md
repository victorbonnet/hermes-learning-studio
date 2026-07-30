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
- **Authorisation lives in the query.** Every learner-owned read carries
  `profile_id` and `learner_id` in its `WHERE` clause, so a handler that forgot
  a check still could not reach another learner's data — there is no query that
  can. Not-found and not-yours return the same message.
- **No mandatory runtime dependencies.** `dependencies` is empty, so installing
  the plugin adds no third-party code to a user's Hermes environment. Pillow and
  FastAPI are optional extras, and `register(ctx)` reaches neither.

## What this plugin actually does

Stated plainly, because a security policy that understates the surface is worse
than none:

- **Four tools**, which read and write learner context, validate and store
  exercises, and import managed images.
- **Profile-scoped SQLite storage** under the Hermes home. Learner identifiers
  are stored as salted HMAC digests rather than in the clear — a lookup key, not
  an authorisation check, and not a claim that identity is unrecoverable by
  somebody holding the file.
- **Managed image assets** on the filesystem, validated on import and re-hashed
  against the recorded digest on every delivery. Publication uses
  descriptor-relative operations so validation cannot be raced by a directory
  swap, and fails closed on platforms without them.
- **An optional Telegram Mini App**, behind the `web` extra: a FastAPI service
  and a static frontend. Nothing starts it — this release launches no server and
  opens no tunnel — but if an operator runs it, the surface is:
  - every `/api/` route requires verified Telegram `initData` *and* membership of
    the profile's allowlist, and all but the bootstrap require a session token
    minted for that same Telegram account;
  - the five static frontend files are public, because a webview cannot attach a
    header to the navigation that loads a page. They are byte-identical for every
    caller and contain no learner data;
  - one route, `POST /api/session/reveal`, discloses one field of one component
    type (a flashcard's back) after an attempt has been committed and frozen.
    Nothing else from the evaluator-only half is reachable through the API.
- **No outbound network requests.** The plugin calls no external service. The one
  external resource the frontend loads is Telegram's own Web App SDK, from
  `telegram.org`, named explicitly in the document's Content-Security-Policy.

## Reporting scope

Findings in any of the above are in scope. Particularly welcome: a path by which
an answer key, rubric, hint, or another learner's data becomes reachable; a way
past the allowlist or the session scope; or content from a manifest that becomes
executable in the Mini App.

Skill content reaches the model, so treat it as untrusted input to the agent:
Hermes logs a warning when skill content resembles a prompt-injection attempt
but still serves it. Review changes to `SKILL.md` with that in mind.
