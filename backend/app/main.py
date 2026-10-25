import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .auth import authenticate, protect_mutation
from .config import Settings
from .demo import seed
from .domain import (
    MODELS,
    AccountData,
    ContactData,
    DiscardChange,
    FeedbackChange,
    FollowUp,
    NewRecord,
    PaymentChange,
    Prepare,
    Problem,
    ProductNotesChange,
    RecordData,
    RecordFields,
    Resolution,
    Revision,
    StageChange,
    Write,
    add_followup,
    discard_record,
    feedback_change,
    now,
    payment_change,
    stage_change,
    today,
)
from .queries import VIEWS, account_matches, account_name_key, matches, query_records
from .storage import DriveBackend, LocalBackend, Repository, digest
from .timing import save_timing, timed_lock, timed_phase

logger = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(settings: Settings | None = None, repository: Repository | None = None):
    settings = settings or Settings.load()
    repo = repository or Repository(
        LocalBackend(settings.data_dir)
        if settings.mode == "demo"
        else DriveBackend(settings.token_json, settings.root_id)
    )

    @asynccontextmanager
    async def lifespan(app):
        if settings.mode == "demo":
            repo.sync(force=True)
            if settings.seed_demo:
                seed(repo)
        yield

    app = FastAPI(title="SalesTrackr API", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.repo = repo

    @app.exception_handler(Problem)
    async def problem_handler(request, error):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}}, status_code=error.status
        )

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_handler(request, error):
        details = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in error.errors()]
        return JSONResponse(
            {
                "error": {
                    "code": "validation",
                    "message": "; ".join(e["message"] for e in details),
                    "fields": details,
                }
            },
            status_code=422,
        )

    @app.middleware("http")
    async def access(request: Request, call_next):
        try:
            if request.url.path != "/healthz":
                request.state.actor = await run_in_threadpool(authenticate, request, settings)
                protect_mutation(request, settings)
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    body = await request.body()
                    if len(body) > 128_000:
                        raise Problem("Request is too large.", 413, "request_size")
            response = await call_next(request)
        except Problem as error:
            response = await problem_handler(request, error)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def metadata():
        conflicts = len(repo.conflicts())
        return {
            "syncedAt": repo.synced_at,
            "conflictCount": conflicts,
            "complete": not conflicts and not repo.error,
        }

    def validate_references(kind, data):
        if kind in ("record", "contact"):
            repo.current(data["accountId"], "account")
        if kind == "record" and data.get("contactId"):
            contact = repo.current(data["contactId"], "contact")
            if contact["data"]["accountId"] != data["accountId"]:
                raise Problem("Choose a contact belonging to this account.")

    def record_relations(data):
        related = {"account": None, "contact": None, "errors": {}}
        for kind in ("account", "contact"):
            entity_id = data.get(f"{kind}Id")
            if entity_id:
                try:
                    related[kind] = repo.current(entity_id, kind)
                except Problem as error:
                    related["errors"][kind] = error.message
        return related

    def saved_response(result):
        revision = result["revision"]
        return {
            **result,
            **metadata(),
            "revisionIsCurrent": repo.heads.get(revision["entityId"]) == {revision["id"]},
            **({"related": record_relations(revision["data"])} if revision["entityType"] == "record" else {}),
        }

    def mutate(kind, write, action, request, transform, create=False):
        with save_timing(kind, action), timed_lock(repo.lock):
            with timed_phase("syncBeforeMs"):
                repo.sync(force=True)
            fingerprint = digest({"kind": kind, "action": action, "write": write.model_dump(mode="json")})
            prior = repo.prior_write(write.writeId, fingerprint, request.state.actor, write.entityId)
            if prior:
                return saved_response({"revision": prior, "warning": None})
            old = None if create else repo.current(write.entityId, kind)
            if create and (write.baseRevision is not None or write.entityId in repo.heads):
                raise Problem("That record already exists.", 409, "record_exists")
            if old and write.baseRevision != old["id"]:
                raise Problem("This record changed. Refresh before editing.", 409, "stale_edit")
            if kind == "record" and old and old["data"].get("discarded", False):
                raise Problem("Discarded sales records are read-only.", 409, "discarded_record")
            data = transform(old["data"] if old else None).model_dump(mode="json")
            validate_references(kind, data)
            if kind == "account" and create:
                name_key = account_name_key(data["name"])
                if any(
                    row["entityType"] == "account"
                    and account_name_key(row["data"]["name"]) == name_key
                    for row in repo.head_data.values()
                ):
                    raise Problem(
                        "An account with this company name already exists. "
                        "Use the existing account instead of creating a duplicate.",
                        409, "account_exists",
                    )
            if kind == "contact" and old and old["data"]["accountId"] != data["accountId"]:
                raise Problem("Contacts cannot be reassigned to another account.")
            if kind == "record" and old and old["data"]["accountId"] != data["accountId"]:
                raise Problem("A sales record cannot be reassigned to another account.")
            revision = Revision(
                id=write.writeId,
                entityId=write.entityId,
                entityType=kind,
                parents=[old["id"]] if old else [],
                createdAt=now(),
                actor=request.state.actor,
                action=action,
                reason=getattr(write.data, "reason", ""),
                fingerprint=fingerprint,
                data=data,
            )
            result = repo.commit(revision.model_dump(mode="json"))
            if kind == "account":
                duplicates = [
                    r
                    for r in repo.list("account")
                    if r["entityId"] != write.entityId
                    and account_name_key(r["data"]["name"]) == account_name_key(data["name"])
                ]
                if duplicates:
                    result["warning"] = "Saved. Another account has the same name; check for duplicates."
            return saved_response(result)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/me")
    def me(request: Request):
        return {"email": request.state.actor, "mode": settings.mode, "today": today().isoformat()}

    @app.get("/api/status")
    def status():
        with repo.lock:
            try:
                repo.sync(force=True)
            except Problem as error:
                return {"mode": settings.mode, **metadata(), "error": error.message}
            return {"mode": settings.mode, **metadata(), "error": None}

    @app.post("/api/writes/prepare")
    def prepare(body: Prepare):
        with save_timing(body.entityType, "prepare"), timed_lock(repo.lock):
            with timed_phase("syncBeforeMs"):
                repo.sync()
            if body.entityId:
                # Preparation is also permitted for a record awaiting conflict resolution.
                if body.entityId not in repo.heads:
                    raise Problem("Record not found.", 404, "not_found")
            return {"writeId": uuid4().hex, "entityId": body.entityId or repo.backend.allocate()}

    @app.get("/api/accounts")
    def accounts(search: str = "", offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        with repo.lock:
            repo.sync()
            rows = sorted(
                (r for r in repo.list("account") if search.casefold() in r["data"]["name"].casefold()),
                key=lambda r: r["data"]["name"].casefold(),
            )
            return {"items": rows[offset : offset + limit], "total": len(rows), **metadata()}

    @app.post("/api/accounts")
    def create_account(body: Write[AccountData], request: Request):
        return mutate("account", body, "create_account", request, lambda _: body.data, create=True)

    @app.get("/api/accounts/matches")
    def matching_accounts(name: str = Query("", max_length=200), exclude: str | None = None):
        with repo.lock:
            repo.sync()
            return {
                "name": name.strip(),
                **account_matches(repo.head_data.values(), name.strip(), exclude=exclude),
                **metadata(),
            }

    @app.patch("/api/accounts/{entity_id}")
    def edit_account(entity_id: str, body: Write[AccountData], request: Request):
        require_id(entity_id, body.entityId)
        return mutate("account", body, "edit_account", request, lambda _: body.data)

    @app.get("/api/contacts")
    def contacts(accountId: str):
        with repo.lock:
            repo.sync()
            return {
                "items": [r for r in repo.list("contact") if r["data"]["accountId"] == accountId],
                **metadata(),
            }

    @app.post("/api/contacts")
    def create_contact(body: Write[ContactData], request: Request):
        return mutate("contact", body, "create_contact", request, lambda _: body.data, create=True)

    @app.patch("/api/contacts/{entity_id}")
    def edit_contact(entity_id: str, body: Write[ContactData], request: Request):
        require_id(entity_id, body.entityId)
        return mutate("contact", body, "edit_contact", request, lambda _: body.data)

    @app.get("/api/records")
    def records(
        view: str = "all",
        search: str = "",
        accountId: str | None = None,
        stage: str | None = None,
        dateBasis: str | None = None,
        start: date | None = None,
        end: date | None = None,
        due: str = "all",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
    ):
        with repo.lock:
            repo.sync()
            account_data = {r["entityId"]: r["data"] for r in repo.list("account")}
            return {
                **query_records(
                    repo.list("record"),
                    account_data,
                    view,
                    search,
                    accountId,
                    stage,
                    dateBasis,
                    start,
                    end,
                    due,
                    offset,
                    limit,
                ),
                **metadata(),
            }

    @app.post("/api/records")
    def create_record(body: Write[NewRecord], request: Request):
        def transform(_):
            values = body.data.model_dump(exclude={"acquiredOn"})
            return RecordData(
                **values, workflowVersion=3, stageDates={"new_lead": body.data.acquiredOn or today()}
            )

        return mutate("record", body, "create_record", request, transform, create=True)

    @app.get("/api/records/{entity_id}/detail")
    def record_detail(entity_id: str):
        with repo.lock:
            repo.sync(force=True)
            revision = repo.current(entity_id, "record")
            return {"revision": revision, "related": record_relations(revision["data"]), **metadata()}

    @app.patch("/api/records/{entity_id}")
    def edit_record(entity_id: str, body: Write[RecordFields], request: Request):
        require_id(entity_id, body.entityId)
        return mutate(
            "record",
            body,
            "edit_record",
            request,
            lambda old: RecordData.model_validate(
                {
                    **old,
                    **body.data.model_dump(),
                }
            ),
        )

    @app.patch("/api/records/{entity_id}/product-notes")
    def edit_product_notes(entity_id: str, body: Write[ProductNotesChange], request: Request):
        require_id(entity_id, body.entityId)
        return mutate(
            "record",
            body,
            "edit_product_notes",
            request,
            lambda old: RecordData.model_validate({**old, "productNotes": body.data.productNotes}),
        )

    @app.post("/api/records/{entity_id}/discard")
    def discard(entity_id: str, body: Write[DiscardChange], request: Request):
        require_id(entity_id, body.entityId)
        return mutate(
            "record",
            body,
            "discard_record",
            request,
            lambda old: discard_record(RecordData.model_validate(old), body.data),
        )

    @app.post("/api/records/{entity_id}/stage")
    def stage(entity_id: str, body: Write[StageChange], request: Request):
        require_id(entity_id, body.entityId)
        return mutate(
            "record",
            body,
            "stage",
            request,
            lambda old: stage_change(RecordData.model_validate(old), body.data),
        )

    @app.post("/api/records/{entity_id}/payment")
    def payment(entity_id: str, body: Write[PaymentChange], request: Request):
        require_id(entity_id, body.entityId)
        return mutate(
            "record",
            body,
            "payment",
            request,
            lambda old: payment_change(RecordData.model_validate(old), body.data),
        )

    @app.post("/api/records/{entity_id}/followups")
    def followup(entity_id: str, body: Write[FollowUp], request: Request):
        require_id(entity_id, body.entityId)
        return mutate(
            "record",
            body,
            "followup",
            request,
            lambda old: add_followup(RecordData.model_validate(old), body.data),
        )

    @app.post("/api/records/{entity_id}/feedback")
    def feedback(entity_id: str, body: Write[FeedbackChange], request: Request):
        require_id(entity_id, body.entityId)
        return mutate(
            "record",
            body,
            "feedback",
            request,
            lambda old: feedback_change(RecordData.model_validate(old), body.data),
        )

    @app.get("/api/dashboard")
    def dashboard():
        with repo.lock:
            repo.sync()
            rows = repo.list("record")
            counts = {view: sum(matches(r["data"], view) for r in rows) for view in VIEWS}
            return {"counts": counts, **metadata()}

    @app.get("/api/conflicts")
    def conflicts():
        with repo.lock:
            repo.sync(force=True)
            return {"items": repo.conflicts(), **metadata()}

    @app.post("/api/entities/{kind}/{entity_id}/resolve")
    def resolve(kind: str, entity_id: str, body: Resolution, request: Request):
        require_kind(kind)
        with save_timing(kind, "resolve_conflict"), timed_lock(repo.lock):
            with timed_phase("syncBeforeMs"):
                repo.sync(force=True)
            fingerprint = digest(
                {"kind": kind, "entityId": entity_id, "resolution": body.model_dump(mode="json")}
            )
            prior = repo.prior_write(body.writeId, fingerprint, request.state.actor, entity_id)
            if prior:
                return {"revision": prior, "warning": None, **metadata()}
            heads = repo.heads.get(entity_id, set())
            if heads != set(body.parents) or len(heads) < 2 or body.selectedRevision not in heads:
                raise Problem(
                    "The conflicting versions changed. Refresh and review them again.", 409, "stale_edit"
                )
            chosen = repo.head_data[body.selectedRevision]
            if chosen["entityType"] != kind:
                raise Problem("Wrong entity type.")
            data = MODELS[kind].model_validate(chosen["data"]).model_dump(mode="json")
            validate_references(kind, data)
            revision = Revision(
                id=body.writeId,
                entityId=entity_id,
                entityType=kind,
                parents=sorted(heads),
                createdAt=now(),
                actor=request.state.actor,
                action="resolve_conflict",
                reason=body.reason,
                fingerprint=fingerprint,
                data=data,
            )
            return {**repo.commit(revision.model_dump(mode="json")), **metadata()}

    @app.get("/api/entities/{kind}/{entity_id}")
    def detail(kind: str, entity_id: str):
        require_kind(kind)
        with repo.lock:
            repo.sync(force=True)
            return {"revision": repo.current(entity_id, kind), **metadata()}

    @app.get("/api/entities/{kind}/{entity_id}/history")
    def history(kind: str, entity_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
        require_kind(kind)
        with repo.lock:
            repo.sync()
            entries = repo.history(entity_id)
            if not entries or entries[0]["entityType"] != kind:
                raise Problem("Record not found.", 404, "not_found")
            saved = {raw["id"]: raw for raw in repo.read_history(entity_id)}
            versions = [saved[entry["id"]] for entry in entries[offset : offset + limit]]
            return {"items": versions, "total": len(entries)}

    @app.get("/{path:path}")
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise Problem("API endpoint not found.", 404, "not_found")
        file = (STATIC / path).resolve()
        if not file.is_relative_to(STATIC.resolve()):
            raise Problem("Not found.", 404, "not_found")
        if file.is_file():
            return FileResponse(file)
        if "." in Path(path).name:
            raise Problem("Asset not found.", 404, "not_found")
        if not (STATIC / "index.html").exists():
            raise Problem(
                "UI not built. Run npm --prefix frontend run build, or use the Vite dev URL.",
                503,
                "ui_missing",
            )
        return FileResponse(STATIC / "index.html")

    return app


def require_id(path_id, body_id):
    if path_id != body_id:
        raise Problem("Path and record ID do not match.")


def require_kind(kind):
    if kind not in MODELS:
        raise Problem("Unknown entity type.", 404, "not_found")
