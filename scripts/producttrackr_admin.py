"""Verify the production workspace and Drive connection without deploying the application."""

import argparse
import base64
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import requests
from google.cloud import firestore
from google.oauth2.credentials import Credentials

from backend.app.config import Settings
from backend.app.firestore_storage import FirestoreRepository
from backend.app.manufacturing_backups import ManufacturingDriveBackupStore
from backend.app.manufacturing_storage import ManufacturingRepository
from backend.app.shared_backups import SharedBackupManager
from scripts.producttrackr_deploy import CONFIG, production_environment


def cloud(*arguments):
    executable = shutil.which("gcloud.cmd") or shutil.which("gcloud")
    if not executable:
        raise RuntimeError("Install and sign in to the Google Cloud CLI first.")
    return subprocess.check_output([executable, *arguments], text=True).strip()


def database_rules(session, project, database):
    session.headers.update({"x-goog-user-project": project})
    base = f"https://firebaserules.googleapis.com/v1/projects/{project}"
    suffix = "" if database == "(default)" else f"/{database}"
    release = f"{base}/releases/cloud.firestore{suffix}"
    source = (Path(__file__).resolve().parents[1] / "firestore.rules").read_text(encoding="utf-8")
    current = session.get(release, timeout=30)
    if current.status_code == 404:
        ruleset = session.post(
            f"{base}/rulesets",
            json={"source": {"files": [{"name": "firestore.rules", "content": source}]}}, timeout=30,
        )
        ruleset.raise_for_status()
        created = session.post(
            f"{base}/releases",
            json={
                "name": f"projects/{project}/releases/cloud.firestore{suffix}",
                "rulesetName": ruleset.json()["name"],
            }, timeout=30,
        )
        created.raise_for_status()
        current = session.get(release, timeout=30)
    current.raise_for_status()
    ruleset = session.get(
        f"https://firebaserules.googleapis.com/v1/{current.json()['rulesetName']}", timeout=30,
    )
    ruleset.raise_for_status()
    if ruleset.json()["source"]["files"] != [{"name": "firestore.rules", "content": source}]:
        # An existing policy is never silently replaced by the production setup utility.
        files = ruleset.json()["source"]["files"]
        if len(files) != 1 or files[0].get("content") != source:
            raise RuntimeError("The database rules differ; review them before deployment.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--service", default="salestrackr")
    parser.add_argument(
        "--backup", action="store_true", help="Create and read back a real production backup.",
    )
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    service = json.loads(cloud(
        "run", "services", "describe", args.service, f"--project={args.project}",
        f"--region={args.region}", "--format=json",
    ))
    if service["metadata"]["annotations"].get("run.googleapis.com/iap-enabled") != "true":
        raise RuntimeError("The existing service must keep IAP enabled.")
    environment = production_environment(args.project, service)
    values = {row["name"]: row.get("value", "") for row in
              service["spec"]["template"]["spec"]["containers"][0]["env"]}
    access_token = cloud("auth", "print-access-token", "--quiet")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {access_token}"
    database_rules(session, args.project, config["database"])
    secret_ref = next(row["valueFrom"]["secretKeyRef"] for row in
                      service["spec"]["template"]["spec"]["containers"][0]["env"]
                      if row["name"] == "GOOGLE_TOKEN_JSON")
    secret = session.get(
        f"https://secretmanager.googleapis.com/v1/projects/{args.project}/secrets/"
        f"{secret_ref['name']}/versions/{secret_ref['key']}:access", timeout=30,
    )
    secret.raise_for_status()
    token = base64.b64decode(secret.json()["payload"]["data"]).decode()
    settings = Settings(
        "production", Path(".local-data"), values["GOOGLE_FOLDER_ID"], token,
        values["IAP_AUDIENCE"], values["PUBLIC_ORIGIN"], False,
        storage_backend="firestore",
        firestore_project=values.get("FIRESTORE_PROJECT_ID", ""),
        firestore_database=values.get("FIRESTORE_DATABASE_ID", "(default)"),
        firestore_workspace=values.get("FIRESTORE_WORKSPACE_ID", "salestrackr"),
        backup_folder_id=values.get("BACKUP_DRIVE_FOLDER_ID", ""),
        manufacturing_enabled=True, manufacturing_project=args.project,
        manufacturing_database=config["database"], manufacturing_workspace=config["workspace"],
        manufacturing_backup_storage="drive",
        recovery_admins=tuple(values.get("RECOVERY_ADMIN_EMAILS", "").split(",")),
        manufacturing_admins=tuple(environment["PRODUCTTRACKR_ADMIN_EMAILS"]["value"].split(",")),
    )
    settings.validate_manufacturing()
    client = firestore.Client(
        project=args.project, database=config["database"],
        credentials=Credentials(token=access_token, quota_project_id=args.project),
    )
    repository = ManufacturingRepository(settings, client=client)
    data, _ = repository.snapshot()
    for kind in ("products", "operations", "suppliers", "contacts"):
        repository.list(kind, "readiness", supplier_id="readiness-supplier" if kind == "contacts" else None)
    manager = SharedBackupManager(FirestoreRepository(settings, client=client), settings)
    files = ManufacturingDriveBackupStore(settings)
    root = files._root_folder()
    products = files.products_folder()
    backups = manager.store._folder()
    result = {
        "database": config["database"], **repository.meta(),
        "counts": {kind: len(rows) for kind, rows in data.items() if isinstance(rows, dict)},
        "rootVerified": root == settings.root_id, "productsFolderId": products,
        "backupsFolderId": backups, "sharedDriveSecretVerified": True, "browserDatabaseAccess": "denied",
    }
    anonymous = requests.get(
        f"https://firestore.googleapis.com/v1/projects/{args.project}/databases/{config['database']}"
        f"/documents/workspaces/{config['workspace']}", timeout=30,
    )
    if anonymous.status_code not in (401, 403):
        raise RuntimeError("Unauthenticated database access was not denied.")
    if args.backup:
        row = manager.backup(uuid4().hex, "production-setup")
        content, _ = manager.get(row["id"])
        restored, _ = manager.read_archive(content)
        if restored["manufacturing"] != data:
            raise RuntimeError("The verified backup does not match the production snapshot.")
        result["verifiedBackupId"] = row["id"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
