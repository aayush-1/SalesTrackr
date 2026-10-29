import os
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.firestore_storage import FirestoreRepository
from backend.app.main import create_app
from backend.app.manufacturing_backups import ManufacturingBackupManager
from backend.app.manufacturing_spreadsheet import SpreadsheetExport
from backend.app.manufacturing_storage import ManufacturingRepository
from backend.app.storage import LocalBackend, Repository
from backend.tests.test_manufacturing import create
from scripts.producttrackr_admin import database_rules
from scripts.producttrackr_deploy import CONFIG, production_environment


@pytest.fixture
def production(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    return Settings(
        "production", tmp_path, "sales-root", "sales-token", "iap-audience",
        "https://app.example.test", False,
        storage_backend="firestore",
        manufacturing_enabled=True, manufacturing_project="example-project",
        manufacturing_database="(default)", manufacturing_workspace="producttrackr",
        firestore_project="example-project",
        backup_folder_id="sales-backups", manufacturing_backup_storage="drive",
        manufacturing_admins=("product-admin@example.test",),
    )


@pytest.mark.parametrize("changes", [
    {"manufacturing_project": ""},
    {"manufacturing_project": "demo-producttrackr"},
    {"manufacturing_database": ""},
    {"manufacturing_backup_storage": "local"},
    {"root_id": ""},
    {"root_id": "sales-backups"},
    {"token_json": ""},
    {"storage_backend": "drive"},
    {"manufacturing_project": "different-project"},
    {"manufacturing_database": "different-database"},
    {"manufacturing_admins": ()},
    {"manufacturing_workspace": "salestrackr"},
])
def test_production_requires_isolated_workspace_and_durable_storage(production, changes):
    production.validate_manufacturing()
    with pytest.raises(RuntimeError):
        replace(production, **changes).validate_manufacturing()


def test_production_rejects_emulators_even_for_the_drive_sales_backend(production, monkeypatch):
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8085")
    with pytest.raises(RuntimeError, match="emulator"):
        production.validate_manufacturing()


def test_production_shares_database_with_separate_workspace_and_runtime_credentials(production):
    client = Mock()
    workspace = client.collection.return_value.document.return_value
    workspace.id = "producttrackr"
    workspace.get.return_value.to_dict.return_value = {
        "generation": "production-generation", "productSearchVersion": 1, "activeDataset": "legacy",
        "namingVersion": 1,
    }
    with patch("backend.app.manufacturing_storage.firestore.Client", return_value=client) as factory:
        repository = ManufacturingRepository(production)
    factory.assert_called_once_with(project="example-project", database="(default)", credentials=None)
    assert repository.meta()["mode"] == "production"
    assert repository.workspace.id == production.manufacturing_workspace
    with patch("backend.app.manufacturing_storage.firestore.Client") as factory:
        assert ManufacturingRepository(production, client=client).client is client
    factory.assert_not_called()


def test_producttrackr_routes_still_require_verified_iap_identity(production):
    sales = Repository(LocalBackend(production.data_dir))
    with TestClient(create_app(production, sales)) as client:
        assert client.get("/api/manufacturing/meta").status_code == 401
        assert client.get("/api/manufacturing/meta", headers={
            "X-Goog-Authenticated-User-Email": "product-admin@example.test",
        }).status_code == 401
        assert client.get("/healthz").status_code == 200


def test_producttrackr_admins_do_not_grant_sales_administrators_access(production):
    settings = replace(production, recovery_admins=("sales-admin@example.test",))
    for service in (ManufacturingBackupManager, SpreadsheetExport):
        instance = object.__new__(service)
        instance.settings = settings
        assert instance.admin("product-admin@example.test")
        assert not instance.admin("sales-admin@example.test")
        assert not instance.admin("ordinary-user@example.test")


def service(**values):
    values = {
        "APP_MODE": "production", "GOOGLE_FOLDER_ID": "sales-root",
        "BACKUP_DRIVE_FOLDER_ID": "sales-backups", "PUBLIC_ORIGIN": "https://app.example.test",
        "RECOVERY_ADMIN_EMAILS": "sales-admin@example.test", **values,
        "STORAGE_BACKEND": values.get("STORAGE_BACKEND", "firestore"),
        "FIRESTORE_PROJECT_ID": values.get("FIRESTORE_PROJECT_ID", "example-project"),
    }
    return {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": key, "value": val} for key, val in values.items()
    ] + [{"name": "GOOGLE_TOKEN_JSON", "valueFrom": {
        "secretKeyRef": {"name": "sales-oauth", "key": "4"},
    }}]}]}}}}


def test_deployment_overlay_reuses_sales_storage_without_separate_drive_configuration():
    before = service()
    retained = deepcopy(before)
    result = production_environment("example-project", before, "product-admin@example.test")
    assert before == retained
    assert result["PRODUCTTRACKR_BACKUP_STORAGE"]["value"] == "drive"
    assert result["PRODUCTTRACKR_FIRESTORE_DATABASE_ID"]["value"] == "(default)"
    assert "PRODUCTTRACKR_BACKUP_DRIVE_FOLDER_ID" not in result
    assert "PRODUCTTRACKR_DRIVE_TOKEN_JSON" not in result
    assert "GOOGLE_TOKEN_JSON" not in result and "GOOGLE_FOLDER_ID" not in result
    assert result["PRODUCTTRACKR_ADMIN_EMAILS"]["value"] == "product-admin@example.test"


@pytest.mark.parametrize("values", [
    {"APP_MODE": "demo"},
    {"FIRESTORE_EMULATOR_HOST": "127.0.0.1:8085"},
    {"GOOGLE_FOLDER_ID": "sales-backups"},
    {"STORAGE_BACKEND": "drive"},
    {"FIRESTORE_PROJECT_ID": "another-project"},
    {"FIRESTORE_WORKSPACE_ID": "producttrackr"},
    {"RECOVERY_ADMIN_EMAILS": ""},
    {"PRODUCTTRACKR_ENABLED": "true", "PRODUCTTRACKR_WORKSPACE_ID": "other-product-workspace"},
])
def test_deployment_rejects_unsafe_or_unconfigured_targets(values):
    with pytest.raises(ValueError):
        production_environment("example-project", service(**values))


def test_deployment_rejects_unpinned_secret_versions():
    existing = service()
    existing["spec"]["template"]["spec"]["containers"][0]["env"][-1]["valueFrom"]["secretKeyRef"][
        "key"
    ] = "latest"
    with pytest.raises(ValueError):
        production_environment("example-project", existing)


def test_database_rules_are_deployed_only_to_the_named_database():
    session = Mock()
    source = (CONFIG.parent / "firestore.rules").read_text(encoding="utf-8")
    session.get.side_effect = [
        Mock(status_code=404),
        Mock(status_code=200, json=lambda: {"rulesetName": "projects/example-project/rulesets/verified"}),
        Mock(json=lambda: {"source": {"files": [{"name": "firestore.rules", "content": source}]}}),
    ]
    session.post.return_value.json.return_value = {"name": "projects/example-project/rulesets/verified"}
    database_rules(session, "example-project", "producttrackr")
    assert session.post.call_args.kwargs["json"]["name"] == (
        "projects/example-project/releases/cloud.firestore/producttrackr"
    )
    assert session.get.call_args_list[0].args[0].endswith("/cloud.firestore/producttrackr")


def test_an_existing_database_rule_policy_is_not_silently_overwritten():
    session = Mock()
    session.get.side_effect = [
        Mock(status_code=200, json=lambda: {"rulesetName": "projects/example-project/rulesets/existing"}),
        Mock(json=lambda: {"source": {"files": [{"name": "firestore.rules", "content": "different"}]}}),
    ]
    with pytest.raises(RuntimeError, match="differ"):
        database_rules(session, "example-project", "producttrackr")
    session.post.assert_not_called()


def test_shared_database_uses_the_default_rules_release_without_replacing_it():
    session = Mock()
    source = (CONFIG.parent / "firestore.rules").read_text(encoding="utf-8")
    session.get.side_effect = [
        Mock(status_code=200, json=lambda: {"rulesetName": "projects/example-project/rulesets/shared"}),
        Mock(json=lambda: {"source": {"files": [{"name": "firestore.rules", "content": source}]}}),
    ]
    database_rules(session, "example-project", "(default)")
    assert session.get.call_args_list[0].args[0].endswith("/releases/cloud.firestore")
    session.post.assert_not_called()


def test_shared_database_product_writes_and_backups_leave_sales_workspace_unchanged(tmp_path):
    if not os.getenv("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("Requires the explicitly configured local Firestore emulator.")
    namespace = uuid4().hex
    settings = Settings(
        "demo", tmp_path, "", "", "", "", False,
        storage_backend="firestore", firestore_project="demo-shared-producttrackr",
        firestore_workspace=f"sales-{namespace}", manufacturing_enabled=True,
        manufacturing_project="demo-shared-producttrackr", manufacturing_workspace=f"products-{namespace}",
    )
    sales = FirestoreRepository(settings)
    with TestClient(create_app(settings, sales)) as client:
        before = sales.workspace.get().to_dict()
        product = create(client, "products", name="Independent product", partNumber="PART-001")
        repository = ManufacturingRepository(settings)
        manager = ManufacturingBackupManager(repository, settings)
        backup = manager.backup(uuid4().hex, "local-demo")
        content, _ = manager.get(backup["id"])
        data, _ = manager.read_archive(content)
        assert data["products"][product["id"]]["name"] == "Independent product"
        assert sales.workspace.get().to_dict() == before
        assert sales.query_accounts(limit=1)["total"] == 0
