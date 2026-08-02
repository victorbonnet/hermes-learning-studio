"""Tool handlers.

Handlers are the boundary between an LLM's arguments and this plugin's
storage, so they hold to four rules:

1. **Always return a JSON string.** Hermes' registry rejects anything else,
   and an exception escaping a handler becomes an opaque failure the agent
   cannot act on.
2. **Identity comes from the host.** The caller is resolved from Hermes'
   session context before anything else happens. No argument names a
   learner, so no argument can impersonate one.
3. **Validate against the advertised schema, at runtime.** Not every
   provider enforces a schema before dispatch, so the same declarations the
   schema is built from are re-checked here — before the database is opened,
   so a malformed request cannot half-apply.
4. **Leak nothing.** Filesystem paths, SQL text, and raw exception strings
   stay in the log. In particular an error must never reveal whether another
   learner's object exists — see :data:`service.NOT_FOUND_MESSAGE`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import service
from .config import ConfigError
from .consent import ConsentRequired
from .identity import IdentityError, resolve_principal
from .paths import PathResolutionError
from .runtime.errors import RuntimeUnavailable
from .schemas import (
    GET_TOOL_NAME,
    IMPORT_ASSET_TOOL_NAME,
    LAUNCH_TOOL_NAME,
    PREPARE_TOOL_NAME,
    RESULTS_TOOL_NAME,
    SAVE_TOOL_NAME,
    STATUS_TOOL_NAME,
    STOP_TOOL_NAME,
    TOOL_SCHEMAS,
)
from .validation import SchemaViolation, validate

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


def _checked(raw: Any, tool_name: str) -> dict[str, Any]:
    """Normalise and fully validate the incoming arguments."""
    if raw is None:
        args: dict[str, Any] = {}
    elif isinstance(raw, dict):
        args = raw
    else:
        raise SchemaViolation("arguments: must be an object")

    validate(args, TOOL_SCHEMAS[tool_name]["parameters"])
    return args


def _run(tool_name: str, raw: Any, call) -> str:
    """Shared entry path: validate, identify, dispatch, and never leak.

    Order matters. Arguments are checked before identity, and identity before
    storage, so an invalid request never reaches the database and an
    unidentifiable caller never creates a record.
    """
    try:
        args = _checked(raw, tool_name)
        principal = resolve_principal()
        return _ok(call(principal, args))
    except SchemaViolation as exc:
        return _error(str(exc))
    except IdentityError as exc:
        return _error(str(exc))
    except service.NotFoundError as exc:
        return _error(str(exc))
    except ConsentRequired as exc:
        # A refusal on policy grounds, not a failure. The message says what to
        # ask the learner; the reason is for logs.
        return _error(exc.message, refused=exc.reason)
    except RuntimeUnavailable as exc:
        # Already written to be shown: no path, port, process, or command in it.
        return _error(exc.message, refused=exc.reason)
    except service.ServiceError as exc:
        return _error(str(exc))
    except (ConfigError, PathResolutionError) as exc:
        # Safe to surface: these messages are written to describe the failure
        # without naming a path, a profile, or another learner.
        return _error(str(exc))
    except Exception as exc:
        logger.exception("%s failed: %s", tool_name, exc)
        return _error(_INTERNAL_ERROR)


def handle_get_context(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_get_context``."""

    def call(principal, args: dict[str, Any]) -> dict[str, Any]:
        return service.get_context(
            principal=principal,
            track_id=args.get("track_id"),
            track_name=args.get("track_name"),
            current_request=args.get("current_request"),
            include_memory_candidates=bool(args.get("include_memory_candidates", False)),
        )

    return _run(GET_TOOL_NAME, params, call)


def handle_save_context(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_save_context``."""

    def call(principal, args: dict[str, Any]) -> dict[str, Any]:
        return service.save_context(
            principal=principal,
            temporary_context=args.get("temporary_context"),
            evidence_context=args.get("evidence_context"),
            corrections=args.get("corrections") or [],
            track=args.get("track"),
            objectives=args.get("objectives") or [],
            memory_candidates=args.get("memory_candidates") or [],
            accessibility_consent=args.get("accessibility_consent"),
        )

    return _run(SAVE_TOOL_NAME, params, call)


def handle_prepare(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_prepare``.

    Note what is *not* passed through: nothing names a learner, and the
    experience id is generated during storage rather than accepted from the
    caller. The response is built by the service from the manifest's visible
    half, so no answer key can travel back up this path.
    """

    def call(principal, args: dict[str, Any]) -> dict[str, Any]:
        return service.prepare_experience(
            principal=principal,
            manifest=args.get("manifest"),
            track_id=args.get("track_id"),
            objective_id=args.get("objective_id"),
        )

    return _run(PREPARE_TOOL_NAME, params, call)


def handle_import_asset(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_import_asset``."""

    def call(principal, args: dict[str, Any]) -> dict[str, Any]:
        return service.import_asset(
            principal=principal,
            source_path=args["source_path"],
            title=args["title"],
            alt_text=args.get("alt_text"),
            decorative=bool(args.get("decorative", False)),
            provenance=args["provenance"],
            generation_prompt=args.get("generation_prompt"),
            track_id=args.get("track_id"),
        )

    return _run(IMPORT_ASSET_TOOL_NAME, params, call)


HANDLERS = {
    GET_TOOL_NAME: handle_get_context,
    SAVE_TOOL_NAME: handle_save_context,
    PREPARE_TOOL_NAME: handle_prepare,
    IMPORT_ASSET_TOOL_NAME: handle_import_asset,
}


# ── The on-demand runtime ─────────────────────────────────────────────────
#
# Four handlers over the same `_run` path as the rest: arguments validated
# against the advertised schema, identity resolved from the host, and every
# failure turned into a stated, actionable, leak-free message.
#
# Two error types are added to that path rather than handled here, so that a
# refusal travels the same route as every other one and cannot acquire a
# different shape: `RuntimeUnavailable` (the machine cannot do this) and
# `ConsentRequired` (it could, but not without asking first).


def handle_launch(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_launch``.

    Note what is not passed through, because it is the whole security story of
    this tool: no destination, no address, no process, no timeout. The
    experience is named by an opaque id whose ownership is checked in SQL, and
    everything else is derived from the host or the operator's configuration.
    """

    def call(principal, args: dict[str, Any]) -> dict[str, Any]:
        from . import launch

        return launch.launch_experience(
            principal=principal,
            experience_id=args["experience_id"],
            initiation=args["initiation"],
            learner_confirmed=bool(args.get("learner_confirmed", False)),
            confirmation_quote=args.get("confirmation_quote"),
        )

    return _run(LAUNCH_TOOL_NAME, params, call)


def handle_status(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_status``."""

    def call(principal, args: dict[str, Any]) -> dict[str, Any]:
        from .runtime import manager

        del principal, args
        return manager.status()

    return _run(STATUS_TOOL_NAME, params, call)


def handle_results(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_results``."""

    def call(principal, args: dict[str, Any]) -> dict[str, Any]:
        from . import launch

        return launch.launch_results(principal=principal, experience_id=args["experience_id"])

    return _run(RESULTS_TOOL_NAME, params, call)


def handle_stop(params: Any = None, **_kwargs: Any) -> str:
    """Handler for ``learning_studio_stop``.

    Identity is still resolved first, even though stopping is per-profile and
    takes no learner argument. A caller Hermes cannot identify is one this
    plugin does nothing for, and making that uniform is worth more than the one
    call it saves.
    """

    def call(principal, args: dict[str, Any]) -> dict[str, Any]:
        from .runtime import manager

        del principal, args
        return manager.stop()

    return _run(STOP_TOOL_NAME, params, call)


HANDLERS.update(
    {
        LAUNCH_TOOL_NAME: handle_launch,
        STATUS_TOOL_NAME: handle_status,
        RESULTS_TOOL_NAME: handle_results,
        STOP_TOOL_NAME: handle_stop,
    }
)
