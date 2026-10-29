"""Stable product folders in the manufacturing workspace's private Drive root."""

import time

from fastapi import APIRouter, Request
from google.cloud import firestore

from .domain import Identifier, Problem
from .manufacturing_backups import ManufacturingDriveBackupStore
from .manufacturing_drive import relocate
from .storage import digest

FOLDER_MIME = "application/vnd.google-apps.folder"
APP = "producttrackr-product-files"


class ProductFiles:
    def __init__(self, repository, settings, store=None):
        self.repo, self.settings = repository, settings
        self.store = store

    def guard(self, control, workspace, generation):
        if (
            workspace != self.repo.workspace.id or generation != control["generation"]
            or self.repo.control["generation"] != control["generation"]
        ):
            raise Problem("ProductTrackr changed. Reload before opening files.", 409, "workspace_changed")
        if control.get("maintenance") or control.get("pendingRecordCleanup"):
            raise Problem("Storage maintenance is in progress. Retry shortly.", 503, "maintenance")
        if (control.get("backupLease") or {}).get("expires", 0) > time.time():
            raise Problem("A backup is in progress. Retry shortly.", 503, "backup_in_progress")

    def ensure(self, product_id, workspace, generation):
        self.guard(self.repo.workspace.get().to_dict(), workspace, generation)
        self.repo.read("products", product_id)
        configured = bool(
            self.settings.root_id and self.settings.token_json
        )
        if not configured and self.store is None:
            return {**self.repo.meta(), "configured": False, "url": None}
        store = self.store or ManufacturingDriveBackupStore(self.settings)
        root = store._root_folder()
        folder = store.products_folder()
        scope = digest(self.settings.manufacturing_identity(self.repo.workspace.id))[:24]
        reference = self.repo.workspace.collection("productFolders").document(product_id)
        properties = {"app": APP, "workspace": scope, "product": product_id}
        existing = reference.get().to_dict() or self.repo.restored_product_folder(product_id)
        candidate = existing["id"] if existing else None
        if not candidate:
            # Find moved/trashed folders too, so lost local mappings do not create replacements.
            matches = list(store.backend._list(
                f"appProperties has {{ key='app' and value='{APP}' }} "
                f"and appProperties has {{ key='workspace' and value='{scope}' }} "
                f"and appProperties has {{ key='product' and value='{product_id}' }}",
                fields="id",
            ))
            if len(matches) > 1:
                raise Problem("More than one managed folder exists for this product.", 409, "product_folder")
            candidate = matches[0]["id"] if matches else store.backend.allocate()

        @firestore.transactional
        def claim(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            product = self.repo.read("products", product_id, transaction)
            mapping = reference.get(transaction=transaction).to_dict()
            self.guard(control, workspace, generation)
            if mapping and mapping["parentId"] not in (root, folder):
                raise Problem(
                    "The product files folder configuration changed. Restore the original Drive root.",
                    409, "product_folder_changed",
                )
            mapping = mapping or {"id": candidate, "parentId": folder}
            transaction.set(reference, mapping)
            return product, mapping

        product, mapping = claim(self.repo.client.transaction())
        backend = store.backend

        def read_folder():
            return backend._execute(backend._api().files().get(
                fileId=mapping["id"], fields="id,name,parents,mimeType,trashed,appProperties",
            ))

        def check_folder(metadata):
            if (
                metadata.get("trashed") or metadata.get("parents") != [folder]
                or metadata.get("mimeType") != FOLDER_MIME
                or metadata.get("appProperties") != properties
            ):
                raise Problem(
                    "The product folder was moved or replaced. Restore it to SalesTrackr / Products.",
                    409, "product_folder_changed",
                )

        try:
            metadata = read_folder()
        except Problem as error:
            if error.code != "drive_missing":
                raise
            metadata = None
        if not metadata:
            # A reserved Drive ID makes concurrent calls and lost-response retries create the same folder.
            self.guard(self.repo.workspace.get().to_dict(), workspace, generation)
            try:
                backend._execute(backend._api().files().create(
                    body={
                        "id": mapping["id"],
                        "name": f"{product['partNumber'] + ' - ' if product['partNumber'] else ''}"
                                f"{product['name']}",
                        "mimeType": FOLDER_MIME, "parents": [folder], "appProperties": properties,
                    },
                    fields="id",
                ))
            except Problem:
                # Another request may have created our reserved ID, including after a response was lost.
                pass
            metadata = read_folder()
        if (
            metadata.get("parents") == [root] and not metadata.get("trashed")
            and metadata.get("mimeType") == FOLDER_MIME and metadata.get("appProperties") == properties
        ):
            self.guard(self.repo.workspace.get().to_dict(), workspace, generation)
            relocate(backend, mapping["id"], root, folder, FOLDER_MIME, properties)
            metadata = read_folder()
        check_folder(metadata)
        if mapping["parentId"] != folder:
            @firestore.transactional
            def finish_move(transaction):
                control = self.repo.workspace.get(transaction=transaction).to_dict()
                current = reference.get(transaction=transaction).to_dict()
                self.guard(control, workspace, generation)
                if current != mapping:
                    raise Problem("The product folder link changed. Retry.", 409, "product_folder_changed")
                transaction.update(reference, {"parentId": folder})

            finish_move(self.repo.client.transaction())
        for _ in range(3):
            etag = backend.etag(mapping["id"])
            product = self.repo.read("products", product_id)
            metadata = read_folder()
            check_folder(metadata)
            if self.repo.read("products", product_id)["version"] != product["version"]:
                continue
            name = f"{product['partNumber'] + ' - ' if product['partNumber'] else ''}{product['name']}"
            if metadata.get("name") != name:
                self.guard(self.repo.workspace.get().to_dict(), workspace, generation)
                request = backend._conditional_api().files().update(
                    fileId=mapping["id"], body={"title": name}, fields="id",
                )
                request.headers["If-Match"] = etag
                try:
                    backend._execute(request)
                except Problem as error:
                    if error.code == "stale_edit":
                        continue
                    raise
            if self.repo.read("products", product_id)["version"] == product["version"]:
                break
        else:
            raise Problem("The product changed while naming its Drive folder. Retry.", 409, "product_changed")
        return {
            **self.repo.meta(), "configured": True,
            "url": f"https://drive.google.com/drive/folders/{mapping['id']}",
        }


def manufacturing_files_router(repository, settings):
    router = APIRouter()

    @router.post("/products/{product_id}/drive-folder")
    def folder(product_id: Identifier, request: Request):
        return ProductFiles(repository(), settings).ensure(
            product_id,
            request.headers.get("x-producttrackr-workspace"),
            request.headers.get("x-producttrackr-generation"),
        )

    return router
