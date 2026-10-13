import hashlib
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from httplib2 import Http, HttpLib2Error
from pydantic import ValidationError

from .domain import MODELS, Problem, Revision, now

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDERS = {"account": "accounts", "contact": "contacts", "record": "sales-records"}


def encode(value: dict | list) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: dict | list) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


class LocalBackend:
    """Local demo only. Production never constructs this backend."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.known_files = set()
        directory.mkdir(parents=True, exist_ok=True)

    def allocate(self):
        return uuid4().hex

    def get(self, file_id: str):
        path = self.directory / f"{file_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def create(self, revision: dict):
        path = self.directory / f"{revision['id']}.json"
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump(revision, output)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            existing = self.get(revision["id"])
            if existing != revision:
                raise Problem("This save identifier is already used.", 409, "write_id_reused") from None

    def initial(self):
        return None, [json.loads(p.read_text(encoding="utf-8")) for p in self.directory.glob("*.json")], None

    def changes(self, cursor):
        # Detect a second demo process or manual edit rather than relying on process-local state.
        revisions = self.initial()[1]
        current = {r["id"] for r in revisions}
        removed = self.known_files - current
        self.known_files = current
        return revisions, None, sorted(removed)

    def checkpoint(self, state):
        return


class DriveBackend:
    def __init__(self, token_json: str, root_id: str):
        info = json.loads(token_json)
        if not info.get("refresh_token"):
            raise RuntimeError("Drive credential must contain a refresh token.")
        self.credentials = Credentials.from_authorized_user_info(info, SCOPES)
        self.root_id = root_id
        self.folders = None
        self.service = None
        self.ignore_checkpoints = False

    def _api(self):
        if self.service is None:
            self.service = build(
                "drive",
                "v3",
                http=AuthorizedHttp(self.credentials, http=Http(timeout=20)),
                cache_discovery=False,
            )
        return self.service

    def _execute(self, request):
        try:
            return request.execute(num_retries=3)
        except (OSError, HttpLib2Error, TransportError):
            raise Problem(
                "Cannot reach Google Drive. Retry after checking connectivity.", 503, "drive_network"
            ) from None
        except RefreshError:
            logger.error("Drive authorization needs renewal")
            raise Problem(
                "Drive needs reconnection. Renew the owner's credential.", 503, "drive_reconnect"
            ) from None
        except HttpError as error:
            status = error.resp.status
            if status == 404:
                raise Problem(
                    "An expected Drive file is missing or inaccessible.", 503, "drive_missing"
                ) from None
            if status == 410:
                raise Problem(
                    "The Drive Changes cursor expired; rebuild the index.", 503, "cursor_expired"
                ) from None
            if status in (401, 403):
                raise Problem(
                    "Drive denied access or its quota was exceeded. "
                    "Check permissions, storage and authorization.",
                    503,
                    "drive_access",
                ) from None
            raise Problem(f"Drive request failed ({status}). Try again.", 503, "drive_unavailable") from None

    def _list(self, query, fields="id,name,parents,appProperties"):
        token = None
        while True:
            result = self._execute(
                self._api()
                .files()
                .list(
                    q=query,
                    fields=f"nextPageToken,files({fields})",
                    pageSize=1000,
                    pageToken=token,
                )
            )
            yield from result.get("files", [])
            token = result.get("nextPageToken")
            if not token:
                break

    def _folders(self):
        if self.folders is None:
            # Root IDs come from operator configuration, never arbitrary request input.
            if not self.root_id.replace("-", "").replace("_", "").isalnum():
                raise Problem("Invalid configured Drive root ID.", 503, "drive_config")
            files = list(
                self._list(f"'{self.root_id}' in parents and name = 'settings.json' and trashed = false")
            )
            if len(files) != 1:
                raise Problem(
                    "Run the Drive bootstrap: exactly one settings.json is required.", 503, "drive_config"
                )
            settings = self.get(files[0]["id"])
            if settings.get("schemaVersion") != 1 or settings.get("rootId") != self.root_id:
                raise Problem("The Drive folder configuration is incompatible.", 503, "drive_config")
            self.folders = settings["folders"]
            for key in (*FOLDERS, "indexes", "backups"):
                metadata = self._execute(
                    self._api()
                    .files()
                    .get(
                        fileId=self.folders[key],
                        fields="id,parents,mimeType,trashed",
                    )
                )
                if (
                    metadata.get("trashed")
                    or self.root_id not in metadata.get("parents", [])
                    or (metadata["mimeType"] != "application/vnd.google-apps.folder")
                ):
                    raise Problem("A configured Drive folder is missing or moved.", 503, "drive_config")
        return self.folders

    def allocate(self):
        return self._execute(self._api().files().generateIds(count=1, space="drive"))["ids"][0]

    def get(self, file_id):
        try:
            content = self._api().files().get_media(fileId=file_id).execute(num_retries=3)
        except (OSError, HttpLib2Error, TransportError):
            raise Problem(
                "Cannot read Google Drive. Check connectivity and retry.", 503, "drive_network"
            ) from None
        except HttpError as error:
            if error.resp.status == 404:
                return None
            raise Problem(
                "Cannot read the saved Drive version. Retry or check access.", 503, "drive_read"
            ) from None
        except RefreshError:
            raise Problem("Drive needs reconnection.", 503, "drive_reconnect") from None
        return json.loads(content)

    def _upload(self, data, parent, filename, file_id=None, properties=None):
        body = {"name": filename, "parents": [parent], "mimeType": "application/json"}
        if file_id:
            body["id"] = file_id
        if properties:
            body["appProperties"] = properties
        payload = encode(data)
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=len(payload) > 5_000_000
        )
        try:
            return self._api().files().create(body=body, media_body=media, fields="id").execute(num_retries=3)
        except (OSError, HttpLib2Error, TransportError):
            raise Problem(
                "Drive save is unconfirmed. Retry without changing this save.", 503, "save_unknown"
            ) from None
        except HttpError as error:
            if error.resp.status == 409 and file_id:
                existing = self.get(file_id)
                if existing == data:
                    return {"id": file_id}
                raise Problem(
                    "Save identifier already exists with different content.", 409, "write_id_reused"
                ) from None
            raise Problem(
                "Drive save was not confirmed. Retry using the same save identifier.", 503, "save_unknown"
            ) from None
        except RefreshError:
            raise Problem(
                "Drive needs reconnection; nothing was confirmed saved.", 503, "drive_reconnect"
            ) from None

    def create(self, revision):
        parent = self._folders()[revision["entityType"]]
        self._upload(
            revision,
            parent,
            f"{revision['entityId']}__{revision['id']}.json",
            revision["id"],
            {"app": "salestrackr", "entityType": revision["entityType"], "entityId": revision["entityId"]},
        )

    def initial(self):
        folders = self._folders()
        checkpoints = list(
            self._list(
                f"'{folders['indexes']}' in parents and trashed = false",
                fields="id,createdTime",
            )
        )
        for file in (
            []
            if self.ignore_checkpoints
            else sorted(
                checkpoints,
                key=lambda f: f["createdTime"],
                reverse=True,
            )
        ):
            try:
                checkpoint = self.get(file["id"])
                state = checkpoint.get("state", {}) if isinstance(checkpoint, dict) else {}
                if (
                    isinstance(checkpoint, dict)
                    and checkpoint.get("format") == 1
                    and checkpoint.get("rootId") == self.root_id
                    and set(state) == {"meta", "heads", "headData", "cursor"}
                    and checkpoint.get("checksum") == digest(state)
                    and isinstance(state["cursor"], str)
                    and all(isinstance(state[k], dict) for k in ("meta", "heads", "headData"))
                ):
                    for head_id, revision in state["headData"].items():
                        parsed = Revision.model_validate(revision)
                        MODELS[parsed.entityType].model_validate(parsed.data)
                        if state["meta"][head_id]["hash"] != digest(revision):
                            raise ValueError("Checkpoint head checksum mismatch")
                    return state, [], state["cursor"]
            except (ValueError, KeyError, TypeError):
                logger.warning("Corrupt derived Drive checkpoint skipped")
            else:
                logger.warning("Incompatible derived Drive checkpoint skipped")
        token = self._execute(self._api().changes().getStartPageToken())["startPageToken"]
        revisions = []
        for key in FOLDERS:
            for file in self._list(f"'{folders[key]}' in parents and trashed = false"):
                content = self.get(file["id"])
                if content is None:
                    raise Problem("A record disappeared during indexing.", 503, "index_incomplete")
                if content.get("id") != file["id"] or content.get("entityType") != key:
                    raise Problem("An unexpected file exists in a records folder.", 503, "index_incomplete")
                revisions.append(content)
        return None, revisions, token

    def changes(self, cursor):
        folders = self._folders()
        allowed = {folders[key] for key in FOLDERS}
        revisions, removed = [], []
        while True:
            result = self._execute(
                self._api()
                .changes()
                .list(
                    pageToken=cursor,
                    pageSize=1000,
                    fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(id,parents,trashed))",
                    includeRemoved=True,
                )
            )
            for change in result.get("changes", []):
                metadata = change.get("file", {})
                if (
                    change.get("removed")
                    or metadata.get("trashed")
                    or not allowed.intersection(metadata.get("parents", []))
                ):
                    removed.append(change["fileId"])
                    continue
                content = self.get(change["fileId"])
                if content is None or content.get("id") != change["fileId"]:
                    raise Problem("A Drive revision is unavailable or altered.", 503, "index_incomplete")
                revisions.append(content)
            if result.get("nextPageToken"):
                cursor = result["nextPageToken"]
            else:
                return revisions, result["newStartPageToken"], removed

    def checkpoint(self, state):
        self._upload(
            {"format": 1, "rootId": self.root_id, "state": state, "checksum": digest(state)},
            self._folders()["indexes"],
            f"checkpoint_{uuid4().hex}.json",
        )


class Repository:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.RLock()
        self.meta = {}
        self.heads = {}
        self.head_data = {}
        self.superseded = set()
        self.cursor = None
        self.initialized = False
        self.synced_at = None
        self.last_sync = 0.0
        self.error = None
        self.pending_checkpoint = 0
        self.rebuilt_cursor = False
        self.missing_files = set()

    def _apply(self, raw):
        revision = Revision.model_validate(raw)
        MODELS[revision.entityType].model_validate(revision.data)
        content_hash = digest(raw)
        old = self.meta.get(revision.id)
        if old:
            if old["hash"] != content_hash:
                raise Problem(
                    "An immutable record was edited in Drive. Restore it before continuing.",
                    503,
                    "data_altered",
                )
            return
        for parent in revision.parents:
            if parent == revision.id:
                raise Problem("A record version cannot reference itself.", 503, "data_altered")
            self.superseded.add(parent)
            self.head_data.pop(parent, None)
        self.meta[revision.id] = {
            "id": revision.id,
            "entityId": revision.entityId,
            "entityType": revision.entityType,
            "parents": revision.parents,
            "createdAt": revision.createdAt,
            "hash": content_hash,
        }
        heads = self.heads.setdefault(revision.entityId, set())
        heads.difference_update(revision.parents)
        if revision.id not in self.superseded:
            heads.add(revision.id)
            self.head_data[revision.id] = raw
        self.pending_checkpoint += 1

    def sync(self, force=False):
        with self.lock:
            if self.initialized and not force and not self.error and time.monotonic() - self.last_sync < 5:
                return
            try:
                if not self.initialized:
                    state, revisions, self.cursor = self.backend.initial()
                    if state:
                        self.meta = state["meta"]
                        self.heads = {k: set(v) for k, v in state["heads"].items()}
                        self.head_data = state["headData"]
                        self.superseded = {p for v in self.meta.values() for p in v["parents"]}
                    for raw in revisions:
                        self._apply(raw)
                    self.initialized = True
                revisions, cursor, removed = self.backend.changes(self.cursor)
                self.missing_files.update(file_id for file_id in removed if file_id in self.meta)
                self.missing_files.difference_update(raw["id"] for raw in revisions)
                if self.missing_files:
                    raise Problem(
                        "An app record was removed or moved in Drive. Restore it to continue.",
                        503,
                        "data_removed",
                    )
                for raw in revisions:
                    self._apply(raw)
                for metadata in self.meta.values():
                    for parent in metadata["parents"]:
                        if (
                            parent not in self.meta
                            or self.meta[parent]["entityId"] != metadata["entityId"]
                            or self.meta[parent]["entityType"] != metadata["entityType"]
                        ):
                            raise Problem(
                                "Record history is incomplete. Refresh or restore missing versions.",
                                503,
                                "index_incomplete",
                            )
                if any(not heads for heads in self.heads.values()):
                    raise Problem(
                        "Record ancestry is invalid; no current version exists.", 503, "data_altered"
                    )
                self.cursor = cursor
                self.synced_at = now()
                self.last_sync = time.monotonic()
                self.error = None
                if self.pending_checkpoint >= 25:
                    self.backend.checkpoint(self.state())
                    self.pending_checkpoint = 0
            except (Problem, ValidationError, ValueError, KeyError, TypeError, OSError) as error:
                if (
                    isinstance(error, Problem)
                    and error.code == "cursor_expired"
                    and isinstance(self.backend, DriveBackend)
                    and not self.rebuilt_cursor
                ):
                    self.rebuilt_cursor = True
                    self.backend.ignore_checkpoints = True
                    self.meta, self.heads, self.head_data, self.superseded = {}, {}, {}, set()
                    self.initialized = False
                    logger.warning("Expired Drive cursor; rebuilding from canonical versions")
                    return self.sync(force=True)
                logger.error("Storage synchronization failed: %s", type(error).__name__)
                self.error = (
                    error.message if isinstance(error, Problem) else "Invalid or unavailable storage data."
                )
                if isinstance(error, Problem):
                    raise
                raise Problem(self.error, 503, "index_incomplete") from None

    def state(self):
        return {
            "meta": self.meta,
            "heads": {k: sorted(v) for k, v in self.heads.items()},
            "headData": self.head_data,
            "cursor": self.cursor,
        }

    def current(self, entity_id, kind=None):
        heads = self.heads.get(entity_id, set())
        if not heads:
            raise Problem("Record not found.", 404, "not_found")
        if len(heads) != 1:
            raise Problem(
                "This record has conflicting saved versions. Resolve it first.", 409, "record_conflict"
            )
        revision = self.head_data[next(iter(heads))]
        if kind and revision["entityType"] != kind:
            raise Problem("Record not found.", 404, "not_found")
        return revision

    def list(self, kind):
        return [
            self.head_data[next(iter(heads))]
            for heads in self.heads.values()
            if len(heads) == 1 and self.head_data[next(iter(heads))]["entityType"] == kind
        ]

    def conflicts(self):
        return [
            {
                "entityId": entity,
                "entityType": self.head_data[next(iter(heads))]["entityType"],
                "versions": [self.head_data[h] for h in sorted(heads)],
            }
            for entity, heads in self.heads.items()
            if len(heads) > 1
        ]

    def prior_write(self, file_id, fingerprint, actor):
        prior = self.backend.get(file_id)
        if prior is not None:
            if prior.get("fingerprint") != fingerprint or prior.get("actor") != actor:
                raise Problem(
                    "Save identifier was already used for a different request.", 409, "write_id_reused"
                )
            self._apply(prior)
            return prior
        return None

    def commit(self, revision):
        self.backend.create(revision)
        self._apply(revision)
        warning = None
        try:
            self.sync(force=True)
        except Problem as error:
            warning = f"Saved, but synchronization is pending: {error.message}"
        return {"revision": revision, "warning": warning}

    def history(self, entity_id):
        entries = sorted(
            (v for v in self.meta.values() if v["entityId"] == entity_id),
            key=lambda v: (v["createdAt"], v["id"]),
            reverse=True,
        )
        return entries
