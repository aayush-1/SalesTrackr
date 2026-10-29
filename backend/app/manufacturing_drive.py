"""Idempotent, private subfolders beneath the existing manufacturing Drive root."""

from .backups import identifier
from .domain import Problem

FOLDER_MIME = "application/vnd.google-apps.folder"


def require_private(backend, folder):
    token = None
    while True:
        page = backend._execute(backend._api().permissions().list(
            fileId=folder, fields="nextPageToken,permissions(type)", pageSize=100, pageToken=token,
        ))
        if any(row["type"] in ("anyone", "domain") for row in page["permissions"]):
            raise Problem("Google Drive folders must remain private.", 503, "backup_permissions")
        token = page.get("nextPageToken")
        if not token:
            return


def child_folder(backend, parent, kind):
    name = {"products": "Products", "backups": "Backups"}[kind]
    app, key = f"producttrackr-{kind}", f"producttrackr_{kind}"
    for _ in range(3):
        etag = backend.etag(parent)
        metadata = backend._execute(backend._api().files().get(
            fileId=parent, fields="id,appProperties,properties",
        ))
        private, public = metadata.get("appProperties", {}), metadata.get("properties", {})
        folder = private.get(key)
        if folder:
            folder = identifier(folder)
            break
        matches = list(backend._list(
            f"'{parent}' in parents and mimeType = '{FOLDER_MIME}' "
            f"and appProperties has {{ key='app' and value='{app}' }}", fields="id",
        ))
        if len(matches) > 1:
            raise Problem(f"More than one managed {name} folder exists.", 409, "drive_layout")
        folder = matches[0]["id"] if matches else backend.allocate()
        properties = [
            {"key": k, "value": v, "visibility": visibility}
            for values, visibility in ((public, "PUBLIC"), ({**private, key: folder}, "PRIVATE"))
            for k, v in values.items()
        ]
        request = backend._conditional_api().files().update(
            fileId=parent, body={"properties": properties}, fields="id",
        )
        request.headers["If-Match"] = etag
        try:
            backend._execute(request)
            break
        except Problem as error:
            if error.code != "stale_edit":
                raise
    else:
        raise Problem("Drive folder setup changed. Retry shortly.", 409, "drive_layout")

    def read():
        return backend._execute(backend._api().files().get(
            fileId=folder, fields="id,parents,mimeType,trashed,appProperties",
        ))

    try:
        metadata = read()
    except Problem as error:
        if error.code != "drive_missing":
            raise
        try:
            backend._execute(backend._api().files().create(
                body={
                    "id": folder, "name": name, "parents": [parent],
                    "mimeType": FOLDER_MIME, "appProperties": {"app": app},
                }, fields="id",
            ))
        except Problem as creation_error:
            try:
                metadata = read()
            except Problem:
                raise creation_error from None
        else:
            metadata = read()
    if (
        metadata.get("trashed") or metadata.get("parents") != [parent]
        or metadata.get("mimeType") != FOLDER_MIME
        or metadata.get("appProperties", {}).get("app") != app
    ):
        raise Problem(f"The managed {name} folder was moved or replaced.", 409, "drive_layout")
    require_private(backend, folder)
    return folder


def relocate(backend, file_id, source, target, mime, properties):
    etag = backend.etag(file_id)
    metadata = backend._execute(backend._api().files().get(
        fileId=file_id, fields="id,parents,mimeType,trashed,appProperties",
    ))
    if (
        metadata.get("trashed") or metadata.get("mimeType") != mime
        or any(metadata.get("appProperties", {}).get(key) != value for key, value in properties.items())
        or metadata.get("parents") not in ([source], [target])
    ):
        raise Problem("A managed Drive item was moved or replaced.", 409, "drive_layout")
    if metadata["parents"] != [target]:
        request = backend._conditional_api().files().update(
            fileId=file_id, addParents=target, removeParents=source, body={}, fields="id",
        )
        request.headers["If-Match"] = etag
        backend._execute(request)
