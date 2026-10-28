import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    mode: str
    data_dir: Path
    root_id: str
    token_json: str
    iap_audience: str
    public_origin: str
    seed_demo: bool
    storage_backend: str = "drive"
    firestore_project: str = ""
    firestore_database: str = "(default)"
    firestore_workspace: str = "salestrackr"
    recovery_admins: tuple[str, ...] = ()
    backup_schedule_ready: bool = False
    backup_folder_id: str = ""
    manufacturing_enabled: bool = False
    manufacturing_project: str = ""
    manufacturing_workspace: str = "producttrackr"
    manufacturing_backup_folder_id: str = ""
    manufacturing_backup_storage: str = "local"
    manufacturing_token_json: str = ""
    manufacturing_database: str = "(default)"
    manufacturing_admins: tuple[str, ...] = ()

    def manufacturing_identity(self, workspace=None):
        return {
            "project": self.manufacturing_project,
            "database": self.manufacturing_database,
            "workspace": workspace or self.manufacturing_workspace,
        }

    def validate_manufacturing(self):
        emulator = os.getenv("FIRESTORE_EMULATOR_HOST", "")
        if self.manufacturing_workspace == self.firestore_workspace:
            raise RuntimeError("ProductTrackr must use a separate workspace from SalesTrackr.")
        if self.mode == "demo":
            if (
                os.getenv("K_SERVICE")
                or not self.manufacturing_project.startswith("demo-")
                or not emulator
                or urlsplit(f"http://{emulator}").hostname not in ("localhost", "127.0.0.1", "::1")
            ):
                raise RuntimeError("Local ProductTrackr requires a demo- project and localhost emulator.")
            return
        if self.mode != "production" or emulator or self.manufacturing_project.startswith("demo-"):
            raise RuntimeError("Production ProductTrackr cannot use demo projects or the Firestore emulator.")
        if not self.manufacturing_project or not self.manufacturing_database:
            raise RuntimeError("Production ProductTrackr requires a Firestore project and database.")
        if self.manufacturing_backup_storage != "drive":
            raise RuntimeError("Production ProductTrackr requires Drive backups; local storage is forbidden.")
        if (
            not self.manufacturing_backup_folder_id
            or self.manufacturing_backup_folder_id in (self.root_id, self.backup_folder_id)
        ):
            raise RuntimeError(
                "Production ProductTrackr requires its own Drive root, separate from SalesTrackr."
            )
        if not self.manufacturing_token_json:
            raise RuntimeError("Production ProductTrackr requires PRODUCTTRACKR_DRIVE_TOKEN_JSON.")
        if not (self.manufacturing_admins or self.recovery_admins):
            raise RuntimeError("Production ProductTrackr requires PRODUCTTRACKR_ADMIN_EMAILS.")

    @classmethod
    def load(cls):
        mode = os.getenv("APP_MODE", "demo")
        if mode not in ("demo", "production"):
            raise RuntimeError("APP_MODE must be demo or production.")
        if os.getenv("K_SERVICE") and mode != "production":
            raise RuntimeError("Demo mode is forbidden on Cloud Run.")
        settings = cls(
            mode=mode,
            data_dir=Path(os.getenv("DEMO_DATA_DIR", ".local-data")),
            root_id=os.getenv("GOOGLE_FOLDER_ID", ""),
            token_json=os.getenv("GOOGLE_TOKEN_JSON", ""),
            iap_audience=os.getenv("IAP_AUDIENCE", ""),
            public_origin=os.getenv("PUBLIC_ORIGIN", "").rstrip("/"),
            seed_demo=os.getenv("DEMO_SEED", "true").lower() == "true",
            storage_backend=os.getenv("STORAGE_BACKEND", "drive"),
            firestore_project=os.getenv("FIRESTORE_PROJECT_ID", ""),
            firestore_database=os.getenv("FIRESTORE_DATABASE_ID", "(default)"),
            firestore_workspace=os.getenv("FIRESTORE_WORKSPACE_ID", "salestrackr"),
            recovery_admins=tuple(
                email.strip().casefold()
                for email in os.getenv("RECOVERY_ADMIN_EMAILS", "").split(",")
                if email.strip()
            ),
            backup_schedule_ready=os.getenv("BACKUP_SCHEDULE_READY", "").lower() == "true",
            backup_folder_id=os.getenv("BACKUP_DRIVE_FOLDER_ID", ""),
            manufacturing_enabled=os.getenv("PRODUCTTRACKR_ENABLED", "").lower() == "true",
            manufacturing_project=os.getenv("PRODUCTTRACKR_FIRESTORE_PROJECT_ID", ""),
            manufacturing_workspace=os.getenv("PRODUCTTRACKR_WORKSPACE_ID", "producttrackr"),
            manufacturing_backup_folder_id=os.getenv("PRODUCTTRACKR_BACKUP_DRIVE_FOLDER_ID", ""),
            manufacturing_backup_storage=os.getenv("PRODUCTTRACKR_BACKUP_STORAGE", "local"),
            manufacturing_token_json=os.getenv("PRODUCTTRACKR_DRIVE_TOKEN_JSON", ""),
            manufacturing_database=os.getenv("PRODUCTTRACKR_FIRESTORE_DATABASE_ID", "(default)"),
            manufacturing_admins=tuple(
                email.strip().casefold()
                for email in os.getenv("PRODUCTTRACKR_ADMIN_EMAILS", "").split(",")
                if email.strip()
            ),
        )
        if settings.manufacturing_enabled:
            settings.validate_manufacturing()
        if settings.manufacturing_backup_storage not in ("local", "drive"):
            raise RuntimeError("PRODUCTTRACKR_BACKUP_STORAGE must be local or drive.")
        if settings.storage_backend not in ("drive", "firestore"):
            raise RuntimeError("STORAGE_BACKEND must be drive or firestore.")
        if settings.storage_backend == "firestore":
            emulator = os.getenv("FIRESTORE_EMULATOR_HOST", "")
            if not settings.firestore_project:
                raise RuntimeError("Firestore requires FIRESTORE_PROJECT_ID.")
            if mode == "demo" and (
                not settings.firestore_project.startswith("demo-")
                or not emulator
                or urlsplit(f"http://{emulator}").hostname not in ("localhost", "127.0.0.1", "::1")
            ):
                raise RuntimeError("Local Firestore requires a demo- project and a localhost emulator.")
            if mode == "production" and emulator:
                raise RuntimeError("The Firestore emulator is forbidden in production.")
            if mode == "production" and not settings.backup_folder_id:
                raise RuntimeError("Firestore requires a separate BACKUP_DRIVE_FOLDER_ID in production.")
        if mode == "production" and not all(
            (
                settings.root_id,
                settings.token_json,
                settings.iap_audience,
                settings.public_origin,
            )
        ):
            raise RuntimeError(
                "Production requires GOOGLE_FOLDER_ID, GOOGLE_TOKEN_JSON, IAP_AUDIENCE, PUBLIC_ORIGIN."
            )
        if mode == "production" and not settings.public_origin.startswith("https://"):
            raise RuntimeError("PUBLIC_ORIGIN must be an HTTPS origin.")
        return settings
