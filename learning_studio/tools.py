"""Tool handlers.

Handlers are the boundary between an LLM's arguments and this plugin's
storage, so they hold to three rules:

1. **Always return a JSON string.** Hermes' registry rejects anything else,
   and an exception escaping a handler becomes an opaque failure the agent
   cannot act on.
2. **Fail closed, and explain.** Bad input produces a refusal that says what
   was wrong, never a partial write.
3. **Leak nothing.** Filesystem paths, SQL text, and raw exception strings
   stay in the log. In particular an error must never reveal whether another
   learner's object exists — see :data:`service.NOT_FOUND_MESSAGE`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import service
from .config import ConfigError
from .schemas import GET_TOOL_NAME, SAVE_TOOL_NAME

logger = logging.getLogger(__name__)

#: Shown to the agent when something unexpected fails. Deliberately free of
#: paths, SQL, and exception detail; the real error goes to the log.
_INTERNAL_ERROR = (
    "The Learning Studio could not complete that request. Nothing was saved. "
    "Continue the session in conversation."
)


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extra}, ensure_ascii=False)


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _params(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise service.ValidationError("tool arguments must be an object")
    return raw


def _reject_unknown(params: dict[str, Any], allowed: frozenset[str]) -> None:
    """Enforce ``additionalProperties: false`` at runtime as well as in schema.

    Providers do not all validate schemas before dispatch, so an unexpected
    key has to be refused here too rather than silently ignored.
    """
    unknown = set(params) - allowed
    if unknown:
        raise service.ValidationError(f"unknown argument(s): {', '.join(sorted(unknown))}")


_GET_ARGS = frozenset(
    {
        "learner_key",
        "track_id",
        "track_name",
        "current_request",
        "include_memory_candidates",
    }
)

_SAVE_ARGS = frozenset(
    {
        "learner_key",
        "temporary_context",
        "evidence_context",
        "corrections",
        "track",
        "objectives",
        "memory_candidates",
        "remember_accessibility_needs",
    }
)


def _bool(raw: Any, label: str) -> bool:
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise service.ValidationError(f"{label} must be true or false")
    return raw


def _list(raw: Any, label: str) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise service.ValidationError(f"{label} must be an array")
    if not all(isinstance(item, dict) for item in raw):
        raise service.ValidationError(f"{label} entries must be objects")
    return raw


def _dict(raw: Any, label: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise service.ValidationError(f"{label} must be an object")
    return raw


def _optional_str(raw: Any, label: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise service.ValidationError(f"{label} must be a non-empty string")
    return raw.strip()


def handle_get_context(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_get_context``."""
    try:
        args = _params(params)
        _reject_unknown(args, _GET_ARGS)
        payload = service.get_context(
            learner_key=args.get("learner_key"),
            track_id=_optional_str(args.get("track_id"), "track_id"),
            track_name=_optional_str(args.get("track_name"), "track_name"),
            current_request=_dict(args.get("current_request"), "current_request"),
            include_memory_candidates=_bool(
                args.get("include_memory_candidates"), "include_memory_candidates"
            ),
        )
        return _ok(payload)
    except service.NotFoundError as exc:
        return _error(str(exc))
    except service.ServiceError as exc:
        return _error(str(exc))
    except ConfigError as exc:
        return _error(f"Learning Studio configuration is invalid: {exc}")
    except Exception as exc:
        logger.exception("learning_studio_get_context failed: %s", exc)
        return _error(_INTERNAL_ERROR)


def handle_save_context(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_save_context``."""
    try:
        args = _params(params)
        _reject_unknown(args, _SAVE_ARGS)
        payload = service.save_context(
            learner_key=args.get("learner_key"),
            temporary_context=_dict(args.get("temporary_context"), "temporary_context"),
            evidence_context=_dict(args.get("evidence_context"), "evidence_context"),
            corrections=_list(args.get("corrections"), "corrections"),
            track=_dict(args.get("track"), "track"),
            objectives=_list(args.get("objectives"), "objectives"),
            memory_candidates=_list(args.get("memory_candidates"), "memory_candidates"),
            remember_accessibility_needs=_bool(
                args.get("remember_accessibility_needs"), "remember_accessibility_needs"
            ),
        )
        return _ok(payload)
    except service.NotFoundError as exc:
        return _error(str(exc))
    except service.ServiceError as exc:
        return _error(str(exc))
    except ConfigError as exc:
        return _error(f"Learning Studio configuration is invalid: {exc}")
    except Exception as exc:
        logger.exception("learning_studio_save_context failed: %s", exc)
        return _error(_INTERNAL_ERROR)


HANDLERS = {
    GET_TOOL_NAME: handle_get_context,
    SAVE_TOOL_NAME: handle_save_context,
}
