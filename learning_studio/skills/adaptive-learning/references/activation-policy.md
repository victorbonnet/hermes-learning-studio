# Activation policy

When you may start an exercise, and when you must ask first.

The principle: **the learner's attention is theirs to give.** Launching
something they asked for is service; launching something they did not ask for
is an interruption, however good the exercise is.

## The two rules

**1. An explicit learner request may launch immediately.** If they asked for
practice, and the tool exists, launch the exercise without asking for further
confirmation. Re-confirming a direct request is friction, not consent, and it
trains the learner to expect a dialogue box between them and their work.

Explicit requests look like: "quiz me on this", "give me ten practice
problems", "test my kanji", "let's drill these", "start the exercise".

**2. Practice you propose yourself needs a yes.** When *you* suggest an
exercise the learner did not ask for, describe it in one line — format, size,
subject — and wait for them to confirm before starting anything.

Agent-initiated cases include: noticing a weak area mid-conversation, deciding
a spaced review is due, converting an explanation into a drill, and following
up "you got two of those wrong" with a set. All of them need a yes.

## Ambiguous cases

| Situation | Treat as |
| --- | --- |
| "Explain recursion" | Explanation. Offer practice, do not launch it. |
| "Explain recursion and check I've got it" | Explicit request — launch. |
| "I keep mixing up the Meiji and Taishō eras" | A complaint, not a request. Offer. |
| "What should I revise for tomorrow?" | Planning. Answer, then offer. |
| Learner accepted a plan that contains exercises | The plan is the consent. Launch the exercises in it. |
| Mid-session, next item in an accepted block | Already consented. Continue. |
| A new format not in the accepted plan | Ask. Consent covered the plan, not the change. |
| Learner asked to stop, then asks a question | Consent expired. Answer only. |
| Scheduled review comes due | Agent-initiated. Ask, always. |

When genuinely unsure, ask — the cost of one extra question is far below the
cost of hijacking someone's attention.

## Consent has a scope and an expiry

- **Scope.** Consent to a titration drill is not consent to a general chemistry
  test, and consent to ten items is not consent to fifty.
- **Expiry.** It ends when the session ends. Yesterday's yes does not authorise
  today's launch. A confirmed long-running track authorises *proposing* the
  next block, never starting it unannounced.
- **Revocation.** "Stop", "later", "not now", or a change of subject ends it
  immediately. Do not re-offer in the same breath.

## Telling the launch tool which of the two happened

`learning_studio_launch` asks you to say which rule applies, because it cannot
work it out and neither can anything else after the fact.

- **Rule 1** — `initiation: "learner_request"`. Nothing else is needed. The
  request *was* the agreement.
- **Rule 2** — `initiation: "agent_suggestion"`, plus `learner_confirmed: true`
  and `confirmation_quote` set to what the learner actually said. Their words,
  not your summary of them.

Two things follow from that, and both are enforced rather than advised:

- **One agreement, one launch.** If the first exercise expired unopened, their
  earlier yes does not open a second one. Ask again. This is what stops a retry
  loop from repeatedly opening a public address on somebody's behalf.
- **An agreement goes stale.** A yes from much earlier in the conversation is
  refused. Ask again rather than arguing with the refusal.

Nothing here can verify that the learner agreed — you are the one writing
`learner_confirmed`, in the same call as everything else. What the rule buys is
that you have to say, once, that a specific person agreed to a specific thing,
and cannot spend that statement twice.

## Launching honestly

Whatever the policy says, the mechanics are the same:

- Check that the tool is actually in your tool list before routing to it.
- If it is not, run the exercise in chat and say that is what you are doing.
- **Never claim that an exercise was launched, opened, or is running unless the
  result says `button_delivered`.** Preparing is not opening; a refusal is not a
  launch; and "it should be there" is not a tool result.
- If a launch fails, say it failed and continue in conversation. Do not fall
  back silently and let the learner believe they are in the app, and do not tell
  them to tap a button that was never sent.
- A repeat launch of the same exercise reports the one already open and sends
  nothing new. If the learner says the button is not there, that is a
  conversation to have — not a reason to keep calling launch.
- **Do not narrate the runtime.** The learner does not need to know about
  servers, addresses, tunnels, or timeouts. "It's open — tap the button" and
  "let's do this here instead" are the two things worth saying.

## Never launch when

- The learner is mid-task on something else.
- They just said they were tired, out of time, or done.
- The content has not been verified — a wrong answer key is worse than no
  exercise.
- The exercise would exclude them: audio-only for someone who told you they
  cannot use audio, drag-and-drop where precise pointing is a barrier.
- You are the only one who thinks it is a good idea. Say why you think it would
  help, and let them decide.
