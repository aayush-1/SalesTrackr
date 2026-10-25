"""Transactional live storage. Immutable payloads are separate from indexed projections."""

import hashlib
import json
import os
import threading
import time
import zlib
from itertools import product
from urllib.parse import urlsplit
from uuid import uuid4

from google.api_core.exceptions import AlreadyExists
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .domain import MODELS, Problem, RecordData, Revision, now, record_stage
from .queries import VIEWS, account_matches, account_name_key, matches, query_records
from .storage import digest, encode, history_heads
from .timing import timed_phase

CHUNK_SIZE = 512 * 1024
MAX_PAYLOAD = 256 * 1024 * 1024


def filtered(query, field, operator, value):
    return query.where(filter=FieldFilter(field, operator, value))


def bucket(view="all", account_id=None, stage=None):
    return f"record|{view}|{account_id or '*'}|{stage or '*'}"


def projection(revisions, heads=None):
    first = revisions[0]
    heads = history_heads(revisions) if heads is None else heads
    current = [r for r in revisions if r["id"] in heads]
    row = {
        "entityId": first["entityId"],
        "kind": first["entityType"],
        "heads": sorted(heads),
        "conflicted": len(heads) != 1,
    }
    if row["kind"] == "account":
        names = {r["id"]: r["data"]["name"] for r in current}
        row.update(
            headNames=names,
            nameChars=sorted(set("".join(account_name_key(name) for name in names.values()))),
        )
        if len(current) == 1:
            data = current[0]["data"]
            row.update(
                name=data["name"], address=data.get("address", ""),
                nameSort=data["name"].casefold(), nameSearchChars=sorted(set(data["name"].casefold())),
            )
    if len(current) == 1 and row["kind"] in ("record", "contact"):
        data = current[0]["data"]
        row.update(accountId=data["accountId"], contactId=data.get("contactId"))
        if row["kind"] == "record":
            data = RecordData.model_validate(data).model_dump(mode="json")
            stage = record_stage(data)
            views = [view for view in VIEWS if matches(data, view)]
            row.update(
                acquiredOn=data["stageDates"]["new_lead"],
                views=views,
                buckets=[
                    bucket(view, account_id, stage_filter)
                    for view, account_id, stage_filter in product(
                        views, (None, data["accountId"]), (None, stage)
                    )
                ],
                queryData={key: data[key] for key in (
                    "accountId", "productNotes", "stageDates", "workflowVersion", "discarded",
                    "paymentReceived", "feedbackReceived", "nextFollowUpOn",
                )},
            )
    return row


def counters(rows):
    counts = {kind: 0 for kind in MODELS}
    dashboard = {view: 0 for view in VIEWS}
    conflicts = 0
    for row in rows:
        counts[row["kind"]] += 1
        conflicts += row["conflicted"]
        for view in row.get("views", []):
            dashboard[view] += 1
    return {"entityCounts": counts, "dashboard": dashboard, "conflictCount": conflicts}


class FirestoreRepository:
    def __init__(self, settings, client=None, *, initialize=False):
        self.settings = settings
        if os.getenv("K_SERVICE") and settings.mode != "production":
            raise RuntimeError("Demo mode is forbidden on Cloud Run.")
        if settings.mode == "demo" and (
            not os.getenv("FIRESTORE_EMULATOR_HOST")
            or not settings.firestore_project.startswith("demo-")
            or urlsplit(f"http://{os.getenv('FIRESTORE_EMULATOR_HOST', '')}").hostname
            not in ("localhost", "127.0.0.1", "::1")
        ):
            raise RuntimeError("The local Firestore backend requires an isolated emulator.")
        if settings.mode == "production" and os.getenv("FIRESTORE_EMULATOR_HOST"):
            raise RuntimeError("Production cannot use the Firestore emulator.")
        self.client = client or firestore.Client(
            project=settings.firestore_project,
            database=settings.firestore_database,
            credentials=AnonymousCredentials() if settings.mode == "demo" else None,
        )
        self.workspace = self.client.collection("workspaces").document(settings.firestore_workspace)
        self.lock = threading.RLock()
        self.backend = self
        self.synced_at = None
        self.error = None
        self.control = {}
        if initialize or settings.mode == "demo":
            namespace = uuid4().hex
            try:
                self.workspace.create({
                    "activeDataset": namespace, "generation": namespace, "sequence": 0,
                    "maintenance": None, "weeklyEnabled": True, **counters([]),
                })
            except AlreadyExists:
                pass
        self.sync()

    @property
    def namespace(self):
        return self.workspace.collection("datasets").document(self.control["activeDataset"])

    @property
    def generation(self):
        return self.control["generation"]

    @property
    def meta(self):
        # The existing synthetic demo seeder only needs an emptiness check.
        first = next(self.namespace.collection("revisions").limit(1).stream(), None)
        return {"initialized": True} if first else {}

    def allocate(self):
        return uuid4().hex

    def _apply(self, revision):
        pass

    def sync(self, force=False):
        with self.lock:
            self._sync()

    def _sync(self):
        value = self.workspace.get().to_dict()
        if not value or not value.get("activeDataset"):
            raise Problem("Firestore has not been migrated and activated.", 503, "storage_uninitialized")
        maintenance = value.get("maintenance")
        if maintenance and maintenance.get("expires", float("inf")) <= time.time():
            @firestore.transactional
            def expire(transaction):
                control = self.workspace.get(transaction=transaction).to_dict()
                pending = control.get("maintenance")
                if pending and pending.get("expires", float("inf")) <= time.time():
                    control = {
                        **control, "maintenance": None,
                        ("lastCleanupError" if pending.get("kind") == "cleanup" else "lastBackupError"): (
                            "An interrupted cleanup expired. Resume it from Settings."
                            if pending.get("kind") == "cleanup"
                            else "An interrupted restore expired; the existing live data was retained."
                        ),
                    }
                    transaction.set(self.workspace, control)
                return control

            value = expire(self.client.transaction())
        self.control = value
        self.synced_at = now()
        self.error = "Storage maintenance is in progress." if value.get("maintenance") else None

    def metadata(self):
        return {
            "syncedAt": self.synced_at,
            "conflictCount": self.control["conflictCount"],
            "complete": not self.control["conflictCount"] and not self.error,
            "generation": self.generation,
            "storageBackend": "firestore",
        }

    def check_generation(self, generation, control=None):
        control = control or self.control
        if not generation or generation != control["generation"]:
            raise Problem(
                "The workspace was restored or this page is outdated. Refresh before saving.",
                409, "workspace_changed",
            )
        if control.get("maintenance"):
            raise Problem("Storage maintenance is in progress. Please retry shortly.", 503, "maintenance")

    def _store_payload(self, raw):
        data = encode(raw)
        if len(data) > MAX_PAYLOAD:
            raise Problem("This saved version exceeds the supported payload size.", 413, "record_size")
        key = hashlib.sha256(data).hexdigest()
        ref = self.workspace.collection("payloads").document(key)
        existing = ref.get().to_dict()
        if existing:
            if existing["hash"] != key or existing["size"] != len(data):
                raise Problem("Immutable payload metadata was altered.", 503, "data_altered")
            return existing
        packed = zlib.compress(data)
        parts = [packed[i:i + CHUNK_SIZE] for i in range(0, len(packed), CHUNK_SIZE)]
        value = {
            "hash": key, "size": len(data), "packedSize": len(packed), "parts": len(parts),
            "encoding": hashlib.sha256(packed).hexdigest(),
        }
        collection = self._part_collection(ref, value)
        for start in range(0, len(parts), 12):
            batch = self.client.batch()
            for index in range(start, min(start + 12, len(parts))):
                part = collection.document(str(index))
                batch.set(part, {"data": parts[index]})
            batch.commit()
        try:
            ref.create(value)
        except AlreadyExists:
            existing = ref.get().to_dict()
            if existing["hash"] != key or existing["size"] != len(data):
                raise Problem("Immutable payload metadata was altered.", 503, "data_altered") from None
            return existing
        return value

    @staticmethod
    def _part_collection(ref, info):
        # Separate compressed encodings cannot overwrite each other across zlib versions.
        if info.get("encoding"):
            return ref.collection("encodings").document(info["encoding"]).collection("parts")
        return ref.collection("parts")

    def _payload(self, metadata):
        try:
            return self._checked_payload(metadata)
        except (KeyError, TypeError, ValueError, zlib.error, json.JSONDecodeError):
            raise Problem("A saved payload or its index is invalid.", 503, "data_altered") from None

    @staticmethod
    def _validate_info(metadata, info):
        if (
            not isinstance(metadata.get("hash"), str) or len(metadata["hash"]) != 64
            or any(character not in "0123456789abcdef" for character in metadata["hash"])
        ):
            raise Problem("A saved payload index is invalid.", 503, "data_altered")
        if not info or info["hash"] != metadata["hash"]:
            raise Problem("A saved payload is missing.", 503, "data_removed")
        if (
            not 0 < info["size"] <= MAX_PAYLOAD
            or not 0 < info["packedSize"] <= MAX_PAYLOAD + 1024 * 1024
            or info["parts"] != (info["packedSize"] + CHUNK_SIZE - 1) // CHUNK_SIZE
        ):
            raise Problem("A saved payload size is invalid.", 503, "data_altered")

    def _checked_payload(self, metadata, info=None, parts=None):
        ref = self.workspace.collection("payloads").document(metadata["hash"])
        info = info if info is not None else ref.get().to_dict()
        self._validate_info(metadata, info)
        if parts is None:
            collection = self._part_collection(ref, info)
            refs = [collection.document(str(index)) for index in range(info["parts"])]
            parts = {int(item.id): item.to_dict() for item in self.client.get_all(refs)}
        if (
            len(parts) != info["parts"]
            or any(
                not part or not isinstance(part.get("data"), bytes) or not 0 < len(part["data"]) <= CHUNK_SIZE
                for part in parts.values()
            )
        ):
            raise Problem("A saved payload is incomplete.", 503, "data_removed")
        packed = b"".join(parts[index]["data"] for index in range(info["parts"]))
        decompressor = zlib.decompressobj()
        data = decompressor.decompress(packed, MAX_PAYLOAD + 1)
        if (
            len(data) > MAX_PAYLOAD or not decompressor.eof or decompressor.unused_data
            or len(data) != info["size"] or len(packed) != info["packedSize"]
            or hashlib.sha256(data).hexdigest() != metadata["hash"]
        ):
            raise Problem("A saved payload failed its integrity check.", 503, "data_altered")
        raw = json.loads(data)
        if any(
            raw[key] != metadata[key]
            for key in ("id", "entityId", "entityType", "parents", "createdAt", "fingerprint", "actor")
            if key in metadata
        ):
            raise Problem("A saved version index was altered.", 503, "data_altered")
        return raw

    def _payload_infos(self, hashes):
        keys = sorted(set(hashes))
        values = {}
        for start in range(0, len(keys), 100):
            refs = [
                self.workspace.collection("payloads").document(key) for key in keys[start:start + 100]
            ]
            values.update((item.id, item.to_dict()) for item in self.client.get_all(refs))
        return values

    def _payloads(self, entries):
        infos = self._payload_infos(entry["hash"] for entry in entries)
        for entry in entries:
            self._validate_info(entry, infos.get(entry["hash"]))
        if sum(infos[entry["hash"]]["size"] for entry in entries) > MAX_PAYLOAD:
            raise Problem("The snapshot exceeds the 256 MiB expanded-data limit.", 413, "backup_size")
        result, group, size = [], [], 0

        def read_group():
            refs, locations = [], {}
            for entry in group:
                key = entry["hash"]
                ref = self.workspace.collection("payloads").document(key)
                collection = self._part_collection(ref, infos[key])
                for index in range(infos[key]["parts"]):
                    part = collection.document(str(index))
                    refs.append(part)
                    locations[part.path] = (key, index)
            parts = {}
            for item in self.client.get_all(refs):
                key, index = locations[item.reference.path]
                parts.setdefault(key, {})[index] = item.to_dict()
            return [
                self._checked_payload(entry, infos[entry["hash"]], parts.get(entry["hash"], {}))
                for entry in group
            ]

        for entry in entries:
            packed_size = infos[entry["hash"]]["packedSize"]
            if group and (len(group) >= 100 or size + packed_size > 4 * 1024 * 1024):
                result.extend(read_group())
                group, size = [], 0
            group.append(entry)
            size += packed_size
        if group:
            result.extend(read_group())
        return result

    def _revision(self, revision_id, namespace=None):
        metadata = (namespace or self.namespace).collection("revisions").document(revision_id).get().to_dict()
        if not metadata:
            raise Problem("A saved version is missing.", 503, "data_removed")
        raw = self._payload(metadata)
        if any(raw[key] != metadata[key] for key in ("id", "entityId", "entityType", "parents", "createdAt")):
            raise Problem("A saved version index was altered.", 503, "data_altered")
        return raw

    def _entity(self, entity_id, kind=None):
        row = self.namespace.collection("entities").document(entity_id).get().to_dict()
        if not row or (kind and row["kind"] != kind):
            raise Problem("Record not found.", 404, "not_found")
        return row

    def current(self, entity_id, kind=None):
        row = self._entity(entity_id, kind)
        if row["conflicted"]:
            raise Problem(
                "This record has conflicting saved versions. Resolve it first.", 409, "record_conflict"
            )
        revision = self._revision(row["heads"][0])
        if revision["entityId"] != entity_id or revision["entityType"] != row["kind"]:
            raise Problem("A current-version index was altered.", 503, "data_altered")
        return revision

    def exists(self, entity_id):
        return self.namespace.collection("entities").document(entity_id).get().exists

    def is_current(self, revision):
        row = self.namespace.collection("entities").document(revision["entityId"]).get().to_dict()
        return bool(row and row["heads"] == [revision["id"]])

    def _entities(self, kind=None, include_conflicts=False):
        query = self.namespace.collection("entities")
        if kind:
            query = filtered(query, "kind", "==", kind)
        if not include_conflicts:
            query = filtered(query, "conflicted", "==", False)
        return query

    def list(self, kind):
        return [self._revision(item.to_dict()["heads"][0]) for item in self._entities(kind).stream()]

    def conflicts(self):
        query = filtered(self.namespace.collection("entities"), "conflicted", "==", True)
        return [{
            "entityId": item.id, "entityType": item.to_dict()["kind"],
            "versions": [self._revision(head) for head in item.to_dict()["heads"]],
        } for item in query.stream()]

    def prior_write(self, write_id, fingerprint, actor, entity_id):
        metadata = self.namespace.collection("revisions").document(write_id).get().to_dict()
        if not metadata:
            return None
        self._check_prior(metadata, fingerprint, actor, entity_id)
        return self._payload(metadata)

    @staticmethod
    def _check_prior(metadata, fingerprint, actor, entity_id):
        if (
            metadata["entityId"] != entity_id or metadata["fingerprint"] != fingerprint
            or metadata["actor"] != actor
        ):
            raise Problem("Save identifier was used for a different request.", 409, "write_id_reused")

    def mutate(self, kind, write, action, actor, transform, create, generation):
        with timed_phase("syncBeforeMs"):
            self.sync()
        self.check_generation(generation)
        fingerprint = digest({"kind": kind, "action": action, "write": write.model_dump(mode="json")})
        prior = self.prior_write(write.writeId, fingerprint, actor, write.entityId)
        if prior:
            return {"revision": prior, "warning": None}
        with timed_phase("readCurrentMs"):
            old = None if create else self.current(write.entityId, kind)
        if create and (write.baseRevision is not None or self.exists(write.entityId)):
            raise Problem("That record already exists.", 409, "record_exists")
        if old and write.baseRevision != old["id"]:
            raise Problem("This record changed. Refresh before editing.", 409, "stale_edit")
        if kind == "record" and old and old["data"].get("discarded", False):
            raise Problem("Discarded sales records are read-only.", 409, "discarded_record")
        data = transform(old["data"] if old else None).model_dump(mode="json")
        if old and kind in ("record", "contact") and old["data"]["accountId"] != data["accountId"]:
            raise Problem("Contacts and sales records cannot be reassigned to another account.")
        revision = Revision(
            id=write.writeId, entityId=write.entityId, entityType=kind,
            parents=[old["id"]] if old else [], createdAt=now(), actor=actor,
            action=action, reason=getattr(write.data, "reason", ""), fingerprint=fingerprint, data=data,
        ).model_dump(mode="json")
        return self.commit(revision, generation=generation)

    def commit(self, revision, *, generation=None):
        Revision.model_validate(revision)
        MODELS[revision["entityType"]].model_validate(revision["data"])
        generation = generation or self.generation
        namespace = self.namespace
        with timed_phase("payloadWriteMs"):
            payload = self._store_payload(revision)
        entity_ref = namespace.collection("entities").document(revision["entityId"])
        version_ref = namespace.collection("revisions").document(revision["id"])
        new_row = projection([revision], heads={revision["id"]})

        @firestore.transactional
        def apply(transaction):
            control = self.workspace.get(transaction=transaction).to_dict()
            self.check_generation(generation, control)
            prior = version_ref.get(transaction=transaction).to_dict()
            if prior:
                self._check_prior(prior, revision["fingerprint"], revision["actor"], revision["entityId"])
                return prior, control, None
            old = entity_ref.get(transaction=transaction).to_dict()
            if revision["entityType"] == "record":
                removed = namespace.collection("recordDeletions").document(revision["entityId"]).get(
                    transaction=transaction
                )
                if removed.exists:
                    raise Problem("This sales record was permanently deleted.", 409, "record_deleted")
            if old and old["kind"] != revision["entityType"]:
                raise Problem("That identifier belongs to another entity type.", 409, "record_exists")
            if set(old["heads"] if old else []) != set(revision["parents"]):
                raise Problem("This record changed. Refresh before editing.", 409, "stale_edit")
            data, kind = revision["data"], revision["entityType"]
            if kind in ("record", "contact"):
                account = namespace.collection("entities").document(data["accountId"]).get(
                    transaction=transaction
                ).to_dict()
                self._reference(account, "account")
                if kind == "record" and data.get("contactId"):
                    contact = namespace.collection("entities").document(data["contactId"]).get(
                        transaction=transaction
                    ).to_dict()
                    self._reference(contact, "contact")
                    if contact["accountId"] != data["accountId"]:
                        raise Problem("Choose a contact belonging to this account.")
            claims = {}
            if kind == "account":
                keys = {account_name_key(data["name"])}
                keys.update(account_name_key(name) for name in (old or {}).get("headNames", {}).values())
                for key in keys:
                    name_id = hashlib.sha256(key.encode()).hexdigest()
                    ref = namespace.collection("accountNames").document(name_id)
                    claim = ref.get(transaction=transaction).to_dict()
                    claims[key] = (ref, set((claim or {}).get("owners", [])))
                other_owners = claims[account_name_key(data["name"])][1] - {revision["entityId"]}
                if old is None and other_owners:
                    raise Problem(
                        "An account with this company name already exists. "
                        "Use the existing account instead of creating a duplicate.", 409, "account_exists",
                    )
            metadata = {
                key: revision[key]
                for key in ("id", "entityId", "entityType", "parents", "createdAt", "actor", "fingerprint")
            }
            metadata["hash"] = payload["hash"]
            transaction.create(version_ref, metadata)
            transaction.set(entity_ref, new_row)
            warning = None
            for key, (ref, owners) in claims.items():
                owners.discard(revision["entityId"])
                if key == account_name_key(data["name"]):
                    if owners:
                        warning = "Saved. Another account has the same name; check for duplicates."
                    owners.add(revision["entityId"])
                transaction.set(ref, {"owners": sorted(owners)})
            updated = {
                **control,
                "sequence": control["sequence"] + 1,
                "entityCounts": dict(control["entityCounts"]),
                "dashboard": dict(control["dashboard"]),
                "conflictCount": control["conflictCount"] - bool((old or {}).get("conflicted")),
            }
            if old is None:
                updated["entityCounts"][kind] += 1
            for view in VIEWS:
                updated["dashboard"][view] += (
                    int(view in new_row.get("views", [])) - int(view in (old or {}).get("views", []))
                )
            transaction.set(self.workspace, updated)
            return None, updated, warning

        with timed_phase("transactionMs"):
            prior, self.control, warning = apply(self.client.transaction())
        self.synced_at = now()
        return {"revision": self._payload(prior) if prior else revision, "warning": warning}

    @staticmethod
    def _reference(row, kind):
        if not row or row["kind"] != kind:
            raise Problem("Record not found.", 404, "not_found")
        if row["conflicted"]:
            raise Problem("Resolve the related record's conflicting versions first.", 409, "record_conflict")

    def create(self, revision):
        return self.commit(revision)["revision"]

    def resolve(self, kind, entity_id, body, actor, generation):
        self.sync()
        self.check_generation(generation)
        fingerprint = digest({
            "kind": kind, "entityId": entity_id, "resolution": body.model_dump(mode="json"),
        })
        prior = self.prior_write(body.writeId, fingerprint, actor, entity_id)
        if prior:
            return {"revision": prior, "warning": None}
        row = self._entity(entity_id, kind)
        if (
            set(row["heads"]) != set(body.parents) or len(row["heads"]) < 2
            or body.selectedRevision not in row["heads"]
        ):
            raise Problem(
                "The conflicting versions changed. Refresh and review them again.", 409, "stale_edit"
            )
        chosen = self._revision(body.selectedRevision)
        revision = Revision(
            id=body.writeId, entityId=entity_id, entityType=kind, parents=sorted(row["heads"]),
            createdAt=now(), actor=actor, action="resolve_conflict", reason=body.reason,
            fingerprint=fingerprint, data=MODELS[kind].model_validate(chosen["data"]).model_dump(mode="json"),
        ).model_dump(mode="json")
        return self.commit(revision, generation=generation)

    def matching_accounts(self, name, exclude=None):
        query = self._entities("account", include_conflicts=True)
        characters = sorted(set(account_name_key(name)))
        if not characters:
            return {"items": [], "total": 0, "exactCount": 0}
        if len(characters) <= 30:
            query = filtered(query, "nameChars", "array_contains_any", characters)
        candidates = []
        for snapshot in query.stream():
            row = snapshot.to_dict()
            candidates.extend({
                "id": revision_id, "entityId": row["entityId"], "entityType": "account",
                "data": {"name": head_name},
            } for revision_id, head_name in row["headNames"].items())
        result = account_matches(candidates, name, exclude)
        for item in result["items"]:
            item["revision"] = self._revision(item["revision"]["id"])
        return result

    def query_accounts(self, search="", offset=0, limit=100):
        query = self._entities("account")
        if search:
            query = filtered(query, "nameSearchChars", "array_contains", search.casefold()[0])
            rows = [s.to_dict() for s in query.stream()]
            rows = sorted(
                (row for row in rows if search.casefold() in row["name"].casefold()),
                key=lambda row: (row["nameSort"], row["entityId"]),
            )
            total, selected = len(rows), rows[offset:offset + limit]
        else:
            query = query.order_by("nameSort").order_by("entityId")
            total = self._count(query)
            selected = [s.to_dict() for s in query.offset(offset).limit(limit).stream()]
        return {"items": [self._revision(row["heads"][0]) for row in selected], "total": total}

    def query_contacts(self, account_id):
        query = filtered(self._entities("contact"), "accountId", "==", account_id)
        return [self._revision(s.to_dict()["heads"][0]) for s in query.stream()]

    @staticmethod
    def _count(query):
        return query.count().get()[0][0].value

    def _account_data(self, ids):
        refs = [self.namespace.collection("entities").document(key) for key in sorted(set(ids))]
        if not refs:
            return {}
        result = {}
        for item in self.client.get_all(refs):
            row = item.to_dict()
            if row and row["kind"] == "account" and not row["conflicted"]:
                result[item.id] = {"name": row["name"], "address": row["address"]}
        return result

    def query_records(self, view, search, account_id, stage, date_basis, start, end, due, offset, limit):
        query_records([], {}, view, search, account_id, stage, date_basis, start, end, due, offset, limit)
        query = filtered(self.namespace.collection("entities"), "buckets", "array_contains", bucket(
            view, account_id, stage
        ))
        if start:
            query = filtered(query, "acquiredOn", ">=", start.isoformat())
        if end:
            query = filtered(query, "acquiredOn", "<", end.isoformat())
        query = query.order_by("acquiredOn", direction=firestore.Query.DESCENDING).order_by(
            "entityId", direction=firestore.Query.DESCENDING
        )
        if not search and due == "all":
            total = self._count(query)
            rows = [s.to_dict() for s in query.offset(offset).limit(limit).stream()]
            accounts = self._account_data(row["accountId"] for row in rows)
            result = query_records(
                [
                    {"id": row["heads"][0], "entityId": row["entityId"], "data": row["queryData"]}
                    for row in rows
                ],
                accounts, view, "", account_id, stage, date_basis, start, end, due, 0, limit,
            )
            result["total"] = total
        else:
            rows = [s.to_dict() for s in query.stream()]
            accounts = self._account_data(row["accountId"] for row in rows)
            result = query_records(
                [
                    {"id": row["heads"][0], "entityId": row["entityId"], "data": row["queryData"]}
                    for row in rows
                ],
                accounts, view, search, account_id, stage, date_basis, start, end, due, offset, limit,
            )
        result["items"] = [{
            **self._revision(item["id"]), "accountName": item["accountName"],
            "accountLocation": item["accountLocation"],
        } for item in result["items"]]
        return result

    def history_page(self, entity_id, kind, offset, limit):
        self._entity(entity_id, kind)
        query = filtered(self.namespace.collection("revisions"), "entityId", "==", entity_id)
        query = query.order_by("createdAt", direction=firestore.Query.DESCENDING).order_by(
            "id", direction=firestore.Query.DESCENDING
        )
        return {
            "items": [self._payload(item.to_dict()) for item in query.offset(offset).limit(limit).stream()],
            "total": self._count(query),
        }

    def read_history(self, entity_id):
        query = filtered(self.namespace.collection("revisions"), "entityId", "==", entity_id)
        return [self._payload(item.to_dict()) for item in query.stream()]

    def snapshot(self):
        for _ in range(3):
            before = self.workspace.get().to_dict()
            namespace = self.workspace.collection("datasets").document(before["activeDataset"])
            entries = [item.to_dict() for item in namespace.collection("revisions").stream()]
            removed = {item.id for item in namespace.collection("recordDeletions").stream()}
            try:
                revisions = self._payloads([entry for entry in entries if entry["entityId"] not in removed])
            except Problem as error:
                changed = self.workspace.get().to_dict()
                if error.code in ("data_removed", "data_altered") and any(
                    before[key] != changed[key] for key in ("activeDataset", "generation", "sequence")
                ):
                    continue
                raise
            after = self.workspace.get().to_dict()
            if all(before[key] == after[key] for key in ("activeDataset", "generation", "sequence")):
                return revisions, before
        raise Problem(
            "Data changed during the backup. Please retry when saves have finished.", 409, "backup_busy"
        )

    def stage(self, revisions):
        groups = {}
        for raw in revisions:
            groups.setdefault(raw["entityId"], []).append(raw)
        rows = [projection(versions) for versions in groups.values()]
        namespace = self.workspace.collection("datasets").document(uuid4().hex)
        namespace.create({"createdAt": now(), "state": "staging"})
        writes = []
        claims = {}
        payload_hashes = {raw["id"]: digest(raw) for raw in revisions}
        existing_payloads = self._payload_infos(payload_hashes.values())
        for raw in revisions:
            payload = existing_payloads.get(payload_hashes[raw["id"]]) or self._store_payload(raw)
            value = {key: raw[key] for key in (
                "id", "entityId", "entityType", "parents", "createdAt", "actor", "fingerprint",
            )}
            value["hash"] = payload["hash"]
            writes.append((namespace.collection("revisions").document(raw["id"]), value))
        for row in rows:
            writes.append((namespace.collection("entities").document(row["entityId"]), row))
            for name in row.get("headNames", {}).values():
                key = hashlib.sha256(account_name_key(name).encode()).hexdigest()
                claims.setdefault(key, set()).add(row["entityId"])
        writes.extend(
            (namespace.collection("accountNames").document(key), {"owners": sorted(owners)})
            for key, owners in claims.items()
        )
        for start in range(0, len(writes), 100):
            batch = self.client.batch()
            for ref, value in writes[start:start + 100]:
                batch.create(ref, value)
            batch.commit()
        expected = {raw["id"]: digest(raw) for raw in revisions}
        entries = [item.to_dict() for item in namespace.collection("revisions").stream()]
        actual = {raw["id"]: digest(raw) for raw in self._payloads(entries)}
        actual_rows = {item.id: item.to_dict() for item in namespace.collection("entities").stream()}
        actual_claims = {
            item.id: item.to_dict() for item in namespace.collection("accountNames").stream()
        }
        expected_claims = {key: {"owners": sorted(owners)} for key, owners in claims.items()}
        if (
            actual != expected or actual_rows != {row["entityId"]: row for row in rows}
            or actual_claims != expected_claims
        ):
            raise Problem(
                "Staged restore verification failed; live data is unchanged.", 503, "restore_verify"
            )
        namespace.update({"state": "verified", "versionHashes": digest(expected)})
        return namespace, counters(rows)
