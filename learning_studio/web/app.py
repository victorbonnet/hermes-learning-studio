"""The protected Mini App API.

Six routes, one authentication rule, and no way round it.

**Every route is protected.** Health included. There is no public endpoint, no
"just for the loading screen" exception, and no development bypass: each
request must carry a Telegram ``initData`` payload that verifies against the
bot token, and every route except the bootstrap must additionally carry a
session token that was minted for *that* Telegram account.

**Authorisation is an intersection and it fails closed.** A verified payload
buys nothing on its own; the account must also be on the profile's Telegram
allowlist, optionally narrowed by this plugin's configuration. No allowlist
means nobody, an unreadable configuration means nobody, and a group launch
means nobody.

**Nothing hidden can leave.** The API serves the stored *learner payloads*,
which were constructed from an allowlist when the experience was prepared and
never contained an answer key, rubric, hint, or branch in the first place. The
evaluator-only tables are not read anywhere in this module — there is no query
here that could return one.

**Errors say little.** A missing experience, one belonging to another learner,
and one belonging to another profile are the same 404. An invalid session, an
expired session, and someone else's session are the same 401. Distinguishing
them would turn every identifier into an oracle.

What this PR deliberately does not do: render anything, score anything, or
store an attempt. Answers are held in the session for its lifetime and the
summary reports progress, not marks. Grading and durable attempts arrive with
the evaluation runtime, and inventing half of one here would mean storing
learner performance data before the design that governs it exists.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..service import NotFoundError, ServiceError
from ..sessions import SessionError, SessionScope
from ..telegram_auth import InitDataError, verify_init_data
from .dependencies import Dependencies, build_dependencies, user_log_reference
from .security import (
    SECURITY_HEADERS,
    InvalidResponseValue,
    RateLimited,
    RateLimiter,
    RequestTooLarge,
    enforce_body_limit,
    log_request,
    validate_response_value,
)

logger = logging.getLogger(__name__)

#: Carries the raw ``initData`` string Telegram handed the webview. A custom
#: header rather than a cookie or query parameter: cookies are sent by the
#: browser on any request the page can be made to issue, and query parameters
#: end up in logs and referrers. A custom header also cannot be sent
#: cross-origin without a preflight this server never answers.
INIT_DATA_HEADER = "X-Telegram-Init-Data"

#: The opaque session token minted by the bootstrap route.
SESSION_HEADER = "X-Learning-Studio-Session"

#: Client-facing messages. Fixed strings, chosen so that none of them
#: distinguishes "does not exist" from "not yours".
UNAUTHENTICATED = "Telegram authentication failed."
FORBIDDEN = "This Telegram account is not authorised to use the Learning Studio here."
SESSION_REQUIRED = "This learning session is no longer valid. Reopen the exercise."
NOT_FOUND = "No such prepared exercise for this learner."
BAD_REQUEST = "That request could not be understood."
TOO_LARGE = "That request was too large."
RATE_LIMITED = "Too many requests. Try again shortly."
INTERNAL = "The Learning Studio could not complete that request."

#: Said on every result summary, because the honest answer to "how did I do?"
#: in this PR is "nothing has been marked".
NOT_SCORED_NOTICE = (
    "Responses are recorded for this session only. Nothing has been marked, and no "
    "attempt or score has been stored."
)


class ApiError(Exception):
    """An HTTP failure with a message already safe to return."""

    def __init__(self, status: int, message: str, *, reason: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason
        self.headers = headers or {}


def create_app(dependencies: Dependencies | None = None):
    """Build the ASGI application.

    ``dependencies`` is the only injection point. It cannot disable
    authentication — there is no branch that consults it for that — it can only
    substitute *which* bot token, allowlist, clock, and storage the same
    verification runs against.

    FastAPI is imported at this module's scope rather than inside this
    function: with postponed annotations, the framework resolves a route's
    ``Request`` annotation against module globals, and a function-local import
    leaves it unresolvable — which FastAPI reads as "an undeclared query
    parameter" and answers 422 to every request. The lazy-import rule this
    plugin follows is about *the plugin surface*, and it still holds: nothing
    outside this package imports this module, so ``register(ctx)`` never
    reaches FastAPI on an install without the ``web`` extra.
    """
    deps = dependencies or build_dependencies()
    config = deps.config
    limiter = RateLimiter(
        limit=config.mini_app_rate_limit_requests,
        window_seconds=config.mini_app_rate_limit_window_seconds,
        clock=deps.clock,
    )

    app = FastAPI(
        title="Hermes Learning Studio Mini App API",
        # No interactive docs and no schema endpoint: they are an unauthenticated
        # description of every route on a server whose whole point is that
        # everything is authenticated.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # ── Cross-cutting ────────────────────────────────────────────────────

    @app.middleware("http")
    async def secure_every_response(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:  # pragma: no cover - defensive; handlers convert theirs
            logger.exception("unhandled error serving %s", request.url.path)
            response = JSONResponse({"error": INTERNAL}, status_code=500)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        log_request(
            level=logging.WARNING,
            event="request_refused",
            route=request.url.path,
            method=request.method,
            status=exc.status,
            reason=exc.reason,
        )
        return JSONResponse(
            {"error": exc.message}, status_code=exc.status, headers=exc.headers or None
        )

    # ── Authentication and authorisation ─────────────────────────────────

    def authenticate(request: Request, *, bootstrap: bool):
        """Verify Telegram auth, authorise the account, and rate-limit it.

        Run on *every* route. ``bootstrap`` only chooses the freshness window:
        opening a session demands recently signed ``initData``, while later
        calls in that session accept a payload as old as a session may live,
        because Telegram signs once at launch and the session's own expiry is
        what bounds the rest.
        """
        raw = request.headers.get(INIT_DATA_HEADER, "")
        max_age = (
            config.mini_app_init_data_max_age_seconds
            if bootstrap
            else max(
                config.mini_app_init_data_max_age_seconds,
                config.mini_app_session_ttl_seconds,
            )
        )
        try:
            verified = verify_init_data(
                raw,
                bot_token=deps.bot_token(),
                now=int(deps.now()),
                max_age_seconds=max_age,
            )
        except InitDataError as exc:
            raise ApiError(401, UNAUTHENTICATED, reason=exc.reason) from exc

        user_ref = user_log_reference(verified.user_id)
        allowed = deps.allowed_users()
        if verified.user_id not in allowed:
            raise ApiError(403, FORBIDDEN, reason="not_on_allowlist")

        try:
            limiter.check(f"user:{verified.user_id}")
        except RateLimited as exc:
            raise ApiError(
                429,
                RATE_LIMITED,
                reason="rate_limited_user",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        return verified, user_ref

    def authorise_session(request: Request):
        """Authenticate, then resolve the session this request may act inside."""
        verified, user_ref = authenticate(request, bootstrap=False)
        token = request.headers.get(SESSION_HEADER, "")
        try:
            session = deps.sessions.resolve(
                token, profile=deps.profile(), telegram_user_id=verified.user_id
            )
        except SessionError as exc:
            raise ApiError(401, SESSION_REQUIRED, reason=exc.reason) from exc

        try:
            limiter.check(f"session:{session.ref}")
        except RateLimited as exc:
            raise ApiError(
                429,
                RATE_LIMITED,
                reason="rate_limited_session",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

        log_request(
            level=logging.DEBUG,
            event="session_request",
            route=request.url.path,
            method=request.method,
            session_ref=session.ref,
            user_ref=user_ref,
        )
        return verified, session

    async def json_body(request: Request) -> dict[str, Any]:
        """Read a bounded JSON object body, or refuse."""
        body = await request.body()
        try:
            enforce_body_limit(
                request.headers.get("content-length"), body, config.mini_app_max_request_bytes
            )
        except RequestTooLarge as exc:
            raise ApiError(413, TOO_LARGE, reason="body_too_large") from exc

        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise ApiError(400, BAD_REQUEST, reason="body_not_json") from exc
        if not isinstance(parsed, dict):
            raise ApiError(400, BAD_REQUEST, reason="body_not_an_object")
        return parsed

    def load_bundle(verified, experience_id: str):
        """Ownership-checked experience read, with service errors made safe."""
        try:
            return deps.load_experience(deps.principal(verified.user_id), experience_id)
        except NotFoundError as exc:
            raise ApiError(404, NOT_FOUND, reason="experience_not_found") from exc
        except ServiceError as exc:
            raise ApiError(400, BAD_REQUEST, reason="experience_unavailable") from exc

    # ── Routes ───────────────────────────────────────────────────────────

    @app.get("/api/health")
    async def health(request: Request):
        """Authenticated liveness. Says nothing a stranger could use."""
        _verified, user_ref = authenticate(request, bootstrap=False)
        log_request(event="health", route="/api/health", user_ref=user_ref, status=200)
        return JSONResponse({"ok": True, "service": "learning-studio", "authenticated": True})

    @app.post("/api/session")
    async def open_session(request: Request):
        """Exchange fresh ``initData`` for a session bound to one experience."""
        verified, user_ref = authenticate(request, bootstrap=True)
        payload = await json_body(request)

        experience_id = payload.get("experience_id")
        if not isinstance(experience_id, str) or not experience_id.strip():
            raise ApiError(400, BAD_REQUEST, reason="experience_id_missing")

        bundle = load_bundle(verified, experience_id.strip())
        experience = bundle.experience
        token, session = deps.sessions.create(
            SessionScope(
                profile=deps.profile(),
                telegram_user_id=verified.user_id,
                learner_id=bundle.learner_id,
                experience_id=str(experience["experience_id"]),
                track_id=experience.get("track_id"),
            ),
            component_count=len(experience["components"]),
        )
        log_request(
            event="session_opened",
            route="/api/session",
            user_ref=user_ref,
            session_ref=session.ref,
            status=201,
        )
        return JSONResponse(
            {
                "session_token": token,
                "expires_in_seconds": int(session.expires_at - session.created_at),
                "experience": _experience_summary(experience),
                "progress": _progress(session),
            },
            status_code=201,
        )

    @app.get("/api/session/component")
    async def current_component(request: Request):
        """The component the learner is on, as stored — nothing more."""
        verified, session = authorise_session(request)
        experience = load_bundle(verified, session.scope.experience_id).experience
        return JSONResponse(
            {
                "progress": _progress(session),
                "component": _component_at(experience, session.position),
            }
        )

    @app.post("/api/session/answer")
    async def submit_answer(request: Request):
        """Record one response against the component currently in view.

        The component is the session's current one, not one the request names
        freely: accepting an arbitrary ID would let a caller walk the whole
        experience out of order, and the ID is checked rather than trusted so
        that a stale client cannot silently answer the wrong question.
        """
        verified, session = authorise_session(request)
        payload = await json_body(request)
        experience = load_bundle(verified, session.scope.experience_id).experience

        if session.completed:
            raise ApiError(409, "This exercise is already finished.", reason="already_complete")

        current = _component_at(experience, session.position)
        if current is None:
            raise ApiError(409, "This exercise is already finished.", reason="no_component")

        claimed = payload.get("component_id")
        if not isinstance(claimed, str) or claimed != current["component_id"]:
            raise ApiError(409, "That is not the current question.", reason="component_mismatch")

        try:
            response = validate_response_value(payload.get("response"))
        except InvalidResponseValue as exc:
            raise ApiError(400, BAD_REQUEST, reason="response_invalid") from exc

        session.answers[current["component_id"]] = response
        session.position += 1
        if session.position >= session.component_count:
            session.completed_at = deps.now()

        log_request(
            event="answer_recorded",
            route="/api/session/answer",
            session_ref=session.ref,
            status=200,
        )
        return JSONResponse(
            {
                "recorded": True,
                "scored": False,
                "progress": _progress(session),
                "next_component": _component_at(experience, session.position),
                "notice": NOT_SCORED_NOTICE,
            }
        )

    @app.get("/api/session/result")
    async def result(request: Request):
        """What happened in this session: progress, not marks."""
        verified, session = authorise_session(request)
        experience = load_bundle(verified, session.scope.experience_id).experience
        return JSONResponse(
            {
                "experience_id": session.scope.experience_id,
                "title": experience["title"],
                "progress": _progress(session),
                "scored": False,
                "answered_components": sorted(session.answers),
                "notice": NOT_SCORED_NOTICE,
            }
        )

    @app.get("/api/assets/{asset_id}")
    async def managed_asset(asset_id: str, request: Request):
        """Serve one managed image, twice constrained.

        Ownership is checked in the service against ``(profile, learner)``, and
        the session's own experience must actually reference the asset. Owning
        an image is not enough to fetch it through an exercise that never
        mentions it.
        """
        verified, session = authorise_session(request)
        bundle = load_bundle(verified, session.scope.experience_id)

        if asset_id not in bundle.asset_ids:
            raise ApiError(404, NOT_FOUND, reason="asset_not_in_experience")

        try:
            asset = deps.load_asset(deps.principal(verified.user_id), asset_id)
        except NotFoundError as exc:
            raise ApiError(404, NOT_FOUND, reason="asset_not_found") from exc
        except ServiceError as exc:
            raise ApiError(404, NOT_FOUND, reason="asset_unverifiable") from exc

        log_request(
            event="asset_served",
            route="/api/assets",
            session_ref=session.ref,
            asset_ref=asset.asset_id,
            status=200,
        )
        return Response(
            content=asset.data,
            media_type=asset.mime_type,
            headers={
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app


# ── Projections ──────────────────────────────────────────────────────────
#
# Every response body below is *constructed*, field by field, from the stored
# learner payload. Nothing is passed through wholesale, so a field added to the
# stored projection later cannot reach a client by accident.


def _experience_summary(experience: dict[str, Any]) -> dict[str, Any]:
    return {
        "experience_id": experience["experience_id"],
        "title": experience["title"],
        "instructions": experience["instructions"],
        "ui_locale": experience["ui_locale"],
        "content_locale": experience["content_locale"],
        "expected_duration_minutes": experience["expected_duration_minutes"],
        "difficulty": experience["difficulty"],
        "accessibility": experience["accessibility"],
        "component_count": len(experience["components"]),
    }


def _component_at(experience: dict[str, Any], position: int) -> dict[str, Any] | None:
    components = experience["components"]
    if position < 0 or position >= len(components):
        return None
    component = components[position]
    return {
        "position": component["position"],
        "component_id": component["component_id"],
        "type": component["type"],
        "payload": component["payload"],
    }


def _progress(session) -> dict[str, Any]:
    return {
        "position": session.position,
        "component_count": session.component_count,
        "answered": len(session.answers),
        "completed": session.completed,
    }
