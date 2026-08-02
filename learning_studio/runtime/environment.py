"""The contract between the supervisor and the runtime it starts.

One module, imported by both sides, so the names cannot drift apart. Every
value the runtime needs arrives in its environment rather than on its command
line, and the reason is uniform: a command line is world-readable through the
process table, and one of these values is a credential.

What travels here, and why each one has to
------------------------------------------

``LEARNING_STUDIO_RUNTIME_ID`` / ``LEARNING_STUDIO_GENERATION``
    Identity, so a control reply can be checked against the record rather than
    believed.

``LEARNING_STUDIO_CONTROL_TOKEN``
    The loopback capability that makes the control endpoint answer at all.
    **Secret.** Never logged, never returned, never in an argument.

``LEARNING_STUDIO_PROFILE``
    The active Hermes profile name. The runtime has no Hermes to ask — it runs
    in its own virtual environment — and every stored row is scoped by this, so
    a runtime that guessed ``default`` would find none of a named profile's
    exercises.

``LEARNING_STUDIO_HANDSHAKE``
    Where to write the port it ended up on. The supervisor cannot know an
    ephemeral port in advance and will not pre-bind one to find out, because a
    port bound, released and re-bound is a race with everything else on the
    machine.

``LEARNING_STUDIO_CLOUDFLARED``
    The absolute, already-resolved path of the tunnel binary. Resolution
    happens in the supervisor so the runtime needs no ``PATH`` at all, which is
    one fewer way for a process this plugin starts to find an executable
    nobody chose.

``LEARNING_STUDIO_IDLE_SECONDS`` / ``LEARNING_STUDIO_MAX_LIFETIME_SECONDS``
    The runtime's own deadlines. It enforces them itself, so a runtime that
    outlives the Hermes process that started it still stops.

What is passed through from the host environment is a closed list —
:data:`INHERITED` — and nothing else. Not because the child is untrusted, but
because a process that answers a public URL should carry the smallest
environment that lets it work: every variable it does not have is one it cannot
leak in a traceback, a crash dump, or a subprocess of its own.
"""

from __future__ import annotations

RUNTIME_ID = "LEARNING_STUDIO_RUNTIME_ID"
GENERATION = "LEARNING_STUDIO_GENERATION"
CONTROL_TOKEN = "LEARNING_STUDIO_CONTROL_TOKEN"
PROFILE = "LEARNING_STUDIO_PROFILE"
HANDSHAKE = "LEARNING_STUDIO_HANDSHAKE"
CLOUDFLARED = "LEARNING_STUDIO_CLOUDFLARED"
IDLE_SECONDS = "LEARNING_STUDIO_IDLE_SECONDS"
MAX_LIFETIME_SECONDS = "LEARNING_STUDIO_MAX_LIFETIME_SECONDS"

#: Set by the supervisor, read by the runtime. The one secret among them is
#: named separately below so a test can assert it never reaches a log or an
#: argument.
OWN_VARIABLES = (
    RUNTIME_ID,
    GENERATION,
    CONTROL_TOKEN,
    PROFILE,
    HANDSHAKE,
    CLOUDFLARED,
    IDLE_SECONDS,
    MAX_LIFETIME_SECONDS,
)

#: The variable whose value is a credential.
SECRET_VARIABLES = (CONTROL_TOKEN,)

#: Passed through from the host environment when — and only when — it is set.
#:
#: ``TELEGRAM_BOT_TOKEN`` is here because the runtime verifies ``initData``
#: against it, which is the whole authentication boundary; without it the
#: runtime authenticates nobody, which is safe but useless. It is a secret and
#: is treated as one: it is read from the environment on each verification and
#: never copied into configuration, a record, a log line, or a response.
#:
#: The Telegram allowlists are here because authorisation is the intersection
#: of Hermes' own gates with this plugin's, and a runtime that could not see
#: them would compute an empty intersection and authorise nobody.
#:
#: ``HERMES_HOME`` is how the runtime finds the profile it belongs to. The
#: locale and TLS variables are here because leaving them out makes a working
#: system fail for reasons nobody enjoys diagnosing.
INHERITED = (
    "HERMES_HOME",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_CHATS",
    "GATEWAY_ALLOWED_USERS",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

#: Inherited by the tunnel child, which needs far less than the runtime does.
#: Notably absent: the bot token, every allowlist, and ``HERMES_HOME``. A
#: tunnel forwards bytes to a loopback port; it has no business holding a
#: credential or knowing where a learner's database lives.
TUNNEL_INHERITED = (
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
