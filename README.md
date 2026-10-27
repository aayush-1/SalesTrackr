# SalesTrackr

A private sales workspace: one customer account, one record per enquiry, and a dated journey from new lead to delivery and payment confirmation.

## Included

- Searchable account list and contact people, including inline account creation.
- Sales records headed by company name and location (from the account's location/address), with product notes instead of an enquiry title.
- A dedicated **Edit** button beside product/requirement notes, alongside the top **Edit details** action for the full record.
- Six dated stages: **New lead -> Follow-up -> Order in hand -> Dispatched -> Delivered -> Closed**.
- Optionally enter a quotation number when moving directly from New lead to Follow-up; add, edit, or clear it later.
- Green reached stages, optional actual dates, automatic recording timestamps, and correction history.
- A payment-details box with a **Received** button; no order amounts, balances, partial-payment inputs, or payment columns in sales tables.
- A feedback box below Payment with a **Feedback received** button, disabled until the record is closed. Confirmation saves the received date and remains in history; **Undo feedback received** corrects a mistaken click.
- A separate **Stock update** box between Payment and Feedback. **Stock updated** becomes available at Order in hand, records the confirmation date and keeps history. Stock confirmation is required before dispatch, not as a separate closure condition. It does not update inventory quantities or advance stages automatically.
- **Stock update pending**, directly below Feedback pending, lists non-discarded confirmed orders whose stock update is not confirmed, including existing dispatched, delivered and closed records.
- **Feedback pending** lists closed, non-discarded records whose feedback is not yet received, including existing closed records. Confirming feedback removes a record from this view without changing its Closed stage or payment state.
- Stage tabs show only the current status: **New leads**, **Follow-ups**, **Order in hand**, **Dispatched**, **Delivered**, and **Closed**. Payment pending and Payment received remain separate views.
- **Lead acquired date** filters by day/month/range in every view, showing those leads' current progress today. **Current sales stage** lists only relevant stages and is hidden on single-stage tabs.
- Sales tables are ordered by **lead-acquired date, newest first**, with a visible descending indicator. Note edits and stage changes do not reorder them by last-update time.
- **Discard record** works at any stage, including Closed, and requires a remark. Discarded records become read-only and remain in **Discarded** and **All sales records**, but leave stage/payment queues and their counts. The discard date, remark, original stage/payment state, and history are retained; nothing is deleted.
- React/TypeScript UI, FastAPI backend, a local demo, Drive storage adapter, and a Cloud Run Dockerfile.
- An opt-in Firestore backend and **Settings -> Backup and recovery** local preview, with verified
  plain ZIP backups, ten-copy rotation, and full-workspace recovery.
- **Settings -> Record cleanup** lets recovery administrators permanently delete discarded records
  or closed records with feedback received, including their saved history. Accounts, contacts and
  existing backups are preserved; deletion requires selecting records and typing `DELETE`.

**Received** confirms payment and automatically closes an already-delivered sale. Payment can be received before dispatch, independently of stock confirmation. Closing still requires delivery and payment receipt; stock is checked when dispatching instead. Payment notes can be saved independently. This replaces the earlier amount-based payment UI; historical amounts remain in saved versions.

Feedback is tracked separately after closure. Undo feedback receipt before reopening a record to correct its stage; the previous feedback confirmation remains in history. Discarded records retain their feedback state and are read-only.

Stock update is tracked separately once an order is confirmed. Dispatch is blocked
until stock is updated; the dispatch date cannot precede the stock confirmation.
Existing later-stage records without the new fields remain valid and show Pending;
their history and stage dates are not rewritten or bulk-migrated. The flag and date
are stored on the next save. Correct a dispatched record back to Order in hand
before undoing stock confirmation. Undo stock confirmation before removing order
confirmation. Discarded records retain
their stock state and remain read-only. Firestore updates its derived queue indexes
per dataset/version, without rewriting revision payloads, stage dates or history.
The index marker also tracks the workspace sequence so an in-flight save from the
previous application revision is repaired safely during deployment.

Existing Quotation pending records appear as New lead, and Quotation sent records appear as Follow-up. An existing Follow-up date takes precedence; otherwise its date comes from the retired Quotation sent milestone. Later stages and quotation references stay intact, and original milestone dates remain in immutable history. No Quotation sent or Awaiting delivery tab is needed.

No product catalogue, Excel import, amount/balance tracking, billing/invoice generation, refunds/payment ledger, formal order-cancellation processing, or performance dashboard. Discarding is a record flag, not a refund or deletion; restoring discarded records is not implemented. The Excel workbook informed the workflow; its customer data is not included.

## Run locally

Requires Node.js 22+ and Python 3.12+ (3.13 recommended). Run from the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
$env:APP_MODE = "demo"
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. The demo uses synthetic accounts and persists changes in ignored `.local-data` files. It does **not** access Google Drive or authenticate other local users; do not use it for real customer data on a shared machine. Use `DEMO_SEED=false` and a new `DEMO_DATA_DIR` for an empty workspace. Demo mode is forbidden on Cloud Run.

For frontend hot reload, keep the API running and use a second terminal:

```powershell
npm --prefix frontend run dev
```

Open http://127.0.0.1:5173. Vite proxies API requests to port 8000.

If this machine's Python was provisioned during implementation, the existing `.\.venv\Scripts\python.exe` can be used directly; `.tools` and `.venv` are local tools, not deployment inputs.

## Google Drive and Cloud Run

Start with [Google Drive setup](docs/google-drive-setup.md), then [Cloud Run deployment](docs/cloud-run-deployment.md). Production requires an owner's OAuth credential in Secret Manager, the app-created folder ID, and IAP-protected operator access.

Repository updates use [SalesTrackr CI and Deploy](https://github.com/aayush-1/SalesTrackr/actions/workflows/deploy.yml). Pull requests run checks without cloud access. Application changes reaching `main` deploy after checks pass; **Run workflow** on `main` also deploys. Documentation-only pushes skip deployment. See [repository deployment setup](docs/cloud-run-deployment.md#repository-driven-updates) for the required keyless identity and repository variables.

The one-time owner consent is separate from signing into the app. Access tokens refresh automatically, but revoked authorization requires reconnection.

**Storage:** one readable JSON file per account, contact, or sales record. Saves update the same Drive file; previous snapshots stay in its embedded `history`. Conditional ETag updates reject overlapping stale saves instead of silently overwriting them. Lists use a rebuildable index and one shared, reused checkpoint file. Do not manually edit the underlying JSON. Full-history files grow with use, and Drive is not a multi-record transactional database. See [architecture, migration, and recovery](docs/architecture.md).

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check backend scripts
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix frontend exec -- playwright install chromium
npm --prefix frontend test
```

The browser suite builds the UI, starts an isolated local API on port 8011, exercises desktop/mobile flows, and writes screenshots into ignored `frontend\test-results`. Its synthetic data is isolated in `.local-data\e2e`.

Runtime dependencies are pinned in `requirements.txt`; frontend dependencies are pinned in `frontend\package-lock.json`.

## Firestore and recovery preview

Production storage remains Drive until an explicit, verified cutover. `STORAGE_BACKEND=firestore` selects
the new backend; leaving it unset preserves existing behavior. Local Firestore uses synthetic data and
a localhost emulator, never a live Google project. Install the Google Cloud Firestore emulator component
and Java 25, then start it in a separate terminal:

```powershell
gcloud emulators firestore start --host-port=127.0.0.1:8085 --project=demo-salestrackr-local
```

After building the frontend, run:

```powershell
$env:APP_MODE = "demo"
$env:STORAGE_BACKEND = "firestore"
$env:FIRESTORE_PROJECT_ID = "demo-salestrackr-local"
$env:FIRESTORE_EMULATOR_HOST = "127.0.0.1:8085"
$env:DEMO_DATA_DIR = ".local-data\firestore-preview"
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/settings**. Demo backups are ordinary ZIP files in the isolated local
backup folder, not uploads to Google Drive. They survive a server restart; the emulator itself is
ephemeral. Weekly preferences can be reviewed locally, but no cloud schedule is running.
Settings is the small gear beside **Sales workspace** at the bottom of the sidebar. New ZIP backups
contain readable `accounts.json`, `contacts.json`, `sales-records.json` and a verified manifest, with
current data and full history. Keep only ten backups total, with no protected-original exception.
Production lists valid accessible archives from the private Drive backup folder; local preview
uses its isolated sample backup folder instead.
Production requires `BACKUP_DRIVE_FOLDER_ID` for the separately marked Firestore backup folder.
There is no fallback to the existing Drive backup/data folders; migration leaves those files intact.

With the emulator running, set `FIRESTORE_EMULATOR_HOST` as above and run the backend checks to include
the real-emulator integration suite. CI starts an emulator and runs these tests automatically.
See [Firestore layout, backup safety, and offline recovery](docs/architecture.md#firestore-backend-and-recovery).

## Deployment status

The application and deployment assets are implemented. Local API/browser checks run without cloud credentials. Actual Drive access, an image build with Docker/Cloud Build, and an IAP-protected cloud deployment require your Google project and credentials and are **not** implied by local demo success. Review the local UI before deployment.

The agreed scope and original implementation sequence are in [PLAN.md](PLAN.md).
