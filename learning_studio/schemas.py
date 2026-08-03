"""JSON schemas for the eight registered tools.

The schemas are the plugin's actual security boundary against a confused or
adversarial caller, so they are restrictive by construction:

- ``additionalProperties: false`` everywhere, so a typo is an error rather
  than a silently ignored field.
- Every string is bounded, every array is bounded.
- The asset import tool alone accepts a local path; runtime containment limits
  it to Hermes' active-profile image cache. No field accepts executable code
  or SQL.
- Nothing names a subject, a language, or a discipline.
- Nothing in the runtime tools names a machine. No property anywhere accepts a
  host, port, URL, executable, command, process id, timeout, environment
  variable, chat, or account — so the model cannot supply one, and the values
  that decide those things come from the operator's config.yaml and from
  Hermes' own session context.
"""

from __future__ import annotations

from typing import Any

from .candidates import Action, Category, Confidence, ConfirmationState, Durability, Origin
from .consent import INITIATIONS, MAX_QUOTE_CHARS, MIN_QUOTE_CHARS
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
from .safety import expressible_rule_summary, text_pattern

GET_TOOL_NAME = "learning_studio_get_context"
SAVE_TOOL_NAME = "learning_studio_save_context"
PREPARE_TOOL_NAME = "learning_studio_prepare"
IMPORT_ASSET_TOOL_NAME = "learning_studio_import_asset"

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
        "tool stores an exercise; it does not run one, render one, or open anything. Call "
        "learning_studio_launch with the experience_id it returns to put it on the "
        "learner's screen, and deliver it in conversation if that is unavailable. The "
        "learner is identified from the Hermes session, never from an argument."
    ),
    "parameters": _prepare_parameters(),
}


IMPORT_ASSET_SCHEMA: dict[str, Any] = {
    "name": IMPORT_ASSET_TOOL_NAME,
    "description": (
        "Securely import a real image produced or selected by the host into managed Learning "
        "Studio storage. The local source must be an absolute path returned from Hermes' "
        "trusted profile image cache. Returns an opaque asset id and safe metadata; never "
        "invent an id or reuse a local path in an exercise. This tool does not generate images."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["source_path", "title", "provenance"],
        "allOf": [
            {
                "if": {
                    "properties": {"decorative": {"const": True}},
                    "required": ["decorative"],
                },
                "then": {"not": {"required": ["alt_text"]}},
                "else": {"required": ["alt_text"]},
            }
        ],
        "properties": {
            "source_path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": (
                    "Absolute local path from a real Hermes image tool result. Only files "
                    "inside the active profile's trusted image cache are accepted."
                ),
            },
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "pattern": text_pattern(),
                "description": expressible_rule_summary(),
            },
            "alt_text": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
                "pattern": text_pattern(),
                "description": (
                    "Useful text conveying what a meaningful image contributes. Omit only "
                    "when decorative is explicitly true. " + expressible_rule_summary()
                ),
            },
            "decorative": {"type": "boolean"},
            "provenance": {
                "type": "string",
                "enum": [
                    "host_image_generation",
                    "learner_provided",
                    "operator_selected",
                ],
            },
            "generation_prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "pattern": r"^(?=.*\S)[\s\S]+$",
                "description": "Generation prompt retained server-side for provenance.",
            },
            "track_id": _TRACK_ID,
        },
    },
}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    GET_TOOL_NAME: GET_CONTEXT_SCHEMA,
    SAVE_TOOL_NAME: SAVE_CONTEXT_SCHEMA,
    PREPARE_TOOL_NAME: PREPARE_SCHEMA,
    IMPORT_ASSET_TOOL_NAME: IMPORT_ASSET_SCHEMA,
}


# ── The on-demand runtime ─────────────────────────────────────────────────
#
# Four tools that start, inspect, report on, and stop a real process — and
# whose payloads, between them, carry one opaque identifier, one enumerated
# word, one boolean, and one quotation.
#
# What is deliberately absent from every schema below: a host, a port, a URL,
# an executable, a command, an argument, a process id, a lock path, an
# environment variable, a timeout, a chat id, a Telegram user id, a bot token,
# and a profile. None of those has a property here, so none of them can be
# supplied, mistyped, or injected. They come from the operator's config.yaml
# and from Hermes' own session context, and the model has no way to reach
# either.

LAUNCH_TOOL_NAME = "learning_studio_launch"
STATUS_TOOL_NAME = "learning_studio_status"
RESULTS_TOOL_NAME = "learning_studio_results"
STOP_TOOL_NAME = "learning_studio_stop"

_EXPERIENCE_ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    "description": "Opaque experience id returned by learning_studio_prepare.",
}

#: Tools that take nothing at all. Still an object with
#: ``additionalProperties: false``, so a caller that invents a parameter is
#: told rather than quietly ignored.
_NO_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}


LAUNCH_SCHEMA: dict[str, Any] = {
    "name": LAUNCH_TOOL_NAME,
    "description": (
        "Open a prepared exercise on the learner's screen: start the Learning Studio "
        "runtime, and send them a button that opens it. Use it after "
        "learning_studio_prepare when someone has asked to practise, revise, be quizzed, or "
        "drill something. The exercise opens in the private Telegram conversation you are "
        "already in - the destination comes from the session, never from you, and there is "
        "no argument for it. You must quote the learner's own words from the message you "
        "are replying to: the Studio checks that quotation against the message the platform "
        "actually delivered, so an exercise cannot be opened on words nobody wrote. Nothing "
        "about how they do will be stored: no score, no attempt, no progress record. Only "
        "claim an exercise opened if this call succeeds and reports button_delivered; if it "
        "refuses, run the exercise in conversation instead and say that is what you are "
        "doing."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["experience_id", "initiation", "learner_quote"],
        # A suggestion additionally needs the explicit confirmation flag. Stated
        # in the schema as well as in the handler so a provider that validates
        # before dispatch refuses the same payloads this code would.
        "allOf": [
            {
                "if": {
                    "properties": {"initiation": {"const": "agent_suggestion"}},
                    "required": ["initiation"],
                },
                # `const: True`, not merely "present". Advertising that the
                # field is required while accepting `false` for it made the
                # schema describe a payload the handler refuses: a provider
                # that validated before dispatch let it through, and the
                # refusal arrived from somewhere the model had been told was
                # already satisfied.
                "then": {
                    "required": ["learner_confirmed"],
                    "properties": {"learner_confirmed": {"const": True}},
                },
            }
        ],
        "properties": {
            "experience_id": _EXPERIENCE_ID,
            "initiation": {
                "type": "string",
                "enum": list(INITIATIONS),
                "description": (
                    "'learner_request' when they asked to practise, revise, be tested, or "
                    "open the exercise - that request is the agreement, and no second ask is "
                    "needed. 'agent_suggestion' when you proposed it and they had not asked; "
                    "that needs their explicit yes first. You are reading what they meant; "
                    "the words themselves are checked against the real message."
                ),
            },
            "learner_confirmed": {
                "type": "boolean",
                "description": (
                    "Required with 'agent_suggestion': your reading that the learner's "
                    "current message agrees to the exercise you proposed. The words are "
                    "verified; what they mean is your judgement."
                ),
            },
            "learner_quote": {
                "type": "string",
                "minLength": MIN_QUOTE_CHARS,
                "maxLength": MAX_QUOTE_CHARS,
                # Length is not content: `"    "` satisfied `minLength` and
                # was then refused as unusable, which is the same mismatch as
                # the confirmation flag above.
                #
                # A *necessary* condition, not an exact one. What the handler
                # actually requires is four characters after whitespace is
                # collapsed, and encoding that normaliser as a regex would
                # produce something nobody could read and that would start
                # rejecting quotations the handler accepts — "a b c" is five
                # characters collapsed and three otherwise. Requiring at least
                # one non-space character removes the reported gap without
                # inventing a stricter rule than the one being enforced.
                "pattern": "\\S",
                "description": (
                    "A few words copied exactly from the learner's CURRENT message - the "
                    "one you are replying to. Required for both kinds of launch. The Studio "
                    "checks the quotation against the message the platform delivered, so it "
                    "must be their words, not your paraphrase. One message opens one "
                    "exercise: a later launch needs a newer message."
                ),
            },
        },
    },
}


STATUS_SCHEMA: dict[str, Any] = {
    "name": STATUS_TOOL_NAME,
    "description": (
        "Report whether a Learning Studio runtime is open for this profile, and whether this "
        "machine can open one at all. Takes no arguments: it always reports on the current "
        "profile's own runtime and can see no other. Use it to explain to the learner why an "
        "exercise cannot be opened - the answer says whether the runtime has been prepared "
        "and whether the tunnel program is installed, both of which are an operator's job. "
        "It reports no address, and nothing about anyone's performance."
    ),
    "parameters": _NO_PARAMETERS,
}


RESULTS_SCHEMA: dict[str, Any] = {
    "name": RESULTS_TOOL_NAME,
    "description": (
        "Report what happened to an exercise you opened: whether the learner opened it, how "
        "far through they are, and whether they finished. That is all this can know. "
        "Nothing is marked and no attempt is stored in this release, so there is no score, "
        "no mastery estimate, and no review schedule to fetch - do not describe one. It does "
        "not return the learner's answers either; ask them how it went. The exercise must be "
        "one prepared for the person you are talking to."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["experience_id"],
        "properties": {"experience_id": _EXPERIENCE_ID},
    },
}


STOP_SCHEMA: dict[str, Any] = {
    "name": STOP_TOOL_NAME,
    "description": (
        "Close this profile's Learning Studio runtime and its temporary public address. "
        "Takes no arguments and can stop nothing but this profile's own runtime. Safe to "
        "call when nothing is running. The runtime also stops itself once it has been idle "
        "or has been open for its maximum time, so this is for finishing early rather than "
        "for tidying up. Stopping loses no record of how anyone did, because no score or "
        "attempt was being kept in the first place."
    ),
    "parameters": _NO_PARAMETERS,
}


TOOL_SCHEMAS.update(
    {
        LAUNCH_TOOL_NAME: LAUNCH_SCHEMA,
        STATUS_TOOL_NAME: STATUS_SCHEMA,
        RESULTS_TOOL_NAME: RESULTS_SCHEMA,
        STOP_TOOL_NAME: STOP_SCHEMA,
    }
)
