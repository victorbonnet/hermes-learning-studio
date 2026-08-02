"""Failures the runtime may report, and the rule about what they may say.

Every message in this module is written to be shown to a language model and,
through it, to a learner. So each one states what happened and what to do, and
none of them quotes a path, a port, an address, an environment variable, a
process id, a command line, a bot token, a tunnel URL, or the output of another
program.

That is not squeamishness. The agent-facing surface is the widest audience this
plugin has: a tool result becomes conversation history, gets summarised, gets
logged, and may be read back months later by someone who was never meant to see
the operator's filesystem layout. Anything an operator genuinely needs in order
to diagnose a failure belongs in the profile's own logs, where the reader is
already the operator.

:class:`RuntimeUnavailable` carries a machine-readable ``reason`` beside the
message. The reason is a fixed identifier chosen by a programmer — never a
string built from runtime values — so it is safe to log and safe to assert on
in tests.
"""

from __future__ import annotations


class RuntimeUnavailable(Exception):
    """The runtime cannot do what was asked. The message is already safe."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


class LaunchRefused(RuntimeUnavailable):
    """The request was understood and refused on policy grounds.

    Separate from the general failure because the agent's next move differs:
    a refusal is answered by asking the learner something, not by retrying.
    """


#: Said when the host operating system cannot supply the primitives this
#: package needs to own a process safely.
UNSUPPORTED_PLATFORM = (
    "The Learning Studio runtime is not available on this operating system: it cannot "
    "prove which processes belong to it here, and it will not signal a process it cannot "
    "prove it owns. Continue the exercise in conversation."
)

#: Said when the plugin-local runtime environment has not been prepared.
NOT_BOOTSTRAPPED = (
    "The Learning Studio runtime has not been prepared on this machine, so nothing was "
    "started. An operator needs to run the one-off runtime bootstrap. Continue the "
    "exercise in conversation."
)

#: Said when `cloudflared` is not where the operator said it would be.
NO_TUNNEL_BINARY = (
    "The Learning Studio cannot open a temporary public address because cloudflared is "
    "not installed or not where this profile is configured to find it. Nothing was "
    "started. Continue the exercise in conversation."
)

#: Said when a start did not finish in time, or fell over on the way up.
START_FAILED = (
    "The Learning Studio runtime could not be started, so no exercise was opened and no "
    "message was sent. Anything that had started was stopped. Continue the exercise in "
    "conversation."
)

#: Said when the tunnel would not come up, or published something unusable.
TUNNEL_FAILED = (
    "The Learning Studio could not open a temporary public address for this exercise, so "
    "nothing was opened and no message was sent. Continue the exercise in conversation."
)

#: Said when the button could not be delivered. The session is already gone by
#: the time anybody reads this.
DELIVERY_FAILED = (
    "The Learning Studio prepared the exercise but could not send the button to open it, "
    "so the learning session was cancelled and nothing is waiting for the learner. Do not "
    "tell them to tap anything. Continue the exercise in conversation."
)

#: Said when the button went out but the launch could not be committed.
#:
#: The only honest answer here is "I do not know". Claiming success would tell
#: a learner to tap something that may admit nobody; claiming failure would say
#: nothing was sent when a message is sitting in their chat.
DELIVERY_INDETERMINATE = (
    "The Learning Studio sent the button but could not finish opening the exercise, so it "
    "cannot tell whether the learner can use it. A message may have arrived. Do not promise "
    "them it works and do not launch it again - ask whether they see a button, and offer to "
    "carry on in conversation."
)

#: Said when a failed launch could not be fully undone.
CLEANUP_INDETERMINATE = (
    "The Learning Studio could not send the button, and could not confirm it undid what it "
    "had already set up. Nothing is waiting for the learner as far as it can tell, but it "
    "cannot promise that. Do not tell them to tap anything; continue in conversation. "
    "Anything left over expires on its own."
)

#: Said when another start or stop is already in flight for this profile.
BUSY = (
    "The Learning Studio runtime is being started or stopped by another request right "
    "now, so nothing was changed. Try again in a moment."
)

#: Said when a runtime record exists but the process behind it cannot be proved
#: to be this plugin's. Nothing is signalled in that state, ever.
UNPROVABLE = (
    "The Learning Studio could not confirm that the recorded runtime process is its own, "
    "so it left that process alone rather than risk signalling an unrelated one. The "
    "record was cleared; the old runtime stops itself when its own time limit expires."
)
