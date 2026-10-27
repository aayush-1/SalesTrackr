from threading import Lock

from fastapi import APIRouter, Query, Request

from .domain import Identifier, Problem
from .manufacturing_cleanup import manufacturing_cleanup_router
from .manufacturing_domain import Command, Directory
from .manufacturing_records import manufacturing_records_router
from .manufacturing_recovery_api import manufacturing_recovery_router
from .manufacturing_spreadsheet import manufacturing_spreadsheet_router
from .manufacturing_storage import ManufacturingRepository


def manufacturing_router(settings):
    router = APIRouter(prefix="/api/manufacturing")
    repository = None
    lock = Lock()

    def repo():
        nonlocal repository
        if not settings.manufacturing_enabled:
            raise Problem(
                "BuildTrackr storage is not configured. Enable the isolated local manufacturing preview.",
                503, "manufacturing_unavailable",
            )
        with lock:
            if repository is None:
                repository = ManufacturingRepository(settings)
        return repository.bind()

    def mutate(request, action, target, body):
        return repo().mutate(
            action, target, body, request.state.actor,
            request.headers.get("x-buildtrackr-workspace"),
            request.headers.get("x-buildtrackr-generation"),
        )

    router.include_router(manufacturing_records_router(repo))
    router.include_router(manufacturing_recovery_router(repo, settings))
    router.include_router(manufacturing_cleanup_router(repo, settings))
    router.include_router(manufacturing_spreadsheet_router(repo, settings))

    @router.get("/meta")
    def metadata():
        return repo().meta()

    @router.get("/{kind}")
    def listing(
        kind: Directory, search: str = Query("", max_length=200), partnerId: Identifier | None = None,
        offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
    ):
        if partnerId and kind != "contacts":
            raise Problem("The partner filter is only supported for contacts.")
        return repo().list(kind, search, partnerId, offset, limit)

    @router.get("/{kind}/{entity_id}")
    def detail(kind: Directory, entity_id: Identifier):
        return repo().detail(kind, entity_id)

    @router.post("/{kind}")
    def create(kind: Directory, body: Command, request: Request):
        if body.baseVersion is not None:
            raise Problem("New records cannot have a base version.")
        return mutate(request, f"create:{kind}", body.id, body)

    @router.post("/products/{product_id}/steps")
    def add_step(product_id: Identifier, body: Command, request: Request):
        return mutate(request, "add-step", product_id, body)

    @router.post("/products/{product_id}/reorder")
    def reorder(product_id: Identifier, body: Command, request: Request):
        return mutate(request, "reorder", product_id, body)

    @router.put("/steps/{step_id}")
    def edit_step(step_id: Identifier, body: Command, request: Request):
        if body.id != step_id:
            raise Problem("The step identifier does not match.")
        return mutate(request, "edit-step", step_id, body)

    @router.post("/steps/{step_id}/assignments")
    def assign(step_id: Identifier, body: Command, request: Request):
        return mutate(request, "assign", step_id, body)

    @router.put("/steps/{step_id}/assignments/{assignment_id}")
    def edit_assignment(step_id: Identifier, assignment_id: Identifier, body: Command, request: Request):
        if body.id != assignment_id:
            raise Problem("The assignment identifier does not match.")
        return mutate(request, "edit-assignment", step_id, body)

    @router.post("/steps/{step_id}/primary")
    def primary(step_id: Identifier, body: Command, request: Request):
        return mutate(request, "primary", step_id, body)

    @router.post("/steps/{step_id}/assignments/{assignment_id}/remove")
    def remove(step_id: Identifier, assignment_id: Identifier, body: Command, request: Request):
        if body.id != assignment_id:
            raise Problem("The assignment identifier does not match.")
        return mutate(request, "remove", step_id, body)

    @router.put("/{kind}/{entity_id}")
    def edit(kind: Directory, entity_id: Identifier, body: Command, request: Request):
        if body.id != entity_id:
            raise Problem("The record identifier does not match this page.")
        return mutate(request, f"edit:{kind}", entity_id, body)

    return router
