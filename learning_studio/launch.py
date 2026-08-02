"""The launch transaction: nine steps, and one place where it commits.

Opening an exercise is the only thing this plugin does that reaches outside the
machine. It creates a temporary public address and sends a person a message, so
it is written as a transaction with a single commit point and a rollback that
touches nothing it did not create.

The order is the design
-----------------------

1. **Who is asking**, from Hermes' session context. Never from an argument.
2. **Which exercise**, by opaque id, checked in SQL against that learner.
3. **May this happen without asking?** — :mod:`learning_studio.consent`.
4. **Where would the message go?** — :mod:`learning_studio.destination`,
   derived from the authenticated private chat and the profile's own
   allowlist. Resolved *before* anything is started, because a launch that
   could never be delivered should not start a server first.
5. **A runtime**, started or reused, under the profile lock.
6. **A public address**, which the runtime's tunnel has already validated and
   which is validated again on the way in.
7. **A grant**: expiring, and bound to profile, generation, Telegram account,
   learner and experience.
8. **The button.**
9. **Commit** — and only here. Everything before this point is undone if
   anything after it fails.

Rollback
--------

Two rules, and the second is the one that matters:

- The grant this call created is revoked, which also ends any session opened
  under it. A learner is never left holding an entrance to an exercise the
  agent has been told was not delivered.
- The runtime is stopped **only if this call started it**. A reused runtime may
  be serving somebody else's session, and tearing it down because a second
  launch failed to send a message would make one learner's failure another
  learner's interruption.

What never travels
------------------

The tunnel address is used to build one button and is then dropped. It is not
in the return value, not in the status payload, not in a log line, and not in a
memory candidate. Neither is the bot token, the chat id, the Telegram user id,
the control secret, or a session token. What the agent gets back is: it worked,
here is an opaque launch id, and here is what is *not* being recorded.
"""

from __future__ import annotations

import logging
from typing import Any

from . import consent as consent_policy
from . import service
from .config import LearningStudioConfig, load_config
from .identity import Principal
from .runtime import manager, ownership, supervisor
from .runtime.errors import TUNNEL_FAILED, RuntimeUnavailable
from .runtime.state import ProfileLock

logger = logging.getLogger(__name__)

#: Said when the runtime came up but published no usable public address.
NO_PUBLIC_ADDRESS = TUNNEL_FAILED


def launch_experience(
    *,
    principal: Principal,
    experience_id: str,
    initiation: str,
    learner_confirmed: bool = False,
    confirmation_quote: str | None = None,
    config: LearningStudioConfig | None = None,
    deliver=None,
    ledger: consent_policy.ConsentLedger | None = None,
) -> dict[str, Any]:
    """Open a prepared exercise for the learner who asked for it.

    ``deliver`` is the seam where a Telegram message is actually sent. It is a
    parameter so that every test in this repository exercises the whole
    transaction — runtime, grant, rollback, reuse — without a network, and so
    that the one function that holds a bot token is small enough to read.
    """
    settings = config or load_config()
    ledger = ledger or consent_policy.LEDGER
    send = deliver or _default_deliver

    # (1) and (2): identity is already resolved; ownership is checked in SQL,
    # and a missing experience and somebody else's are the same refusal.
    bundle = service.delivery_bundle(
        principal=principal, experience_id=str(experience_id), config=settings
    )
    experience = bundle.experience

    # (3) Consent, before anything exists to roll back.
    decision = ledger.decide(
        profile=principal.profile,
        learner_scope=principal.scope,
        experience_id=str(experience["experience_id"]),
        initiation=initiation,
        learner_confirmed=learner_confirmed,
        confirmation_quote=confirmation_quote,
    )

    # (4) The destination, before a process is started. A launch that has
    # nowhere to send a button is a refusal, not a runtime plus a refusal.
    from .destination import resolve_destination

    destination = resolve_destination(principal=principal, config=settings)

    manager.require_prerequisites(settings)

    with ProfileLock():
        handle = supervisor.ensure_running(settings)
        started_here = handle.started
        try:
            return _launch_within(
                settings=settings,
                handle=handle,
                destination=destination,
                bundle=bundle,
                experience=experience,
                principal=principal,
                decision=decision,
                confirmation_quote=confirmation_quote,
                ledger=ledger,
                send=send,
            )
        except BaseException:
            if started_here:
                # Only what this call created. A reused runtime is somebody
                # else's session as far as this function knows.
                supervisor.stop(settings)
            raise


def _launch_within(
    *,
    settings: LearningStudioConfig,
    handle,
    destination,
    bundle,
    experience: dict[str, Any],
    principal: Principal,
    decision: consent_policy.Decision,
    confirmation_quote: str | None,
    ledger: consent_policy.ConsentLedger,
    send,
) -> dict[str, Any]:
    """Steps 6 to 9, with the grant as the thing that gets rolled back."""
    public_url = handle.public_url
    if not public_url:
        raise RuntimeUnavailable(NO_PUBLIC_ADDRESS, reason="tunnel_not_ready")

    granted = ownership.call(
        handle.record,
        ownership.GRANT_PATH,
        {
            "telegram_user_id": destination.telegram_user_id,
            "learner_id": bundle.learner_id,
            "experience_id": str(experience["experience_id"]),
            # When consent has already been spent, this call may report the
            # launch that exists and may not become a second one. Enforced in
            # the runtime rather than here, so nothing is created and then undone.
            "reuse_only": not decision.may_create,
        },
    )

    if not granted.get("created") and not granted.get("reused"):
        raise consent_policy.ConsentRequired(
            consent_policy.ALREADY_USED, reason="confirmation_already_used"
        )

    if granted.get("reused"):
        # A repeat. The learner already has a button for this exercise and a
        # second message would be noise — or worse, a second thing to tap after
        # the first one stopped working.
        return _result(
            experience=experience,
            granted=granted,
            decision=decision,
            delivered=False,
            reused=True,
        )

    launch_id = str(granted["launch_id"])
    try:
        send(
            destination=destination,
            url=public_url,
            label=settings.launch_button_label,
            title=str(experience["title"]),
        )
    except BaseException:
        # The learner has no button, so they must not have a grant either.
        with _suppressed("revoking a grant after a failed delivery"):
            ownership.call(handle.record, ownership.GRANT_REVOKE_PATH, {"launch_id": launch_id})
        raise

    if decision.initiation == consent_policy.AGENT_SUGGESTION:
        # Spent only now, so a failure anywhere above leaves the learner's
        # agreement intact and the next attempt does not need a second ask.
        ledger.spend(
            profile=principal.profile,
            learner_scope=principal.scope,
            experience_id=str(experience["experience_id"]),
            quote=confirmation_quote or "",
        )

    return _result(
        experience=experience, granted=granted, decision=decision, delivered=True, reused=False
    )


def _result(
    *,
    experience: dict[str, Any],
    granted: dict[str, Any],
    decision: consent_policy.Decision,
    delivered: bool,
    reused: bool,
) -> dict[str, Any]:
    """The agent-facing answer. Built field by field; nothing passed through."""
    return {
        "ok": True,
        "launched": True,
        "launch_id": granted.get("launch_id"),
        "experience_id": experience["experience_id"],
        "title": experience["title"],
        "component_count": len(experience["components"]),
        "button_delivered": delivered,
        "reused_existing_launch": reused,
        "basis": decision.basis,
        "expires_in_seconds": granted.get("expires_in_seconds"),
        "delivery": (
            "A button to open the exercise has been sent to the learner in this chat. Tell "
            "them it is there; do not repeat the link, because there is no link to repeat."
            if delivered
            else "The learner already has a button for this exercise; nothing new was sent."
        ),
        **_honest_scoring(),
    }


def _honest_scoring() -> dict[str, Any]:
    return {
        "scored": False,
        "attempts_stored": False,
        "notice": (
            "Nothing about how the learner does will be stored: no attempt, score, mastery, "
            "or progress record exists in this release. Ask them how it went, and use "
            "learning_studio_results only for whether they opened and finished it."
        ),
    }


class _suppressed:
    """``contextlib.suppress`` that says what it swallowed, at DEBUG.

    Used on the rollback path, where a second failure must not replace the
    first: the caller is already raising the error the agent needs to see.
    """

    def __init__(self, what: str) -> None:
        self.what = what

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback) -> bool:
        if value is not None:
            logger.debug("%s failed: %s", self.what, type(value).__name__)
        return True


def _default_deliver(**kwargs: Any) -> None:
    """Send the button, through the one module that holds a bot token.

    Imported here rather than at this module's scope so that the single file
    able to reach a remote host is loaded only on the path that actually sends
    something — reading a launch's ownership and consent logic does not need to
    drag in a network client.
    """
    from .telegram_launch import deliver_web_app_button

    deliver_web_app_button(**kwargs)


def launch_results(
    *,
    principal: Principal,
    experience_id: str,
    config: LearningStudioConfig | None = None,
) -> dict[str, Any]:
    """What happened to a launch, with nothing invented.

    The honest answer in this release is short, and the shape of the response
    says so in three places rather than one, because "how did they do?" is the
    question a reader most easily assumes has been answered.

    What is actually known: whether an exercise was opened, which component the
    learner reached, how many they answered, and whether they finished. Those
    are facts the runtime holds while a session is alive. What is *not* known,
    and is stated as not known: any mark, score, mastery estimate, durable
    attempt, or review schedule. None of those exists in this release, and a
    field reporting one would be a fabrication with a plausible name.

    A learner's answers are never returned either. How far somebody got is
    progress; what they wrote is their work, and an agent that wants to discuss
    it should ask them.
    """
    settings = config or load_config()

    # Ownership first, in SQL, against this principal. A learner asking about
    # somebody else's exercise gets the same answer as one asking about an
    # exercise that does not exist.
    bundle = service.delivery_bundle(
        principal=principal, experience_id=str(experience_id), config=settings
    )
    experience = bundle.experience

    handle = supervisor.current(settings)
    if handle is None:
        return {
            "ok": True,
            "experience_id": experience["experience_id"],
            "title": experience["title"],
            "state": "not_running",
            "opened": False,
            "message": (
                "No Learning Studio runtime is open for this profile, so there is nothing "
                "in progress. Nothing was recorded while it was."
            ),
            **_honest_scoring(),
        }

    try:
        progress = ownership.call(
            handle.record,
            ownership.LAUNCH_PATH,
            {
                "telegram_user_id": principal.user_id,
                "experience_id": str(experience["experience_id"]),
            },
        )
    except ownership.ControlError:
        progress = {"found": False}

    if not progress.get("found"):
        return {
            "ok": True,
            "experience_id": experience["experience_id"],
            "title": experience["title"],
            "state": "not_launched",
            "opened": False,
            "message": "This exercise has not been opened for this learner in the current runtime.",
            **_honest_scoring(),
        }

    return {
        "ok": True,
        "experience_id": experience["experience_id"],
        "title": experience["title"],
        "state": progress.get("state"),
        "opened": bool(progress.get("opened")),
        "position": progress.get("position"),
        "component_count": progress.get("component_count"),
        "answered": progress.get("answered"),
        "completed": bool(progress.get("completed")),
        "responses_returned": False,
        # Bounded, selective, and empty on purpose. See the docstring: finishing
        # one exercise is an event, not a durable fact about a person, and this
        # release observes nothing else. Anything worth remembering comes from
        # the conversation and goes through `learning_studio_save_context`.
        "memory_candidates": [],
        "memory_candidates_note": (
            "No durable memory candidate is proposed from running an exercise. Completing "
            "one is an event rather than a fact that stays true, and no mark or attempt is "
            "recorded to draw a conclusion from. Propose durable facts through "
            "learning_studio_save_context, from what the learner actually told you."
        ),
        **_honest_scoring(),
    }
