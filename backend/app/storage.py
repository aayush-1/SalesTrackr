import hashlib
import io
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
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


def history_heads(revisions):
    versions = {}
    children = {}
    for raw in revisions:
        parsed = Revision.model_validate(raw)
        MODELS[parsed.entityType].model_validate(parsed.data)
        if parsed.id in versions:
            raise Problem("A record contains duplicate history identifiers.", 503, "data_altered")
        versions[parsed.id] = raw
        children[parsed.id] = []
    if not versions:
        raise Problem("A record has no saved versions.", 503, "data_altered")
    entity = {(r["entityType"], r["entityId"]) for r in versions.values()}
    if len(entity) != 1:
        raise Problem("Record history mixes different entities.", 503, "data_altered")
    remaining = {}
    for raw in versions.values():
        parents = raw["parents"]
        if len(parents) != len(set(parents)) or any(p not in versions for p in parents):
            raise Problem("Record history has missing or repeated parents.", 503, "data_altered")
        remaining[raw["id"]] = len(parents)
        for parent in parents:
            children[parent].append(raw["id"])
    ready = [key for key, count in remaining.items() if count == 0]
    visited = 0
    while ready:
        key = ready.pop()
        visited += 1
        for child in children[key]:
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
    if visited != len(versions):
        raise Problem("Record history contains a cycle.", 503, "data_altered")
    return {key for key, descendants in children.items() if not descendants}


def document(revisions):
    heads = history_heads(revisions)
    current = next((r for r in revisions if heads == {r["id"]}), None)
    value = {
        "schemaVersion": 2,
        "entityId": revisions[0]["entityId"],
        "entityType": revisions[0]["entityType"],
        "current": current,
        "history": sorted(
            (r for r in revisions if current is None or r["id"] != current["id"]),
            key=lambda r: (r["createdAt"], r["id"]),
        ),
    }
    return {**value, "checksum": digest(value)}


def document_revisions(value):
    if not isinstance(value, dict) or value.get("schemaVersion") != 2:
        raise Problem("Expected a version-2 record document.", 503, "data_altered")
    if set(value) != {"schemaVersion", "entityId", "entityType", "current", "history", "checksum"}:
        raise Problem("A record document has unexpected fields.", 503, "data_altered")
    if not isinstance(value["history"], list):
        raise Problem("A record's embedded history is invalid.", 503, "data_altered")
    revisions = value["history"] + ([value["current"]] if value["current"] is not None else [])
    if document(revisions) != value:
        raise Problem("A record's contents or checksum were altered.", 503, "data_altered")
    return revisions


def prior_operation(revisions, revision):
    prior = next((r for r in revisions if r["id"] == revision["id"]), None)
    if prior is not None and any(
        prior[key] != revision[key] for key in ("entityId", "entityType", "fingerprint", "actor")
    ):
        raise Problem("Save identifier already exists with different content.", 409, "write_id_reused")
    return prior


def append_revision(revisions, revision):
    prior = prior_operation(revisions, revision)
    if prior is not None:
        return None, prior
    heads = history_heads(revisions) if revisions else set()
    if heads != set(revision["parents"]):
        raise Problem("This record changed. Refresh before editing.", 409, "stale_edit")
    return document([*revisions, revision]), revision


class LocalBackend:
    """Local demo only. Production never constructs this backend."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.known_files = set()
        self.entity_files = {}
        self.file_versions = {}
        directory.mkdir(parents=True, exist_ok=True)

    def allocate(self):
        return uuid4().hex

    def get(self, file_id: str):
        path = self.directory / f"{file_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @contextmanager
    def _lock(self):
        with (self.directory / ".storage.lock").open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, entity_id, value):
        path = self.directory / f"{entity_id}.json"
        temporary = self.directory / f"{entity_id}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, revision: dict):
        with self._lock():
            existing = self.get(revision["entityId"])
            revisions = document_revisions(existing) if existing is not None else []
            value, saved = append_revision(revisions, revision)
            if value is not None:
                self._write(revision["entityId"], value)
            self.entity_files[revision["entityId"]] = revision["entityId"]
            self.file_versions[revision["entityId"]] = {r["id"] for r in revisions} | {saved["id"]}
            return saved

    def revisions(self, entity_id):
        value = self.get(entity_id)
        if value is None:
            raise Problem("A saved record is missing.", 503, "data_removed")
        return document_revisions(value)

    def initial(self):
        with self._lock():
            groups, legacy = {}, []
            for path in self.directory.glob("*.json"):
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("schemaVersion") == 2:
                    revisions = document_revisions(value)
                    if path.stem != value["entityId"]:
                        raise Problem("A record file has the wrong name.", 503, "data_altered")
                else:
                    Revision.model_validate(value)
                    revisions = [value]
                    legacy.append(path)
                for raw in revisions:
                    versions = groups.setdefault(raw["entityId"], {})
                    if raw["id"] in versions and versions[raw["id"]] != raw:
                        raise Problem("Conflicting local history content.", 503, "data_altered")
                    versions[raw["id"]] = raw
            if legacy:
                for entity_id, versions in groups.items():
                    self._write(entity_id, document(list(versions.values())))
                for path in legacy:
                    if path.stem not in groups:
                        path.unlink()
            self.entity_files = {entity: entity for entity in groups}
            self.file_versions = {entity: set(versions) for entity, versions in groups.items()}
            revisions = [raw for versions in groups.values() for raw in versions.values()]
            return None, revisions, None

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
    def __init__(self, token_json: str, root_id: str, *, operator=False):
        info = json.loads(token_json)
        if not info.get("refresh_token"):
            raise RuntimeError("Drive credential must contain a refresh token.")
        self.credentials = Credentials.from_authorized_user_info(info, SCOPES)
        self.root_id = root_id
        self.folders = None
        self.service = None
        self.conditional_service = None
        self.ignore_checkpoints = False
        self.operator = operator
        self.settings_id = None
        self.settings = None
        self.entity_files = {}
        self.file_versions = {}

    def _api(self):
        if self.service is None:
            self.service = build(
                "drive",
                "v3",
                http=AuthorizedHttp(self.credentials, http=Http(timeout=20)),
                cache_discovery=False,
            )
        return self.service

    def _conditional_api(self):
        if self.conditional_service is None:
            # v3 does not enforce If-Match. v2 still exposes and enforces file ETags.
            self.conditional_service = build(
                "drive",
                "v2",
                http=AuthorizedHttp(self.credentials, http=Http(timeout=20)),
                cache_discovery=False,
            )
        return self.conditional_service

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
            if status == 412:
                raise Problem("This record changed. Refresh before editing.", 409, "stale_edit") from None
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

    def _folders(self, *, refresh=False, allow_legacy=False):
        if self.folders is None or refresh:
            # Root IDs come from operator configuration, never arbitrary request input.
            if not self.root_id.replace("-", "").replace("_", "").isalnum():
                raise Problem("Invalid configured Drive root ID.", 503, "drive_config")
            if self.settings_id is None:
                files = list(
                    self._list(f"'{self.root_id}' in parents and name = 'settings.json' and trashed = false")
                )
                if len(files) != 1:
                    raise Problem(
                        "Run the Drive bootstrap: exactly one settings.json is required.", 503, "drive_config"
                    )
                self.settings_id = files[0]["id"]
            settings = self.get(self.settings_id)
            if (
                not isinstance(settings, dict)
                or settings.get("schemaVersion") not in (1, 2)
                or settings.get("rootId") != self.root_id
            ):
                raise Problem("The Drive folder configuration is incompatible.", 503, "drive_config")
            if settings.get("maintenance") and not self.operator:
                raise Problem(
                    "Storage maintenance is in progress. Retry shortly.", 503, "storage_maintenance"
                )
            if settings["schemaVersion"] == 1 and not allow_legacy:
                raise Problem(
                    "Storage is awaiting the one-file migration. Saves are paused until it completes.",
                    503,
                    "migration_required",
                )
            if settings["schemaVersion"] == 2 and not settings.get("checkpointId"):
                raise Problem("The shared checkpoint ID is missing.", 503, "drive_config")
            if self.folders != settings["folders"]:
                for key in (*FOLDERS, "indexes", "backups"):
                    metadata = self._execute(
                        self._api()
                        .files()
                        .get(
                            fileId=settings["folders"][key],
                            fields="id,parents,mimeType,trashed",
                        )
                    )
                    if (
                        metadata.get("trashed")
                        or self.root_id not in metadata.get("parents", [])
                        or (metadata["mimeType"] != "application/vnd.google-apps.folder")
                    ):
                        raise Problem("A configured Drive folder is missing or moved.", 503, "drive_config")
            self.settings = settings
            self.folders = settings["folders"]
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

    def etag(self, file_id):
        return self._execute(self._conditional_api().files().get(fileId=file_id, fields="etag"))["etag"]

    def versioned_get(self, file_id):
        before = self.etag(file_id)
        content = self.get(file_id)
        after = self.etag(file_id)
        if before != after:
            raise Problem("This record changed. Refresh before editing.", 409, "stale_edit")
        if content is None:
            raise Problem("A saved record is missing.", 503, "data_removed")
        return content, after

    def replace(self, file_id, data, etag, *, name=None, properties=None):
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode()
        body = {}
        if name is not None:
            body["title"] = name
        if properties is not None:
            body["properties"] = [
                {"key": key, "value": value, "visibility": "PRIVATE"} for key, value in properties.items()
            ]
        request = self._conditional_api().files().update(
            fileId=file_id,
            body=body,
            media_body=MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json"),
            fields="id",
        )
        request.headers["If-Match"] = etag
        try:
            return request.execute(num_retries=0)
        except HttpError as error:
            if error.resp.status == 412:
                raise Problem("This record changed. Refresh before editing.", 409, "stale_edit") from None
            raise Problem(
                "Drive save is unconfirmed. Retry without changing this save.", 503, "save_unknown"
            ) from None
        except (OSError, HttpLib2Error, TransportError):
            raise Problem(
                "Drive save is unconfirmed. Retry without changing this save.", 503, "save_unknown"
            ) from None
        except RefreshError:
            raise Problem("Drive needs reconnection; save is unconfirmed.", 503, "drive_reconnect") from None

    def trash(self, file_id, *, etag=None):
        if etag is None:
            _, etag = self.versioned_get(file_id)
        request = self._conditional_api().files().update(
            fileId=file_id, body={"labels": {"trashed": True}}, fields="id"
        )
        request.headers["If-Match"] = etag
        return self._execute(request)

    def _upload(self, data, parent, filename, file_id=None, properties=None):
        body = {"name": filename, "parents": [parent], "mimeType": "application/json"}
        if file_id:
            body["id"] = file_id
        if properties:
            body["appProperties"] = properties
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode()
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
        folders = self._folders(refresh=True)
        entity_id = revision["entityId"]
        file_id = self.entity_files.get(entity_id)
        if file_id:
            value, etag = self.versioned_get(file_id)
            revisions = document_revisions(value)
        else:
            revisions, etag = [], None
            file_id = entity_id
        value, saved = append_revision(revisions, revision)
        if value is None:
            return saved
        try:
            if etag is not None:
                self.replace(file_id, value, etag)
            else:
                self._upload(
                    value,
                    folders[revision["entityType"]],
                    f"{entity_id}.json",
                    file_id,
                    {"app": "salestrackr", "entityType": revision["entityType"], "entityId": entity_id},
                )
        except Problem as error:
            if error.code not in ("stale_edit", "write_id_reused"):
                raise
            latest = self.get(file_id)
            if latest is not None:
                prior = prior_operation(document_revisions(latest), revision)
                if prior is not None:
                    self.entity_files[entity_id] = file_id
                    self.file_versions[file_id] = {r["id"] for r in document_revisions(latest)}
                    return prior
            raise Problem("This record changed. Refresh before editing.", 409, "stale_edit") from None
        self.entity_files[entity_id] = file_id
        self.file_versions[file_id] = {r["id"] for r in [*revisions, saved]}
        return saved

    def revisions(self, entity_id):
        file_id = self.entity_files.get(entity_id)
        if not file_id:
            raise Problem("The record's Drive file is not indexed.", 503, "index_incomplete")
        value = self.get(file_id)
        if value is None:
            raise Problem("A saved record is missing.", 503, "data_removed")
        revisions = document_revisions(value)
        if value["entityId"] != entity_id:
            raise Problem("A Drive file belongs to the wrong record.", 503, "data_altered")
        return revisions

    def _ingest(self, file_id, value, kind):
        revisions = document_revisions(value)
        entity = value["entityId"]
        if value["entityType"] != kind:
            raise Problem("A record is in the wrong Drive folder.", 503, "data_altered")
        if entity in self.entity_files and self.entity_files[entity] != file_id:
            raise Problem("Multiple Drive files exist for one record.", 503, "data_altered")
        ids = {r["id"] for r in revisions}
        if not self.file_versions.get(file_id, set()).issubset(ids):
            raise Problem("A record's saved history was removed.", 503, "data_altered")
        self.entity_files[entity] = file_id
        self.file_versions[file_id] = ids
        return revisions

    def initial(self):
        folders = self._folders(refresh=True)
        self.entity_files, self.file_versions = {}, {}
        if not self.ignore_checkpoints:
            try:
                checkpoint = self.get(self.settings["checkpointId"])
                state = checkpoint.get("state", {}) if isinstance(checkpoint, dict) else {}
                if (
                    isinstance(checkpoint, dict)
                    and checkpoint.get("format") == 2
                    and checkpoint.get("rootId") == self.root_id
                    and set(state) == {"meta", "heads", "headData", "cursor", "entityFiles"}
                    and checkpoint.get("checksum") == digest(state)
                    and isinstance(state["cursor"], str)
                    and all(isinstance(state[k], dict) for k in ("meta", "heads", "headData", "entityFiles"))
                ):
                    for head_id, revision in state["headData"].items():
                        parsed = Revision.model_validate(revision)
                        MODELS[parsed.entityType].model_validate(parsed.data)
                        if state["meta"][head_id]["hash"] != digest(revision):
                            raise ValueError("Checkpoint head checksum mismatch")
                    if len(set(state["entityFiles"].values())) != len(state["entityFiles"]):
                        raise ValueError("Checkpoint contains duplicate entity files")
                    file_versions = {}
                    for revision_id, metadata in state["meta"].items():
                        file_id = state["entityFiles"][metadata["entityId"]]
                        file_versions.setdefault(file_id, set()).add(revision_id)
                    self.entity_files = state["entityFiles"].copy()
                    self.file_versions = file_versions
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
                revisions.extend(self._ingest(file["id"], content, key))
        return None, revisions, token

    def changes(self, cursor):
        folders = self._folders(refresh=True)
        allowed = {folders[key]: key for key in FOLDERS}
        revisions, removed = [], []
        downloaded = {}
        while True:
            result = self._execute(
                self._api()
                .changes()
                .list(
                    pageToken=cursor,
                    pageSize=1000,
                    fields=(
                        "nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,file(id,parents,trashed,version))"
                    ),
                    includeRemoved=True,
                )
            )
            for change in result.get("changes", []):
                metadata = change.get("file", {})
                if (
                    change.get("removed")
                    or metadata.get("trashed")
                    or not set(allowed).intersection(metadata.get("parents", []))
                ):
                    removed.extend(self.file_versions.get(change["fileId"], set()))
                    downloaded.pop(change["fileId"], None)
                    continue
                version = metadata.get("version")
                if version is not None and downloaded.get(change["fileId"]) == version:
                    continue
                content = self.get(change["fileId"])
                if content is None:
                    raise Problem("A Drive record is unavailable.", 503, "index_incomplete")
                kind = next(allowed[parent] for parent in metadata["parents"] if parent in allowed)
                revisions.extend(self._ingest(change["fileId"], content, kind))
                downloaded[change["fileId"]] = version
            if result.get("nextPageToken"):
                cursor = result["nextPageToken"]
            else:
                return revisions, result["newStartPageToken"], removed

    def checkpoint(self, state):
        folders = self._folders()
        value = {"format": 2, "rootId": self.root_id, "state": state, "checksum": digest(state)}
        file_id = self.settings["checkpointId"]
        try:
            existing = self.get(file_id)
        except (ValueError, TypeError):
            logger.warning("Replacing corrupt derived Drive checkpoint")
            existing = True
        if existing is None:
            self._upload(value, folders["indexes"], "checkpoint.json", file_id)
        else:
            metadata = self._execute(
                self._api().files().get(fileId=file_id, fields="parents,mimeType,trashed")
            )
            if (
                metadata.get("trashed")
                or metadata.get("parents") != [folders["indexes"]]
                or metadata.get("mimeType") != "application/json"
            ):
                raise Problem("The checkpoint is outside its configured index folder.", 503, "drive_config")
            self.replace(file_id, value, self.etag(file_id), name="checkpoint.json")


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
            "entityFiles": self.backend.entity_files.copy(),
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

    def prior_write(self, write_id, fingerprint, actor, entity_id):
        metadata = self.meta.get(write_id)
        if metadata is not None:
            if metadata["entityId"] != entity_id:
                raise Problem("Save identifier belongs to another record.", 409, "write_id_reused")
            prior = next(r for r in self.read_history(entity_id) if r["id"] == write_id)
            if prior.get("fingerprint") != fingerprint or prior.get("actor") != actor:
                raise Problem(
                    "Save identifier was already used for a different request.", 409, "write_id_reused"
                )
            self._apply(prior)
            return prior
        return None

    def commit(self, revision):
        revision = self.backend.create(revision)
        self._apply(revision)
        warning = None
        try:
            self.sync(force=True)
        except Problem as error:
            warning = f"Saved, but synchronization is pending: {error.message}"
        return {"revision": revision, "warning": warning}

    def read_history(self, entity_id):
        revisions = self.backend.revisions(entity_id)
        expected = {key: value["hash"] for key, value in self.meta.items() if value["entityId"] == entity_id}
        actual = {r["id"]: digest(r) for r in revisions}
        if expected != actual:
            raise Problem(
                "Record history changed or is incomplete. Refresh and retry.", 503, "data_altered"
            )
        return revisions

    def history(self, entity_id):
        entries = sorted(
            (v for v in self.meta.values() if v["entityId"] == entity_id),
            key=lambda v: (v["createdAt"], v["id"]),
            reverse=True,
        )
        return entries
