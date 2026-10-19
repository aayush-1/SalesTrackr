"""Authorize locally and create a private, app-owned CRM folder."""

import argparse
import json
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

from backend.app.storage import FOLDERS, SCOPES, DriveBackend


def create_root(backend, name):
    api = backend._api()
    root = backend._execute(
        api.files().create(
            body={
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "appProperties": {"app": "salestrackr"},
            },
            fields="id",
        )
    )["id"]
    folders = {}
    for key, name in {**FOLDERS, "indexes": "indexes", "backups": "backups"}.items():
        folders[key] = backend._execute(
            api.files().create(
                body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [root]},
                fields="id",
            )
        )["id"]
    backend._upload(
        {"schemaVersion": 2, "rootId": root, "folders": folders, "checkpointId": backend.allocate()},
        root,
        "settings.json",
    )
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials", required=True, type=Path, help="Downloaded Desktop OAuth client JSON"
    )
    parser.add_argument(
        "--token-out", required=True, type=Path, help="Protected output outside source control"
    )
    parser.add_argument("--name", default="SalesTrackr")
    parser.add_argument(
        "--reconnect", action="store_true", help="Only renew credentials; do not create a folder"
    )
    args = parser.parse_args()
    if args.token_out.exists():
        parser.error("Choose a new token output file; existing credentials will not be overwritten.")
    flow = InstalledAppFlow.from_client_secrets_file(str(args.credentials), SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise RuntimeError("No refresh token was issued; offline access is required.")
    credentials.refresh(Request())
    args.token_out.parent.mkdir(parents=True, exist_ok=True)
    with args.token_out.open("x", encoding="utf-8") as output:
        output.write(credentials.to_json())
    if not args.reconnect:
        backend = DriveBackend(credentials.to_json(), "")
        root = create_root(backend, args.name)
        print(json.dumps({"GOOGLE_FOLDER_ID": root}))
    print("Credential saved to the specified output. Upload it to Secret Manager; never commit it.")


if __name__ == "__main__":
    main()
