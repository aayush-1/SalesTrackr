from fastapi import APIRouter

from .domain import Problem


def manufacturing_recovery_router():
    router = APIRouter()

    @router.api_route("/settings/backup-recovery", methods=["GET", "POST"])
    @router.api_route("/backups", methods=["GET", "POST"])
    @router.api_route("/backups/{path:path}", methods=["GET", "POST"])
    def shared_recovery():
        raise Problem(
            "Back up and restore both Trackrs from the shared Settings page.",
            409, "shared_settings",
        )

    return router
