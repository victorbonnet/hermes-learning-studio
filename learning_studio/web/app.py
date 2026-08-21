"""The protected Mini App API, and the static shell that calls it.

Eight API routes, one authentication rule, and no way round it.

**Every route that can return learner data is protected.** Health included.
There is no development bypass: each such request must carry a Telegram
``initData`` payload that verifies against the bot token, and every route
except the bootstrap must additionally carry a session token that was minted
for *that* Telegram account.

**The frontend shell is the one unauthenticated thing here**, and it is not an
exception to the rule above so much as a consequence of how a webview loads a
page: the navigation that fetches the document cannot carry a header, so there
is no request in which the shell could prove who wants it. What is served is
six checked-in files that are byte-identical for every caller and contain no
learner data, no identifier, and no configured value — see
:mod:`learning_studio.web.static_files`. The document that comes back knows
nothing; it has to ask, with headers, for everything it displays.

**Authorisation is an intersection and it fails closed.** A verified payload
buys nothing on its own; the account must also be on the profile's Telegram
allowlist, optionally narrowed by this plugin's configuration. No allowlist
means nobody, an unreadable configuration means nobody, and a group launch
means nobody.

**Nothing hidden leaves except narrow, deliberate projections.** Every route that
serves a component serves the stored *learner payload*, which was constructed
from an allowlist when the experience was prepared and never contained an answer
key, rubric, hint, or branch in the first place.

One exception to "learner payload only" predates this PR: ``POST
/api/session/reveal``, which turns a flashcard over. It exists because
retrieval practice is not retrieval practice unless the learner finds out
whether they were right, and the component contract this plugin publishes
requires an explicit reveal. It is not a general read of the hidden half:
:func:`learning_studio.service.reveal_component_answer` returns *one string of
one field of one component type*, chosen by a mapping in that module, and
there is no query anywhere here that could return an evaluation record. The
reveal is granted only after an attempt has been committed, and that attempt is
frozen — so reading the answer and then improving the recall is refused rather
than merely discouraged.

The second exception is ``GET /api/session/summary``: once the whole session is
complete it scores every answered component and returns overall score,
per-component correct/incorrect, and the stored ``feedback`` for *that* outcome.
For an incorrect ``multiple_choice`` only, it may also project the prompt and the
visible labels for the selected and accepted options. The service re-authorises
ownership and verifies alias provenance; the web layer cannot request an
evaluator field. Scoring and that projection are computed once per session and
cached on it, so a refreshed or backgrounded webview sees the same result rather
than a second scoring pass that could disagree or write a second durable attempt.
See :func:`learning_studio.service.record_attempt`.

**Errors say little.** A missing experience, one belonging to another learner,
and one belonging to another profile are the same 404. An invalid session, an
expired session, and someone else's session are the same 401. Distinguishing
them would turn every identifier into an oracle.

The interface this API serves lives in ``static/`` and is described in
:mod:`learning_studio.web.static_files`; the shell is served from here, from a
closed allowlist of files.

What this still deliberately does not do: return a learner's free-text response,
the answer object, a rubric, a hint, a branch, or another component's feedback;
nor does it accept a rubric-graded criterion selection from a client. The one
submitted response it projects is an already-visible option label in the bounded
completed web session. Durable attempts and agent tools remain answer-free.
Open-ended rubric responses are recorded as attempted but not machine-graded — see
:mod:`learning_studio.evaluation` for why that is a design choice and not an
omission.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..responses import (
    INVALID_RESPONSE_MESSAGE,
    ResponseContractError,
    validate_component_response,
)
from ..service import (
    REVEALABLE_ANSWER_FIELDS,
    AliasState,
    ComponentAliases,
    NotFoundError,
    ServiceError,
)
from ..sessions import (
    SessionError,
    SessionProgress,
    SessionScope,
    SessionStep,
    SessionTransitionError,
)
from ..telegram_auth import InitDataError, verify_init_data
from .dependencies import Dependencies, build_dependencies, user_log_reference
from .security import (
    MAX_RESPONSE_CHARS,
    SECURITY_HEADERS,
    InvalidResponseValue,
    RateLimited,
    RateLimiter,
    RequestTooLarge,
    enforce_declared_length,
    enforce_measured_length,
    log_request,
    log_unhandled,
    validate_response_value,
)
from .static_files import (
    DOCUMENT,
    DOCUMENT_CONTENT_SECURITY_POLICY,
    STATIC_ASSETS,
    asset_bytes,
)

logger = logging.getLogger(__name__)

#: The URL paths that receive the document policy — derived from the allowlist's
#: own ``document`` flag rather than restated, so a second HTML page could not be
#: added without deciding which policy it runs under. ``/`` is what a Mini App
#: button opens; ``/index.html`` is what a reload may ask for.
DOCUMENT_PATHS = frozenset({"/"} | {asset.url_path for asset in STATIC_ASSETS if asset.document})

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
ATTEMPT_REQUIRED = "Write what you remember before turning the card over."
REVEAL_REQUIRED = "Turn the card over before rating your recall."
RECALL_FROZEN = "Your recall was recorded when you turned the card over."
NOT_REVEALABLE = "There is nothing to turn over on this card."
NOT_FINISHED = "This exercise is not finished yet."

#: Said while the exercise is still in progress: nothing is marked *yet*.
NOT_SCORED_NOTICE = (
    "Responses are recorded for this session and are not scored until the exercise is "
    "finished. Nothing has been marked yet."
)

#: Said on the final answer, so the client knows where to go next.
FINISHED_NOTICE = "That was the last one. Call GET /api/session/summary for the score."

#: Said on the completion screen itself.
SCORED_NOTICE = (
    "Scored from this attempt. Summary-level only: no stored answer key, rubric, or "
    "another component's feedback is ever included here."
)


class ApiError(Exception):
    """An HTTP failure with a message already safe to return."""

    def __init__(self, status: int, message: str, *, reason: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason
        self.headers = headers or {}


#: Every move a session can refuse, and the answer this API has always given for
#: it. The session decides *whether* a transition may happen; what a caller is
#: told about it is decided here, once, so no route invents its own wording.
_TRANSITION_ERRORS: dict[str, tuple[int, str]] = {
    "already_complete": (409, "This exercise is already finished."),
    "no_component": (409, "This exercise is already finished."),
    "component_mismatch": (409, "That is not the current question."),
    "type_not_revealable": (409, NOT_REVEALABLE),
    "reveal_required": (409, REVEAL_REQUIRED),
    "recall_changed_after_reveal": (409, RECALL_FROZEN),
    "not_completed": (409, NOT_FINISHED),
    "session_retired": (401, SESSION_REQUIRED),
}

#: A finished exercise has no card to turn over, and the reveal route has always
#: logged that as "no component" rather than as a completed submission. The
#: session states the fact; this renames it for the one route where it reads
#: differently.
_REVEAL_REASONS = {"already_complete": "no_component"}


def _transition_error(
    exc: SessionTransitionError, *, rename: dict[str, str] | None = None
) -> ApiError:
    """The HTTP answer to a refused session transition."""
    reason = (rename or {}).get(exc.reason, exc.reason)
    status, message = _TRANSITION_ERRORS[reason]
    return ApiError(status, message, reason=reason)


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

    # A URL is either in the allowlist or it is a 404. With slash redirection
    # on, ``/static/app.js/`` and ``/api/health/`` answer 307 towards paths this
    # server does intend to serve, which quietly makes the reachable URL space
    # larger than the declared one for no benefit anybody wanted.
    app.router.redirect_slashes = False

    # ── Cross-cutting ────────────────────────────────────────────────────

    @app.middleware("http")
    async def secure_every_response(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            log_unhandled(exc, route=request.url.path, method=request.method)
            response = JSONResponse({"error": INTERNAL}, status_code=500)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        if request.url.path in DOCUMENT_PATHS:
            # The one response that may execute anything, and therefore the only
            # one whose policy has to say what it may execute.
            response.headers["Content-Security-Policy"] = DOCUMENT_CONTENT_SECURITY_POLICY
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

        Run on *every* route. ``bootstrap`` only chooses the freshness window.
        Opening a session demands recently signed ``initData``. Later calls in
        that session accept a payload old enough to have opened the session and
        then lived out its whole term — ``session TTL + bootstrap window`` —
        because Telegram signs once at launch and the session's own expiry is
        what bounds the rest.

        That sum is not padding. Taking ``max()`` of the two, as this did
        first, silently shortened every session opened with slightly stale
        ``initData``: a payload 299 seconds old at bootstrap hit the 1800-second
        freshness bound after only ~1501 seconds of a session that had
        advertised 1800, so the API returned 401 while the session store still
        considered it live. The advertised TTL has to be the one a caller
        actually gets.
        """
        raw = request.headers.get(INIT_DATA_HEADER, "")
        max_age = (
            config.mini_app_init_data_max_age_seconds
            if bootstrap
            else config.mini_app_session_ttl_seconds + config.mini_app_init_data_max_age_seconds
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

        if verified.auth_date < session.auth_date:
            # The wider freshness window above is what makes this check worth
            # having: a session may be continued with the launch that opened it
            # or a newer one, never with an older captured payload.
            raise ApiError(401, SESSION_REQUIRED, reason="init_data_older_than_session")

        try:
            limiter.check(f"session:{session.ref}")
        except RateLimited as exc:
            raise ApiError(
                429,
                RATE_LIMITED,
                reason="rate_limited_session",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

        # The runtime's idle timer is driven from exactly here, and from the
        # bootstrap below. By this point the request has produced a verified
        # Telegram signature, an account on the allowlist, and a session token
        # minted for that account — so "somebody is studying" is a fact rather
        # than an inference from traffic arriving at a public URL.
        deps.sessions.note_activity()

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
        """Read a bounded JSON object body, or refuse — without buffering it all.

        The order is the point, and getting it wrong was a real defect here.
        ``await request.body()`` buffers the entire request *before* anything
        can be measured, so a declared-oversize request was fully resident in
        memory by the time it earned its 413. Now:

        1. an oversized or malformed ``Content-Length`` is refused having read
           nothing at all;
        2. otherwise the body is consumed chunk by chunk and abandoned the
           moment the cumulative size crosses the limit, which is what bounds a
           chunked request that declared no length.

        The peak cost of an oversized request is therefore one chunk, not the
        whole payload.
        """
        limit = config.mini_app_max_request_bytes
        try:
            enforce_declared_length(request.headers.get("content-length"), limit)
        except RequestTooLarge as exc:
            raise ApiError(413, TOO_LARGE, reason="declared_body_too_large") from exc

        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            try:
                enforce_measured_length(received, limit)
            except RequestTooLarge as exc:
                # Raising from inside the iteration abandons the rest of the
                # stream rather than draining it politely, which is the point:
                # the bytes still in flight are never pulled.
                raise ApiError(413, TOO_LARGE, reason="body_too_large") from exc
            chunks.append(chunk)
        body = b"".join(chunks)

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

    # ── The static shell ─────────────────────────────────────────────────
    #
    # One route per file, bound at startup. No path parameter, no directory
    # walk, no ``StaticFiles`` mount: the set of URLs this server answers is
    # written down in ``static_files.STATIC_ASSETS`` and nowhere else.

    def serve_static(asset):
        async def serve():
            return Response(content=asset_bytes(asset.filename), media_type=asset.media_type)

        return serve

    for static_asset in STATIC_ASSETS:
        app.add_api_route(
            static_asset.url_path,
            serve_static(static_asset),
            methods=["GET"],
            include_in_schema=False,
        )
    app.add_api_route("/", serve_static(DOCUMENT), methods=["GET"], include_in_schema=False)

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

        # Which exercise, and on whose authority.
        #
        # On a runtime this plugin launched there is a grant store, and the
        # only acceptable answer is a *launch id*: an opaque selector the
        # button carried in its URL fragment. The experience is then derived
        # from the grant, so a client cannot name an exercise of its own — not
        # even one it owns, because owning an exercise is not the same as
        # having been invited to open it.
        #
        # Without a grant store this is an operator-run server, where no launch
        # ever happened and the experience id is the selector it has always
        # been. Authentication, the allowlist, and the SQL ownership check are
        # what protect that path, exactly as before.
        grant = None
        if deps.grants is not None:
            grant = deps.grants.admit(
                launch_id=payload.get("launch_id") or "",
                telegram_user_id=verified.user_id,
            )
            if grant is None:
                # The same 404 an experience belonging to somebody else
                # produces, so a caller cannot tell a revoked launch, an
                # expired one, another account's, or one that never existed.
                raise ApiError(404, NOT_FOUND, reason="no_launch_grant")
            experience_id = grant.experience_id
        else:
            experience_id = payload.get("experience_id")
            if not isinstance(experience_id, str) or not experience_id.strip():
                raise ApiError(400, BAD_REQUEST, reason="experience_id_missing")
            experience_id = experience_id.strip()

        bundle = load_bundle(verified, experience_id)
        experience = bundle.experience

        if grant is not None and bundle.learner_id != grant.learner_id:
            # The grant was issued to a learner; the ownership check resolved a
            # learner. They are derived independently and must agree.
            raise ApiError(404, NOT_FOUND, reason="grant_learner_mismatch")

        def mint():
            return deps.sessions.create(
                SessionScope(
                    profile=deps.profile(),
                    telegram_user_id=verified.user_id,
                    learner_id=bundle.learner_id,
                    experience_id=str(experience["experience_id"]),
                    track_id=experience.get("track_id"),
                ),
                steps=_session_steps(experience),
                auth_date=verified.auth_date,
            )

        if grant is not None:
            # Minted under the grant's lock, so two simultaneous opens cannot
            # both produce a live token — and a reload resumes where the
            # learner was instead of starting again.
            admitted = deps.grants.admit_session(grant, mint)
            if admitted is None:
                # Revoked, expired, or superseded while this request was doing
                # its ownership check. Indistinguishable from every other way a
                # launch can fail to admit somebody, deliberately.
                raise ApiError(404, NOT_FOUND, reason="no_launch_grant")
            token, session = admitted
        else:
            token, session = mint()

        deps.sessions.note_activity()
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
                "progress": _progress_payload(session.snapshot().progress),
                # Told to the client rather than duplicated in it. These are the
                # bounds a submission is actually judged against, and they are
                # operator-tunable, so a frontend constant would eventually be a
                # lie that surfaced as a 413 the learner could not act on.
                "limits": {
                    "max_response_chars": MAX_RESPONSE_CHARS,
                    "max_request_bytes": config.mini_app_max_request_bytes,
                },
            },
            status_code=201,
        )

    @app.get("/api/session/component")
    async def current_component(request: Request):
        """The component the learner is on, as stored — nothing more."""
        verified, session = authorise_session(request)
        experience = load_bundle(verified, session.scope.experience_id).experience
        # One read: the progress a client is shown and the card it is shown
        # beside it are the same moment, not two.
        view = session.snapshot()
        return JSONResponse(
            {
                "progress": _progress_payload(view.progress),
                "component": _component_named(
                    experience,
                    view.progress.position,
                    view.current_component_id,
                ),
            }
        )

    @app.post("/api/session/answer")
    async def submit_answer(request: Request):
        """Record one response against the component currently in view.

        The component is the session's current one, not one the request names
        freely: accepting an arbitrary ID would let a caller walk the whole
        experience out of order, and the ID is checked rather than trusted so
        that a stale client cannot silently answer the wrong question.

        That check belongs to the session, and this route asks for it *first* —
        before it inspects the response at all — because refusing a stale
        component before its payload is the order in which a caller has always
        learned about the two kinds of mistake. The recording below asks again:
        the session is the authority, not this sequence.
        """
        verified, session = authorise_session(request)
        payload = await json_body(request)
        experience = load_bundle(verified, session.scope.experience_id).experience

        try:
            step = session.require_current_step(payload.get("component_id"))
        except SessionTransitionError as exc:
            raise _transition_error(exc) from exc

        # The response is checked against the component *as served*, so the
        # served component and the session's step have to be the same one. They
        # were built from this list, so they can only disagree if the stored
        # exercise changed underneath a live session — in which case neither is
        # trusted over the other.
        current = _component_at(experience, session.snapshot().progress.position)
        if current is None:
            raise ApiError(409, "This exercise is already finished.", reason="no_component")
        if current["component_id"] != step.component_id:
            raise ApiError(409, "That is not the current question.", reason="component_mismatch")

        try:
            submitted = validate_response_value(payload.get("response"))
        except InvalidResponseValue as exc:
            raise ApiError(400, BAD_REQUEST, reason="response_invalid") from exc

        # Generic bounds are not a contract. What follows checks the response
        # against *this* component — its declared shape, its required fields, and
        # the identifiers this learner was actually served — and translates the
        # served aliases back into the ones an evaluator will read. Both happen
        # before any session state moves, so a refused response leaves the
        # exercise exactly where it was.
        aliases = deps.component_aliases(
            deps.principal(verified.user_id),
            session.scope.experience_id,
            step.component_id,
        )
        try:
            response = validate_component_response(
                current["type"],
                current["payload"].get("content", {}),
                submitted,
                resolve=_identifier_resolver(aliases),
            )
        except ResponseContractError as exc:
            raise ApiError(400, INVALID_RESPONSE_MESSAGE, reason=exc.reason) from exc

        # Recording, advancing, the reveal contract, and the completion stamp are
        # one transition, and it is the session's. A response this route accepted
        # can still be refused here — an unturned card, a rewritten recall — and
        # nothing moves when it is.
        try:
            receipt = session.record_answer(step.component_id, response, now=deps.now())
        except SessionTransitionError as exc:
            raise _transition_error(exc) from exc

        log_request(
            event="answer_recorded",
            route="/api/session/answer",
            session_ref=session.ref,
            status=200,
        )
        # Every field below comes from the receipt, which was taken while the
        # transition still held the session's lock. Reading the position back
        # off the session here — which is what this used to do — meant a second
        # request arriving in between could supply the card this response named,
        # so one body reported this answer's progress beside another's next
        # question.
        return JSONResponse(
            {
                "recorded": True,
                "scored": False,
                "progress": _progress_payload(receipt.progress),
                "next_component": _component_named(
                    experience,
                    receipt.progress.position,
                    receipt.next_component_id,
                ),
                "notice": FINISHED_NOTICE if receipt.progress.completed else NOT_SCORED_NOTICE,
            }
        )

    @app.post("/api/session/reveal")
    async def reveal(request: Request):
        """Turn a flashcard over, having first committed an attempt.

        The narrowest route in this API, and the only one that discloses anything
        from the evaluator-only half. What makes it safe is not that the payload is
        small but that every one of these has to hold at once:

        - the request authenticates as a Telegram account on the allowlist;
        - it carries a session token minted for *that* account, in this profile;
        - the session's experience is the learner's own, checked in SQL;
        - the named component is the one the session is currently on;
        - the component's type is one whose answer may ever be shown at all;
        - and an attempt has been committed *in the same request*.

        The attempt is frozen the first time it arrives. A second reveal returns
        the same card and keeps the first attempt, so refreshing is safe and
        rewriting the recall after reading the answer is not possible — the
        submission is checked against the frozen value further down.

        Nothing is scored here and no attempt is stored durably. This route
        discloses one string and records one string in memory.
        """
        verified, session = authorise_session(request)
        payload = await json_body(request)
        # Ownership is re-checked before anything can be disclosed, and keeps its
        # own 404: an exercise that has gone away is not "a card with no back".
        load_bundle(verified, session.scope.experience_id)

        # Which card may be turned over is the session's answer, and it is asked
        # before the attempt is read so that a finished or out-of-order request
        # is refused as such rather than as a malformed one.
        try:
            step = session.require_current_step(payload.get("component_id"))
        except SessionTransitionError as exc:
            raise _transition_error(exc, rename=_REVEAL_REASONS) from exc

        attempt = payload.get("attempt")
        if not isinstance(attempt, str) or not attempt.strip():
            # An empty attempt is not an attempt. Granting a reveal for one would
            # turn retrieval practice into reading.
            raise ApiError(400, ATTEMPT_REQUIRED, reason="attempt_missing")
        try:
            attempt = validate_response_value(attempt)
        except InvalidResponseValue as exc:
            raise ApiError(400, BAD_REQUEST, reason="attempt_invalid") from exc

        def disclose() -> str:
            """The one authorised read of the evaluator-only half."""
            return deps.reveal_answer(
                deps.principal(verified.user_id),
                session.scope.experience_id,
                step.component_id,
            )

        # The session orders these: it retrieves *before* it commits anything, and
        # it refuses a card that has no back without asking the service at all.
        # Freezing first meant a card that could not be turned over — the wrong
        # type, a transient failure reading the store — still recorded the attempt
        # that bought the reveal, and the learner was then held to a recall they
        # never got an answer for. Now a failed reveal changes nothing and the next
        # attempt is the one that counts.
        try:
            outcome = session.reveal(step.component_id, attempt, disclose)
        except NotFoundError as exc:
            raise ApiError(404, NOT_REVEALABLE, reason="nothing_to_reveal") from exc
        except ServiceError as exc:
            raise ApiError(409, NOT_REVEALABLE, reason="type_not_revealable") from exc
        except SessionTransitionError as exc:
            raise _transition_error(exc, rename=_REVEAL_REASONS) from exc

        log_request(
            event="answer_revealed",
            route="/api/session/reveal",
            session_ref=session.ref,
            status=200,
        )
        return JSONResponse(
            {
                "component_id": step.component_id,
                "revealed": True,
                "back": outcome.back,
                # Echoed so a client that lost its own copy — a refresh, a
                # backgrounded webview — shows the attempt that actually counts
                # rather than an empty box it would then try to submit.
                "attempt": outcome.attempt,
                "notice": NOT_SCORED_NOTICE,
            }
        )

    @app.get("/api/session/result")
    async def result(request: Request):
        """What happened in this session: progress, and whether it is scored yet.

        The mark itself is not here — see ``GET /api/session/summary`` — this
        route only says whether one exists, so a client knows whether to ask.
        """
        verified, session = authorise_session(request)
        experience = load_bundle(verified, session.scope.experience_id).experience
        # Four facts about one session — how far, whether finished, whether
        # scored, and which components were answered — taken together. Read one
        # at a time they could describe different moments, and the notice would
        # then be chosen from a completion the counts beside it did not show.
        view = session.snapshot()
        return JSONResponse(
            {
                "experience_id": session.scope.experience_id,
                "title": experience["title"],
                "progress": _progress_payload(view.progress),
                "scored": bool(view.scored),
                "answered_components": list(view.answered_component_ids),
                "notice": (
                    SCORED_NOTICE
                    if view.scored
                    else (FINISHED_NOTICE if view.progress.completed else NOT_SCORED_NOTICE)
                ),
            }
        )

    @app.get("/api/session/summary")
    async def summary(request: Request):
        """The completion screen: score this session, once, and report it.

        Requires the session to be finished — every component answered or
        skipped — because scoring a partial attempt would report a mark for
        questions the learner has not reached yet. The result is computed the
        first time this is called and cached on the session; a repeat request
        (a refresh, a backgrounded webview resuming) returns the identical
        cached summary rather than scoring the session a second time or
        writing a second durable attempt.

        Both rules — finished first, scored once — belong to the session, so
        this route asks it rather than checking the completion itself and then
        hoping the cache agrees.
        """
        verified, session = authorise_session(request)

        def compute(completion) -> dict[str, Any]:
            """Score the finished session from its snapshot, and persist it.

            Everything read here comes from ``completion`` rather than from the
            session: the responses are already a copy, so nothing below can write
            back into the state it is scoring.
            """
            principal = deps.principal(verified.user_id)
            responses = completion.responses
            reviews = {
                component_id: review
                for component_id, response in responses.items()
                if (
                    review := deps.completion_answer_review(
                        principal=principal,
                        experience_id=session.scope.experience_id,
                        component_key=component_id,
                        response=response,
                    )
                )
                is not None
            }
            attempt = deps.record_attempt(
                principal,
                session.scope.experience_id,
                dict(responses),
                _iso(completion.created_at),
                _iso(completion.completed_at),
            )
            for component in attempt["components"]:
                if component.get("correct") is False:
                    review = reviews.get(component.get("component_id"))
                    if review is not None:
                        component["answer_review"] = review
            return attempt

        try:
            result = session.score_once(compute)
        except SessionTransitionError as exc:
            raise _transition_error(exc) from exc
        except NotFoundError as exc:
            raise ApiError(404, NOT_FOUND, reason="experience_not_found") from exc
        except ServiceError as exc:
            raise ApiError(400, BAD_REQUEST, reason="attempt_unavailable") from exc

        log_request(
            event="session_scored",
            route="/api/session/summary",
            session_ref=session.ref,
            status=200,
        )
        return JSONResponse(
            {
                "experience_id": session.scope.experience_id,
                "attempt_id": result["attempt_id"],
                "scored": True,
                "overall_score": result["overall_score"],
                "overall_max_score": result["overall_max_score"],
                "graded_component_count": result["graded_component_count"],
                "correct_component_count": result["correct_component_count"],
                "component_count": result["component_count"],
                "components": result["components"],
                "review": result["review"],
                "notice": SCORED_NOTICE,
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


def _identifier_resolver(aliases: ComponentAliases):
    """Turn a served identifier into the one an evaluator knows, or refuse.

    There is exactly one state in which an identifier passes through unchanged —
    :data:`AliasState.CANONICAL`, meaning the stored evaluator record is intact
    and simply predates aliasing, so the payload this learner was served names
    canonical identifiers. That is a *positive* finding about the record, not an
    inference from something being absent.

    :data:`AliasState.ALIASED` translates only after its exact correspondence is
    checked against producer evidence outside the evaluator JSON. The explicit
    legacy state :data:`AliasState.ALIASED_UNVERIFIED` also translates so existing
    mapping-only experiences keep working, but it never earns the verified name.
    Scheme 2 is not placed in that compatibility state: its stored inventory made
    a stronger claim that target permutations can violate, so it fails closed.

    Everything else refuses. The version before this returned the identifier
    unchanged whenever the lookup came back empty, which reads as a harmless
    default and is not one: a missing evaluator row, a malformed marker, an
    unknown scheme, and a record written by the previous release all took that
    path, and a learner-facing alias was stored as though it were an evaluator's
    identifier — a well-formed value naming nothing in the answer key, which
    nothing downstream could have caught.
    """
    if aliases.state is AliasState.CANONICAL:
        return lambda value: value
    if not aliases.translates:
        # Nothing is resolvable, so nothing may be accepted. Raised per identifier
        # rather than up front so that the refusal travels the same path as every
        # other contract failure and produces the same generic message.
        def refuse(_value: str) -> str:
            raise ResponseContractError("identifier_unresolvable")

        return refuse

    mapping = aliases.mapping

    def resolve(value: str) -> str:
        try:
            return mapping[value]
        except KeyError:
            raise ResponseContractError("identifier_unresolvable") from None

    return resolve


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


def _progress_payload(progress: SessionProgress) -> dict[str, Any]:
    """A session's own progress value, as the four JSON fields clients read.

    Written out field by field, and coerced to primitives here rather than
    trusted to serialise: the session owns a typed value, and this is the one
    place that decides what it looks like on the wire. A field added to the
    projection later cannot reach a client without being named here.
    """
    return {
        "position": int(progress.position),
        "component_count": int(progress.component_count),
        "answered": int(progress.answered),
        "completed": bool(progress.completed),
    }


def _component_named(
    experience: dict[str, Any], position: int, component_id: str | None
) -> dict[str, Any] | None:
    """The component a session named at its position, if both still agree.

    The session's projection carries both values from one state. Its ordered
    plan was built from this same list, so they normally agree; if the stored
    exercise changed underneath a live session, neither a moved component nor
    whatever replaced it at this position is trusted.
    """
    if component_id is None:
        return None
    component = _component_at(experience, position)
    if component is None or component["component_id"] != component_id:
        return None
    return component


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


def _session_steps(experience: dict[str, Any]) -> tuple[SessionStep, ...]:
    """The ordered walk a session is minted over.

    Built from the *learner-facing* component list, and from exactly two of its
    fields: the identifier the learner was served, and whether the type is one
    whose answer may ever be shown. No payload, no stored evaluator record, and
    no lookup — so a session's plan cannot become a second place where hidden
    content lives, and the reveal contract is decided from what was published
    rather than from what a route remembered to check.
    """
    return tuple(
        SessionStep(
            component_id=component["component_id"],
            reveal_required=component["type"] in REVEALABLE_ANSWER_FIELDS,
        )
        for component in experience["components"]
    )


def _iso(epoch_seconds: float | None) -> str:
    """A session timestamp (from ``deps.clock()``) as an ISO-8601 string.

    Storage stores every other timestamp this way; a session's are the only
    ones kept as epoch floats, because that is what the in-memory clock
    speaks. Converted only at the boundary where they cross into a durable
    row.
    """
    value = epoch_seconds if epoch_seconds is not None else 0.0
    return datetime.fromtimestamp(value, tz=UTC).isoformat()
