"""Build the production-only overlay without changing SalesTrackr's runtime settings."""

import json
import re
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "producttrackr.production.json"


def production_environment(project, service, admins="", config=None):
    config = json.loads(CONFIG.read_text(encoding="utf-8")) if config is None else config
    patterns = {
        "database": r"(?:\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])",
        "workspace": r"[a-zA-Z0-9_-]{1,160}",
        "driveRootId": r"[a-zA-Z0-9_-]{10,160}",
        "driveSecret": r"[a-zA-Z0-9_-]+",
        "driveSecretVersion": r"[1-9][0-9]*",
    }
    if set(config) != set(patterns) or any(
        not isinstance(config[key], str) or not re.fullmatch(pattern, config[key])
        for key, pattern in patterns.items()
    ):
        raise ValueError("Invalid ProductTrackr production configuration.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project) or project.startswith("demo-"):
        raise ValueError("Use the existing production Google Cloud project.")
    old = {
        item["name"]: item
        for item in service["spec"]["template"]["spec"]["containers"][0].get("env", [])
    }
    def value(key):
        return old.get(key, {}).get("value", "")
    if value("APP_MODE") != "production" or "FIRESTORE_EMULATOR_HOST" in old:
        raise ValueError("The existing service must be production and must not use a Firestore emulator.")
    if config["driveRootId"] in (value("GOOGLE_FOLDER_ID"), value("BACKUP_DRIVE_FOLDER_ID")):
        raise ValueError("ProductTrackr must not use a SalesTrackr Drive folder.")
    if config["workspace"] == (value("FIRESTORE_WORKSPACE_ID") or "salestrackr"):
        raise ValueError("ProductTrackr must not use the SalesTrackr workspace.")
    admins = admins or value("PRODUCTTRACKR_ADMIN_EMAILS") or value("RECOVERY_ADMIN_EMAILS")
    emails = sorted({email.strip().casefold() for email in admins.split(",") if email.strip()})
    if not emails or any(not re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+", email) for email in emails):
        raise ValueError("Set PRODUCTTRACKR_ADMIN_EMAILS in the production GitHub environment.")
    values = {
        "PRODUCTTRACKR_ENABLED": "true",
        "PRODUCTTRACKR_FIRESTORE_PROJECT_ID": project,
        "PRODUCTTRACKR_FIRESTORE_DATABASE_ID": config["database"],
        "PRODUCTTRACKR_WORKSPACE_ID": config["workspace"],
        "PRODUCTTRACKR_BACKUP_DRIVE_FOLDER_ID": config["driveRootId"],
        "PRODUCTTRACKR_BACKUP_STORAGE": "drive",
        "PRODUCTTRACKR_ADMIN_EMAILS": ",".join(emails),
    }
    for key in (
        "PRODUCTTRACKR_FIRESTORE_PROJECT_ID", "PRODUCTTRACKR_FIRESTORE_DATABASE_ID",
        "PRODUCTTRACKR_WORKSPACE_ID", "PRODUCTTRACKR_BACKUP_DRIVE_FOLDER_ID",
    ):
        if value("PRODUCTTRACKR_ENABLED") == "true" and value(key) and value(key) != values[key]:
            raise ValueError(
                "An existing ProductTrackr storage identity changed; review its migration first."
            )
    return {
        **{key: {"name": key, "value": val} for key, val in values.items()},
        "PRODUCTTRACKR_DRIVE_TOKEN_JSON": {
            "name": "PRODUCTTRACKR_DRIVE_TOKEN_JSON",
            "valueFrom": {"secretKeyRef": {
                "name": config["driveSecret"], "key": config["driveSecretVersion"],
            }},
        },
    }
