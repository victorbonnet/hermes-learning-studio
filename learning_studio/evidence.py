"""What the learner actually said, captured before the model could invent it.

The problem this solves
-----------------------

Consent used to be three tool arguments: ``initiation``, ``learner_confirmed``,
and a quotation. All three are written by the model, in the same call, and
nothing tied any of them to a message a person had sent. So "the learner asked
me to open this" was an assertion the plugin repeated back as though it were a
finding — and a model that had merely decided an exercise would be nice could
open a public address and message somebody, truthfully reporting that consent
had been given.

The fix is not to trust the model harder. It is to have a second source.

Hermes fires ``pre_gateway_dispatch`` with the real ``MessageEvent`` **before
the agent runs**, and binds ``HERMES_SESSION_MESSAGE_ID`` for the turn that
message starts. So the plugin can record what arrived, keyed by exactly which
message it was, and later require the model's quotation to appear in *that*
message. The model still decides what the words mean — that is a judgement, and
judgement is what it is for — but it cannot supply the words.

What this is not
----------------

It is not proof of intent. A learner who types "go on then" has said something;
whether they meant "open an exercise" is an interpretation, and this module
does not pretend otherwise. What it removes is the ability to launch on words
nobody said, in a turn nobody started, or on the same sentence twice.

Privacy
-------

This holds a learner's own message text, briefly, in memory, in the process
that already had it. So:

- **nothing is written to disk**, ever — not the database, not a log, not a
  runtime record;
- **nothing is returned** to the model or the learner; the only thing that
  leaves is a boolean and a fixed reason string;
- entries expire on a short operator-independent TTL and the store is bounded,
  so a long-lived gateway does not accumulate a transcript;
- only the current message is kept. There is no history here, and no way to ask
  this module what somebody said earlier.

Spent evidence leaves a **tombstone**, and the tombstone outlives the evidence.
That ordering is the whole replay defence: if the record simply vanished when
it expired, the same quotation would look unused again the moment it went
stale, and a retry loop would get a fresh launch out of a sentence from ten
minutes ago.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: How long a learner's message can authorise a launch. Short: this is the gap
#: between somebody typing and the agent acting, not the length of a session.
EVIDENCE_TTL_SECONDS = 600

#: How long the *memory that it was used* lasts. Strictly longer than the
#: evidence itself, so a spent message can never become unspent by expiring.
TOMBSTONE_TTL_SECONDS = EVIDENCE_TTL_SECONDS * 3

#: Bounded, so a busy gateway cannot turn this into a transcript.
MAX_ENTRIES = 64
MAX_TOMBSTONES = 512

#: Conversations for which the highest consumed message id is remembered.
#:
#: A tombstone names one message and the table holding them is bounded, so an
#: old enough tombstone is evicted — and a redelivery of that same Telegram
#: update then looked like a message nobody had used. A watermark is one
#: integer per conversation rather than one entry per message, so it survives
#: the traffic that evicts tombstones and answers the same question for every
#: message below it at once.
MAX_WATERMARKS = 4096

#: Longest message text retained. A learner's request is a sentence; anything
#: past this is truncated before it is stored, because the only thing it is
#: used for is checking whether a short quotation appears in it.
MAX_TEXT_CHARS = 4000

#: Shortest quotation that may be matched. Not a security bound — the message
#: identity is what carries the weight — but a two-character quotation matches
#: so much text that it would tell a reader nothing about whether the model had
#: actually read the message.
MIN_MATCH_CHARS = 4

_WHITESPACE = re.compile(r"\s+")


def _ordinal(message_id: str) -> int | None:
    """A message id as a comparable position, or ``None`` if it is not one.

    Telegram numbers messages upwards within a conversation, which is what
    lets one integer stand for every message before it. Anything that is not a
    plain non-negative integer has no such order and is not treated as though
    it did.
    """
    text = str(message_id or "").strip()
    if not text.isdigit():
        return None
    try:
        return int(text)
    except ValueError:  # pragma: no cover - `isdigit` already excludes this
        return None


def normalise(text: object) -> str:
    """Casefold and collapse whitespace, so a quotation may be reformatted.

    Deliberately *not* punctuation-stripping. "Quiz me on photosynthesis"
    should match "Can you quiz me on photosynthesis?", which this does; making
    it match "quiz-me-on-photosynthesis" as well would start turning a
    quotation into a fuzzy search.
    """
    return _WHITESPACE.sub(" ", str(text or "")).strip().casefold()


@dataclass(frozen=True)
class EvidenceKey:
    """Exactly which message, from exactly whom, in exactly which conversation.

    Every field is part of the identity. Two of them are worth calling out:

    ``profile`` — captured when the message arrived and compared when the tool
    runs. Under multiplexing those can differ, and when they do the lookup
    fails and the launch is refused. That is the safe direction: a mismatch
    means this plugin cannot tell whose message it is holding.

    ``message_id`` — the reason this is evidence rather than a guess. Without
    it, "the learner said this at some point" would be enough, and the whole
    point is that they said it *now*.
    """

    profile: str
    platform: str
    chat_id: str
    thread_id: str
    user_id: str
    message_id: str

    @property
    def complete(self) -> bool:
        """Every part present. A partial key identifies nothing and is refused."""
        return all((self.profile, self.platform, self.chat_id, self.user_id, self.message_id))


@dataclass
class _Entry:
    text: str
    captured_at: float


class EvidenceStore:
    """Bounded, expiring, thread-safe. Holds the current message and nothing else.

    Thread-safe because the gateway dispatches concurrently: the hook runs on
    whichever task received the message, and the tool runs on another. Without
    the lock, two launches racing on one message could both see it unspent.
    """

    def __init__(
        self,
        *,
        clock=time.time,
        ttl_seconds: int = EVIDENCE_TTL_SECONDS,
        tombstone_ttl_seconds: int = TOMBSTONE_TTL_SECONDS,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._clock = clock
        self._ttl = int(ttl_seconds)
        self._tombstone_ttl = int(tombstone_ttl_seconds)
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._entries: dict[EvidenceKey, _Entry] = {}
        self._spent: dict[EvidenceKey, float] = {}
        #: Claimed but not yet resolved. Held for the length of one launch
        #: transaction, and never handed to a second caller.
        self._reserved: dict[EvidenceKey, float] = {}
        #: ``conversation -> (highest consumed message ordinal, when)``.
        self._watermarks: dict[tuple, tuple[int, float]] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- capture ---------------------------------------------------------

    def record(self, key: EvidenceKey, text: str) -> None:
        """Remember one incoming message. Ignores anything it cannot key."""
        if not key.complete:
            return
        cleaned = normalise(text)[:MAX_TEXT_CHARS]
        if not cleaned:
            return
        now = float(self._clock())
        with self._lock:
            self._purge(now)
            if key in self._spent or key in self._reserved:
                # A message id this store has already spent — or is in the
                # middle of spending — must not be re-armed by a second
                # delivery of the same update.
                return
            if self._below_watermark(key):
                # Older than something already consumed in this conversation.
                # The tombstone for it may be long gone — that table is bounded
                # and evicts by age — and without this a redelivered update
                # from before the eviction became trusted evidence again.
                return
            self._entries[key] = _Entry(text=cleaned, captured_at=now)
            while len(self._entries) > self._max:
                oldest = min(self._entries, key=lambda k: self._entries[k].captured_at)
                del self._entries[oldest]

    # -- use -------------------------------------------------------------

    def state(self, key: EvidenceKey, quote: str) -> str:
        """Classify this quotation against the message it claims to come from.

        Returns one of ``matched``, ``spent``, ``absent``, ``mismatched``, or
        ``unusable``. A string rather than an exception because the caller maps
        each case to a different thing to say to the learner, and because the
        distinction between "you already used this" and "nobody said that" is
        one the agent needs and the learner never sees.
        """
        if not key.complete:
            return "unusable"
        needle = normalise(quote)
        if len(needle) < MIN_MATCH_CHARS:
            return "unusable"

        now = float(self._clock())
        with self._lock:
            self._purge(now)
            if key in self._spent or key in self._reserved or self._below_watermark(key):
                # A reservation reads as spent to everybody except the holder.
                # It may become free again if that launch gives up, but until
                # then nobody else may act on it. Below the watermark reads the
                # same way, and says the accurate thing — "already used" rather
                # than "nobody said that" — for a message whose tombstone has
                # since been evicted.
                return "spent"
            entry = self._entries.get(key)
            if entry is None:
                return "absent"
            return "matched" if needle in entry.text else "mismatched"

    def reserve(self, key: EvidenceKey) -> bool:
        """Claim this message's authority *before* acting on it. Once.

        This is the arbitration, and it has to happen here rather than at the
        end. Spending only after delivery meant two concurrent launches could
        both read the message as unspent, both create a grant, and both send a
        button — and the loser of the eventual ``spend`` race was simply
        ignored. The learner got two messages for one sentence.

        A reservation is held from the moment a launch is authorised until it
        either commits or explicitly gives up. Nothing else can reserve the
        same message in between, so "one message, one launch" is true under a
        double tap rather than merely intended.

        A reservation that is never resolved — an interruption after the button
        went out, a process that died mid-transaction — stays held and expires
        with the tombstone. That is deliberate: the safe residue of "we may
        have launched" is "this message cannot launch again".
        """
        if not key.complete:
            return False
        now = float(self._clock())
        with self._lock:
            self._purge(now)
            if key in self._spent or key in self._reserved or self._below_watermark(key):
                return False
            if key not in self._entries:
                return False
            self._reserved[key] = now
            return True

    def commit(self, key: EvidenceKey) -> bool:
        """Turn a reservation into a spend. The launch happened."""
        now = float(self._clock())
        with self._lock:
            if key not in self._reserved:
                return False
            del self._reserved[key]
            self._entries.pop(key, None)
            self._spent[key] = now
            while len(self._spent) > MAX_TOMBSTONES:
                oldest = min(self._spent, key=lambda k: self._spent[k])
                del self._spent[oldest]
            self._raise_watermark(key, now)
            return True

    def release(self, key: EvidenceKey) -> bool:
        """Give a reservation back, because nothing reached the learner.

        Only ever called on a path that has *proved* nothing was delivered. A
        failure that might have sent a message keeps the reservation, because
        releasing one there is what would let a retry send a second.
        """
        with self._lock:
            return self._reserved.pop(key, None) is not None

    def spend(self, key: EvidenceKey) -> bool:
        """Reserve and commit in one step, for a caller with nothing to undo."""
        return self.reserve(key) and self.commit(key)

    # -- housekeeping ----------------------------------------------------

    def _purge(self, now: float) -> None:
        """Expire evidence, and expire tombstones strictly later.

        The caller holds the lock. The asymmetry is deliberate and is the
        replay defence: evidence goes at ``ttl``, the memory that it was used
        goes at ``tombstone_ttl``, and until then a stale quotation stays
        refused instead of quietly becoming new again.
        """
        for key in [k for k, e in self._entries.items() if now - e.captured_at > self._ttl]:
            del self._entries[key]
        for key in [k for k, at in self._spent.items() if now - at > self._tombstone_ttl]:
            del self._spent[key]
        # A reservation nobody resolved expires on the tombstone clock too, so
        # a crashed transaction does not hold a message hostage forever — but
        # it outlives the evidence, so it cannot become "new" in between.
        for key in [k for k, at in self._reserved.items() if now - at > self._tombstone_ttl]:
            del self._reserved[key]

    # -- the watermark ---------------------------------------------------

    def _conversation(self, key: EvidenceKey) -> tuple:
        return (key.profile, key.platform, key.chat_id, key.thread_id, key.user_id)

    def _raise_watermark(self, key: EvidenceKey, now: float) -> None:
        """Remember that everything up to this message has been consumed.

        Telegram numbers the messages in a conversation upwards, so one integer
        stands in for every tombstone below it — which is what makes this
        survive the eviction that bounded the tombstones. The caller holds the
        lock.
        """
        ordinal = _ordinal(key.message_id)
        if ordinal is None:
            # A platform whose message ids are not ordered. Nothing can be
            # inferred about "older", so this conversation relies on the
            # tombstone table alone, and does not take a slot here.
            return
        conversation = self._conversation(key)
        highest, _ = self._watermarks.get(conversation, (0, 0.0))
        self._watermarks[conversation] = (max(highest, ordinal), now)
        while len(self._watermarks) > MAX_WATERMARKS:
            oldest = min(self._watermarks, key=lambda c: self._watermarks[c][1])
            del self._watermarks[oldest]

    def _below_watermark(self, key: EvidenceKey) -> bool:
        """Whether this message is at or below one already consumed here."""
        ordinal = _ordinal(key.message_id)
        if ordinal is None:
            return False
        highest, _ = self._watermarks.get(self._conversation(key), (0, 0.0))
        return bool(highest) and ordinal <= highest

    def clear(self) -> None:
        """Forget everything. For tests, and for a deliberate shutdown."""
        with self._lock:
            self._entries.clear()
            self._spent.clear()
            self._watermarks.clear()
            self._reserved.clear()


#: One store per Hermes process. Keyed by profile inside, so two profiles
#: sharing a process cannot read each other's messages.
STORE = EvidenceStore()


# ── The host hook ─────────────────────────────────────────────────────────


def current_key(profile: str) -> EvidenceKey:
    """Build the key for the turn that is running, from trusted session state.

    Every field comes from Hermes' session context, which the platform adapter
    bound from the real payload before the agent started. None of it is
    reachable from a tool argument.
    """
    from .identity import session_value

    return EvidenceKey(
        profile=str(profile or ""),
        platform=session_value("HERMES_SESSION_PLATFORM"),
        chat_id=session_value("HERMES_SESSION_CHAT_ID"),
        thread_id=session_value("HERMES_SESSION_THREAD_ID"),
        user_id=session_value("HERMES_SESSION_USER_ID"),
        message_id=session_value("HERMES_SESSION_MESSAGE_ID"),
    )


def _could_ever_authorise(source, platform: str) -> bool:
    """Whether a message from here could authorise a launch at all.

    The store is small and bounded on purpose — it holds one turn's worth of
    what people said, not a history — and until now it recorded *everything*
    the gateway saw. A busy group on another platform, or somebody who is not
    on the allowlist, filled it with messages that no launch could ever have
    used, and each one evicted a message that could: a learner's "yes" in a DM
    could be pushed out by strangers talking in a room the learner is not in.

    So the filter is exactly the set of launches that can happen. A launch goes
    to a Telegram direct message, from an allowlisted account, and nowhere
    else; anything else is not evidence for anything and is not kept.

    Fail closed twice over. An unreadable chat type is refused rather than
    assumed private, and an allowlist that cannot be computed authorises
    nobody — the same rule the launch path itself applies.
    """
    from .destination import PRIVATE_CHAT_TYPES

    if platform.lower() != "telegram":
        return False

    chat_type = str(getattr(source, "chat_type", "") or "").strip().lower()
    if chat_type not in PRIVATE_CHAT_TYPES:
        # Absent counts as unrecognised. A group message that simply did not
        # carry its type must not be stored as though it were a DM.
        return False

    user_id = str(getattr(source, "user_id", "") or "").strip()
    try:
        from .authorization import effective_allowed_users, is_authorized
        from .config import load_config, load_raw_config

        allowed = effective_allowed_users(
            plugin_restriction=load_config().mini_app_allowed_telegram_users,
            host_config=load_raw_config(),
        )
    except Exception as exc:
        logger.debug("consent evidence not captured: %s", type(exc).__name__)
        return False
    return is_authorized(user_id, allowed)


def capture_message_evidence(event=None, gateway=None, session_store=None, **_kwargs) -> None:
    """``pre_gateway_dispatch``: record the incoming message, and nothing else.

    Returns ``None``, which Hermes reads as "carry on normally". This hook
    never skips a message, never rewrites one, and never inspects one for any
    purpose other than the launch consent check.

    It cannot raise. Hermes already guards the call, but a plugin that could
    break message dispatch by mishandling an unexpected event shape would be a
    plugin nobody should enable — so every failure here is swallowed after
    being logged by class name.
    """
    del gateway, session_store
    try:
        source = getattr(event, "source", None)
        if source is None or getattr(source, "is_bot", False):
            # A bot or webhook is not a learner, and its text is not consent.
            return None

        platform = getattr(getattr(source, "platform", None), "value", None) or str(
            getattr(source, "platform", "") or ""
        )
        if not _could_ever_authorise(source, str(platform).strip()):
            return None

        from .paths import profile_id

        key = EvidenceKey(
            profile=profile_id(),
            platform=str(platform).strip(),
            chat_id=str(getattr(source, "chat_id", "") or "").strip(),
            thread_id=str(getattr(source, "thread_id", "") or "").strip(),
            user_id=str(getattr(source, "user_id", "") or "").strip(),
            message_id=str(getattr(event, "message_id", "") or "").strip(),
        )
        STORE.record(key, getattr(event, "text", ""))
    except Exception as exc:  # pragma: no cover - defensive; host-side shapes vary
        logger.debug("consent evidence not captured: %s", type(exc).__name__)
    return None
