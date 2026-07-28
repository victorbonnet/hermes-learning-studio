"""JSON schemas for the two registered tools.

The schemas are the plugin's actual security boundary against a confused or
adversarial caller, so they are restrictive by construction:

- ``additionalProperties: false`` everywhere, so a typo is an error rather
  than a silently ignored field.
- Every string is bounded, every array is bounded.
- No field accepts a filesystem path, executable code, or SQL. There is
  nothing here a caller could use to reach outside the Learning Studio's own
  storage, because no such parameter exists to begin with.
- Nothing names a subject, a language, or a discipline.
"""

from __future__ import annotations

from typing import Any

from .candidates import Action, Category, Confidence, ConfirmationState, Durability, Origin
from .models import (
    LIST_FIELDS,
    MAX_LIST_ITEMS,
    MAX_NAME_CHARS,
    MAX_VALUE_CHARS,
    OBJECTIVE_TEXT_MAX,
    SCALAR_FIELDS,
    ObjectiveStatus,
    TrackStatus,
)

GET_TOOL_NAME = "learning_studio_get_context"
SAVE_TOOL_NAME = "learning_studio_save_context"
PREPARE_TOOL_NAME = "learning_studio_prepare"

_MAX_ITEMS = 25

_TRACK_ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    "description": "Opaque track identifier returned by a previous call.",
}


def _context_properties() -> dict[str, Any]:
    """One property per context field: scalars as strings, the rest as arrays."""
    properties: dict[str, Any] = {
        field: {"type": "string", "minLength": 1, "maxLength": MAX_VALUE_CHARS}
        for field in SCALAR_FIELDS
    }
    for field in LIST_FIELDS:
        properties[field] = {
            "type": "array",
            "maxItems": MAX_LIST_ITEMS,
            "items": {"type": "string", "minLength": 1, "maxLength": MAX_VALUE_CHARS},
        }
    return properties


def context_object(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": False,
        "properties": _context_properties(),
    }


GET_CONTEXT_SCHEMA: dict[str, Any] = {
    "name": GET_TOOL_NAME,
    "description": (
        "Retrieve what is known about the person you are talking to: their temporary "
        "(unconfirmed) context, the durable context of a confirmed learning track, and a "
        "resolved view applying the precedence rules with provenance for every value. "
        "Call this before planning a session so you do not re-ask what they already told "
        "you. The learner is identified from the Hermes session, not from any argument - "
        "there is deliberately no way to ask about a different person."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "track_id": _TRACK_ID,
            "track_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_NAME_CHARS,
                "description": "Select a track by name instead of by ID.",
            },
            "current_request": context_object(
                "What the learner is asking for right now. These values take precedence "
                "over everything stored: saved context must never override someone who "
                "has just said something different."
            ),
            "include_memory_candidates": {
                "type": "boolean",
                "description": "Also return previously proposed memory candidates.",
            },
        },
    },
}


_CORRECTION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["field", "value"],
    "properties": {
        "field": {
            "type": "string",
            "enum": list(SCALAR_FIELDS + LIST_FIELDS),
            "description": "The context field being corrected.",
        },
        "value": {
            "oneOf": [
                {"type": "string", "minLength": 1, "maxLength": MAX_VALUE_CHARS},
                {
                    "type": "array",
                    "maxItems": MAX_LIST_ITEMS,
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_VALUE_CHARS},
                },
            ],
            "description": "The corrected value, which supersedes the one it replaces.",
        },
        "track_id": _TRACK_ID,
        "durable": {
            "type": "boolean",
            "description": (
                "Store the correction against a confirmed track rather than the temporary "
                "context. Requires a track_id."
            ),
        },
    },
}

_TRACK = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Create or update an ongoing learning track. Creating one requires confirmed=true, "
        "which is your assertion that the learner said yes in so many words - repetition, "
        "your own confidence, and prior sessions are not that. The flag is a deliberate "
        "speed bump, not proof: this plugin cannot verify it, so a track authorises nothing "
        "sensitive and never stands in for consent."
    ),
    "properties": {
        "track_id": _TRACK_ID,
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_NAME_CHARS,
            "description": "Short name for the track. Must be unique for this learner.",
        },
        "confirmed": {
            "type": "boolean",
            "description": (
                "Set only when the learner has explicitly agreed to ongoing work. Without "
                "it no durable track is created and the context stays temporary. You are "
                "asserting this; nothing here can check it."
            ),
        },
        "status": {
            "type": "string",
            "enum": [status.value for status in TrackStatus],
            "description": (
                "Set to 'archived' or 'withdrawn' to let a learner retire or take back a "
                "track. Existing tracks only."
            ),
        },
        "context": context_object(
            "Durable context for this track. Accessibility needs are session-only and are "
            "always returned without being stored; accessibility_consent cannot authorise "
            "durable storage because it is model-controlled too."
        ),
    },
}

_OBJECTIVE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "objective_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "track_id": _TRACK_ID,
        "behavior": {
            "type": "string",
            "minLength": 1,
            "maxLength": OBJECTIVE_TEXT_MAX,
            "description": "The observable behaviour, e.g. 'state the base case'.",
        },
        "condition": {
            "type": "string",
            "minLength": 1,
            "maxLength": OBJECTIVE_TEXT_MAX,
            "description": "The condition, e.g. 'given a recursive function, unaided'.",
        },
        "standard": {
            "type": "string",
            "minLength": 1,
            "maxLength": OBJECTIVE_TEXT_MAX,
            "description": "The measurable standard, e.g. '4 times in 5'.",
        },
        "status": {
            "type": "string",
            "enum": [status.value for status in ObjectiveStatus],
        },
        "confirm_met": {
            "type": "boolean",
            "description": (
                "Required to set status='met'. An objective is met by performing to its "
                "standard consistently, never on the strength of one answer."
            ),
        },
    },
}

_MEMORY_CANDIDATE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "statement", "evidence_summary", "origin"],
    "properties": {
        "category": {"type": "string", "enum": [c.value for c in Category]},
        "statement": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_VALUE_CHARS,
            "description": (
                "The durable fact being proposed, in one sentence. No raw answers, "
                "attempts, scores, transcripts, identifiers, or credentials."
            ),
        },
        "evidence_summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_VALUE_CHARS,
            "description": "Why you believe it — summarised, never a transcript.",
        },
        "origin": {
            "type": "string",
            "enum": [o.value for o in Origin if o is not Origin.MODEL_PROPOSED],
            "description": (
                "What produced this proposal. Only these origins may. One error, one slow "
                "reply, a single inference, momentary frustration, a raw score, or session "
                "state must never become a candidate. Note that an origin asserting the "
                "learner said, confirmed, corrected or withdrew something is always stored "
                "as 'model_proposed' - an owned track proves scope, not what the learner "
                "said. The response reports the downgrade."
            ),
        },
        "recommended_action": {"type": "string", "enum": [a.value for a in Action]},
        "confidence": {"type": "string", "enum": [c.value for c in Confidence]},
        "durability": {
            "type": "string",
            "enum": [d.value for d in Durability],
            "description": (
                "What happens to the proposal. 'session' is returned to you and never "
                "stored, because this plugin has only durable SQLite and no session-scoped "
                "store. 'short_term' is stored with an expiry and swept once it passes. "
                "'durable' is kept until something replaces it."
            ),
        },
        "confirmation_state": {
            "type": "string",
            "enum": [s.value for s in ConfirmationState],
            "description": (
                "'learner_confirmed' and 'learner_declined' are always stored as "
                "'unconfirmed': neither an owned track nor anything else in a tool call "
                "can show that a learner reached a decision."
            ),
        },
        "replaces": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_VALUE_CHARS,
            "description": (
                "The existing memory entry this supersedes. Required when "
                "recommended_action is 'replace'."
            ),
        },
        "consented_need": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_VALUE_CHARS,
            "description": (
                "Accepted for compatibility and always refused. An 'accessibility' "
                "candidate cannot be stored at all: the consent, the need, the origin and "
                "the confirmation would all be written by you in this one call, so none of "
                "them shows that the learner agreed. Honour the need in conversation."
            ),
        },
        "evidence_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": (
                "Independent observations behind a 'repeated_evidence' proposal. Below the "
                "configured minimum the proposal is refused and stays temporary."
            ),
        },
        "track_id": _TRACK_ID,
    },
}

_ACCESSIBILITY_CONSENT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["consent_statement", "needs"],
    "description": (
        "Records what the learner said when agreeing, for your own reasoning and the "
        "response's audit trail. It does NOT authorise storage: accessibility needs are "
        "never written to storage under any circumstances, because this statement and the "
        "need beside it are both written by you in the same call. Honour the need in the "
        "current request and pass it again next session."
    ),
    "properties": {
        "consent_statement": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_VALUE_CHARS,
            "description": (
                "Your record of what the learner said when agreeing, in their words. "
                "Reported back to you; never treated as proof."
            ),
        },
        "needs": {
            "type": "array",
            "maxItems": MAX_LIST_ITEMS,
            "items": {"type": "string", "minLength": 1, "maxLength": MAX_VALUE_CHARS},
            "description": (
                "The exact needs they agreed to have remembered. None of them is stored: "
                "the response says so for each one."
            ),
        },
    },
}


SAVE_CONTEXT_SCHEMA: dict[str, Any] = {
    "name": SAVE_TOOL_NAME,
    "description": (
        "Save what you have learned about the person you are talking to. Temporary "
        "context is kept as unconfirmed conversational evidence and expires; an ongoing "
        "track is durable and requires explicit learner confirmation. Also validates "
        "memory candidates — proposals for you to weigh. The learner is identified from "
        "the Hermes session, not from any argument. This tool never reads or writes "
        "Hermes memory, and the response always says so."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "temporary_context": context_object(
                "What the learner has said this session. Stored as unconfirmed temporary "
                "context that expires; it never becomes a track on its own."
            ),
            "evidence_context": context_object(
                "What recent work suggests, as distinct from what the learner said. "
                "Adapts temporary context only; it never rewrites confirmed preferences."
            ),
            "corrections": {
                "type": "array",
                "maxItems": _MAX_ITEMS,
                "items": _CORRECTION,
                "description": "Explicit corrections, which supersede the value they correct.",
            },
            "track": _TRACK,
            "objectives": {
                "type": "array",
                "maxItems": _MAX_ITEMS,
                "items": _OBJECTIVE,
            },
            "memory_candidates": {
                "type": "array",
                "maxItems": _MAX_ITEMS,
                "items": _MEMORY_CANDIDATE,
            },
            "accessibility_consent": _ACCESSIBILITY_CONSENT,
        },
    },
}


def _prepare_parameters() -> dict[str, Any]:
    """Build the prepare schema from the component registry itself.

    Generated rather than written out, so the schema the model is shown and
    the rules the handler enforces are the same declarations. A component type
    added to the registry appears here automatically; one described here that
    the registry does not implement cannot exist.
    """
    from .components import components_schema, shared_definitions
    from .manifest import manifest_schema

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["manifest"],
        # Shapes every component type shares, stated once. The union of 31
        # fully inlined types is over 140 KB of schema on every request; these
        # references are the same declarations, written down a single time.
        "$defs": shared_definitions(),
        "properties": {
            "track_id": {
                **_TRACK_ID,
                "description": (
                    "Attach this experience to a confirmed learning track. Omit it for a "
                    "one-off exercise; a track is never created here."
                ),
            },
            "objective_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": (
                    "An existing objective on that track that this experience assesses. "
                    "Requires track_id."
                ),
            },
            "manifest": manifest_schema(components_schema()),
        },
    }


PREPARE_SCHEMA: dict[str, Any] = {
    "name": PREPARE_TOOL_NAME,
    "description": (
        "Prepare a bespoke exercise for the person you are talking to and store it as "
        "validated data. Use it whenever someone asks to practise, revise, be quizzed, "
        "drill, or test themselves on anything at all - any subject, any level. You "
        "design the exercise: choose the components, write the prompts, and supply the "
        "answer key. The answer key, rubrics, scoring rules, hints and feedback are kept "
        "server-side and are never returned to you, so nothing you get back can give the "
        "answers away. Returns an opaque experience id and a learner-safe summary. This "
        "tool stores an exercise; it does not run one, render one, or open anything - "
        "deliver it in conversation and say so. The learner is identified from the Hermes "
        "session, never from an argument."
    ),
    "parameters": _prepare_parameters(),
}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    GET_TOOL_NAME: GET_CONTEXT_SCHEMA,
    SAVE_TOOL_NAME: SAVE_CONTEXT_SCHEMA,
    PREPARE_TOOL_NAME: PREPARE_SCHEMA,
}
