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
- **No network calls in tests.** Tests never contact Telegram, Cloudflare, an
  image provider, or public DNS, and never read a real credential. Two tests
  start the real runtime on a loopback port with a shell-script stand-in for
  `cloudflared`; nothing leaves the machine.
- **The model controls no machine.** No tool payload accepts a host, port, URL,
  executable, command, argument, process id, timeout, environment variable,
  lock path, chat, account, or profile. Those come from the operator's
  `config.yaml` and from Hermes' own session context, and there is no property
  in any schema through which one could be supplied.
- **No process is signalled without proof, and no signal is sent to a bare
  number.** Ownership is a challenge answered over a loopback control endpoint
  using a secret passed in the child's environment; a recycled pid cannot answer
  it, and a runtime that cannot be proved is left strictly alone. Escalation
  additionally holds a pid file descriptor across the proof and the signal, so
  the identity cannot be recycled in between — and where no such handle exists
  (macOS), escalation refuses rather than signalling a number.
- **Launch authority comes from the platform, not the model.** The agent may
  read what a learner meant; it cannot supply their words. A launch requires a
  quotation that appears in the message Hermes actually delivered for the
  current turn, recorded before the model ran, and one message authorises one
  launch once.
- **Credentials are resolved per profile.** The bot token and Telegram
  allowlists come from Hermes' profile-scoped secret API, not `os.environ`,
  because a multiplexed process may hold another profile's.
- **No shell, ever.** Every child process is started from an argument array.
  `os.system`, `os.popen`, the `exec`/`fork` family and `shell=True` appear
  nowhere in the package, and a source-scanning test names the four files
  permitted to start a process at all.
- **Nothing is downloaded or installed.** `cloudflared` is an operator
  prerequisite. The plugin never fetches a binary, never invokes a package
  manager, and never asks for privilege.
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

- **Eight tools**, which read and write learner context, validate and store
  exercises, import managed images, and open, inspect, report on and close an
  exercise on the learner's screen.
- **Profile-scoped SQLite storage** under the Hermes home. Learner identifiers
  are stored as salted HMAC digests rather than in the clear — a lookup key, not
  an authorisation check, and not a claim that identity is unrecoverable by
  somebody holding the file.
- **Managed image assets** on the filesystem, validated on import and re-hashed
  against the recorded digest on every delivery. Publication uses
  descriptor-relative operations so validation cannot be raced by a directory
  swap, and fails closed on platforms without them.
- **A Telegram Mini App**, behind the `web` extra: a FastAPI service and a
  static frontend. Its surface is:
  - every `/api/` route requires verified Telegram `initData` *and* membership of
    the profile's allowlist, and all but the bootstrap require a session token
    minted for that same Telegram account;
  - on a runtime this plugin launched, opening a session **additionally**
    requires an activated, unexpired launch grant issued to that account in this
    runtime generation, named by a selector the button carried in its URL
    fragment — so knowing the public address is not enough to open anything, and
    a client cannot name an exercise of its own;
  - the five static frontend files are public, because a webview cannot attach a
    header to the navigation that loads a page. They are byte-identical for every
    caller and contain no learner data;
  - one route, `POST /api/session/reveal`, discloses one field of one component
    type (a flashcard's back) after an attempt has been committed and frozen.
    Nothing else from the evaluator-only half is reachable through the API.
- **Two child processes, on demand.** `learning_studio_launch` starts the Mini
  App service bound to a configured loopback address, and that service starts
  `cloudflared` to expose it. Both are started from argument arrays, in a new
  session so the process group holds them and nothing else, with an environment
  built by naming every variable rather than by copying and subtracting. The
  tunnel child gets neither the bot token nor `HERMES_HOME`.
- **A temporary public address.** While a runtime is open, a
  `*.trycloudflare.com` hostname reaches the loopback service from the internet.
  It is assigned by Cloudflare, needs no account, and is withdrawn when the
  runtime stops — which it does after an idle period and again at an absolute
  maximum lifetime, both enforced inside the runtime itself. What an anonymous
  visitor to that address can reach is the five public frontend files; every
  route that returns learner data still requires all four gates above.
- **One outbound request, to one host.** A launch sends one Telegram Bot API
  `sendMessage` carrying a Web App button. It is the only remote host the plugin
  contacts, from the only module that can, and a test asserts that. The bot token
  is read from the environment per send and never enters a log, a response, an
  exception, a record, or a process argument. The one external resource the
  frontend loads is Telegram's own Web App SDK, from `telegram.org`, named
  explicitly in the document's Content-Security-Policy.

## Reporting scope

Findings in any of the above are in scope. Particularly welcome: a path by which
an answer key, rubric, hint, or another learner's data becomes reachable; a way
past the allowlist, the session scope, or a launch grant; content from a
manifest that becomes executable in the Mini App; a string `cloudflared` could
print that this plugin would accept as a Cloudflare address; a way to make it
signal a process it does not own; and anything that puts a bot token, a control
secret, a session token, or a tunnel address somewhere it can be read.

Skill content reaches the model, so treat it as untrusted input to the agent:
Hermes logs a warning when skill content resembles a prompt-injection attempt
but still serves it. Review changes to `SKILL.md` with that in mind.
