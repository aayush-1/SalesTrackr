from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from .domain import Identifier, Model, Problem
from .manufacturing_backups import ManufacturingBackupManager


class BackupWrite(Model):
    writeId: Identifier


def manufacturing_recovery_router(repository, settings):
    router = APIRouter()

    def manager():
        return ManufacturingBackupManager(repository(), settings)

    def fence(request, recovery):
        if (
            request.headers.get("x-buildtrackr-workspace") != recovery.repo.workspace.id
            or request.headers.get("x-buildtrackr-generation") != recovery.repo.control["generation"]
        ):
            raise Problem("BuildTrackr changed. Reload before starting recovery.", 409, "workspace_changed")

    @router.get("/settings/backup-recovery")
    def status(request: Request):
        return manager().status(request.state.actor)

    @router.post("/backups")
    def create(body: BackupWrite, request: Request):
        recovery = manager()
        fence(request, recovery)
        return {"backup": recovery.backup(body.writeId, request.state.actor)}

    @router.get("/backups/{backup_id}/download")
    def download(backup_id: Identifier):
        content, _ = manager().get(backup_id)
        return Response(
            content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="BuildTrackr-{backup_id}.zip"'},
        )

    async def input_data(request, recovery):
        recovery.require_admin(request.state.actor)
        if request.headers.get("content-type", "").split(";")[0] == "application/zip":
            return await request.body(), None, {}
        try:
            body = await request.json()
            content, row = await run_in_threadpool(recovery.get, body.get("backupId"))
            return content, row, body
        except (ValueError, AttributeError, TypeError):
            raise Problem(
                "Select a BuildTrackr backup or upload its ZIP archive.", 422, "invalid_backup"
            ) from None

    @router.post("/backups/inspect")
    async def inspect(request: Request):
        recovery = manager()
        fence(request, recovery)
        content, row, _ = await input_data(request, recovery)
        return await run_in_threadpool(recovery.inspect, content, request.state.actor, row)

    @router.post("/backups/restore")
    async def restore(request: Request):
        recovery = manager()
        if request.headers.get("x-buildtrackr-workspace") != recovery.repo.workspace.id:
            raise Problem("BuildTrackr changed. Reload before starting recovery.", 409, "workspace_changed")
        upload = request.headers.get("content-type", "").split(";")[0] == "application/zip"
        if upload:
            content, _, body = await input_data(request, recovery)
            result = await run_in_threadpool(
                recovery.completed_upload_restore, content, request.state.actor,
                request.headers.get("x-buildtrackr-restore-confirm", ""),
                request.headers.get("x-buildtrackr-write-id"),
            )
            if result:
                return result
        else:
            try:
                body = await request.json()
                result = await run_in_threadpool(
                    recovery.completed_restore,
                    body.get("backupId"),
                    body.get("writeId"),
                    request.state.actor,
                    body.get("confirmation"),
                )
                if result:
                    return result
            except (ValueError, AttributeError, TypeError):
                raise Problem(
                    "Select a BuildTrackr backup before restoring.", 422, "invalid_backup"
                ) from None
        fence(request, recovery)
        if not upload:
            content, _, body = await input_data(request, recovery)
        return await run_in_threadpool(
            recovery.restore,
            content,
            request.state.actor,
            body.get("previewToken") or request.headers.get("x-buildtrackr-restore-token", ""),
            body.get("confirmation") or request.headers.get("x-buildtrackr-restore-confirm", ""),
            body.get("writeId") or request.headers.get("x-buildtrackr-write-id"),
        )

    return router
