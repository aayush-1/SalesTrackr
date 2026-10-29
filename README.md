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
- Product stock and minimum quantities, quantity-bearing sales parts, and an **Order in hand shortage** report combining demand before Dispatch.
- ProductTrackr **Low stock**, audited manual stock editing, and manufacturing quantities credited when the entire job completes.
- **Feedback pending** lists closed, non-discarded records whose feedback is not yet received, including existing closed records. Confirming feedback removes a record from this view without changing its Closed stage or payment state.
- Stage tabs show only the current status: **New leads**, **Follow-ups**, **Order in hand**, **Dispatched**, **Delivered**, and **Closed**. Payment pending and Payment received remain separate views.
- **Lead acquired date** filters by day/month/range in every view, showing those leads' current progress today. **Current sales stage** lists only relevant stages and is hidden on single-stage tabs.
- Sales tables are ordered by **lead-acquired date, newest first**, with a visible descending indicator. Note edits and stage changes do not reorder them by last-update time.
- **Discard record** works at any stage, including Closed, and requires a remark. Discarded records become read-only and remain in **Discarded** and **All sales records**, but leave stage/payment queues and their counts. The discard date, remark, original stage/payment state, and history are retained; nothing is deleted.
- React/TypeScript UI, FastAPI backend, a local demo, Drive storage adapter, and a Cloud Run Dockerfile.
- An opt-in Firestore backend and **Settings -> Backup and recovery** local preview, with verified
  plain ZIP backups, ten-copy rotation, and full-workspace recovery.
- **Settings -> Sales record cleanup** lets recovery administrators permanently delete discarded records
  or closed records with feedback received, including their saved history. Accounts, contacts and
  existing backups are preserved; deletion requires selecting records and typing `DELETE`.

**Received** confirms payment and automatically closes an already-delivered sale. Payment can be received before dispatch. Closing requires delivery and payment receipt; stock is checked at Dispatch. Payment notes can be saved independently. This replaces the earlier amount-based payment UI; historical amounts remain in saved versions.

Feedback is tracked separately after closure. Undo feedback receipt before reopening a record to correct its stage; the previous feedback confirmation remains in history. Discarded records retain their feedback state and are read-only.

For newly created shared-workspace sales, Dispatch deducts each product's combined part quantity
atomically or fails without changing the sale or stock. Delivered and Closed do not deduct again.
Returning to Order in hand or earlier returns the deducted quantities; returning only to Dispatched
does not. Dispatch retries and sent-part corrections reconcile the difference without duplicate changes.
Existing records remain untracked and are not replayed or bulk-migrated.

Existing Quotation pending records appear as New lead, and Quotation sent records appear as Follow-up. An existing Follow-up date takes precedence; otherwise its date comes from the retired Quotation sent milestone. Later stages and quotation references stay intact, and original milestone dates remain in immutable history. No Quotation sent or Awaiting delivery tab is needed.

No Excel import, amount/balance tracking, billing/invoice generation, refunds/payment ledger, formal order-cancellation processing, or performance dashboard. Discarding is a record flag, not a refund or deletion; restoring discarded records is not implemented. The Excel workbook informed the workflow; its customer data is not included.

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

## Shared Settings and combined recovery (local preview)

The sidebar gear opens the same `/settings` page from either Trackr, with **General**,
**Backup and recovery**, **Sales record cleanup**, **Manufacturing record cleanup**, and
**Products spreadsheet export** tabs. Old `/manufacturing/settings` links redirect there.
Sales records can link catalog parts and optional customer machines. Manufacturing jobs retain
their independent workflow; sales deliveries do not change manufacturing progress.

Shared Settings and combined recovery are the only configuration when both Trackrs are present;
there is no feature flag or separate recovery page. For local preview, add these variables to the
Firestore local configuration above. Keep the two workspace IDs distinct:

```powershell
$env:PRODUCTTRACKR_ENABLED = "true"
$env:FIRESTORE_PROJECT_ID = "demo-producttrackr-local"
$env:PRODUCTTRACKR_FIRESTORE_PROJECT_ID = "demo-producttrackr-local"
$env:FIRESTORE_WORKSPACE_ID = "salestrackr"
$env:PRODUCTTRACKR_WORKSPACE_ID = "producttrackr"
$env:DEMO_SEED = "false"
$env:DEMO_DATA_DIR = ".local-data\shared-preview"
```

Combined ZIPs are saved locally in `DEMO_DATA_DIR\shared-backups`. Each contains
`manifest.json`, `salestrackr.zip`, and `producttrackr.zip`, with integrity checks for both
complete histories. Recovery validates both, creates a combined safety backup, stages both
datasets, and activates both in one Firestore transaction. Changes in either workspace
invalidate a preview; old forms in both apps must reload. Both cleanup tabs create a verified
combined safety backup before deletion. Scheduled backups use the same combined format.

Both workspaces must use the same Firestore project/database. Production recovery
requires administrator permission for both. Separate legacy archives are retained untouched and
are not rotated or accepted by combined recovery: a single-app backup must not erase the other
workspace. The nested child archives remain readable by the existing offline tools. Drive folder
links are included, but actual drawings/files stay in Drive; existing verified connections retain
precedence over recovered copies.

The intended shared Drive layout uses only `GOOGLE_FOLDER_ID` and `GOOGLE_TOKEN_JSON`:
`SalesTrackr / Backups` (the dedicated `BACKUP_DRIVE_FOLDER_ID`),
`SalesTrackr / Products / <part number - product name>`, and
`SalesTrackr / ProductTrackr Directory.xlsx`. There is no separate ProductTrackr root/token
configuration. Demo backups and exports stay local; do not inject Drive tokens for this preview.

**No production migration is performed by enabling the local preview.** Before production
cutover, preserve independent backups and verify SalesTrackr authorization can access the existing
managed files. Move the existing Products folder and workbook without changing their IDs, register
the moved Products folder on the SalesTrackr root, update the workbook's saved root reference, and
retain old archives. Verify all existing product URLs, workbook links, a combined backup and a
recovery before removing the old root or revoking its authorization. Do not deploy this change
to production until those migration checks are complete.

## Models, account machines, and parts sent

Manage **Models** in SalesTrackr. An account can have multiple named machines of the same model;
serial number and manufacturing year are optional. Products fit one or more models, or **Common**
for every model. An empty model selection means Common.

Sales records accept optional catalog parts, each optionally assigned to a machine at that account.
The first closure publishes them to **Sales History** using the Delivered date. Closed-sale
part/date corrections update those deliveries with audit history. Reopening retains deliveries
while Delivered remains; undoing/reinstating delivery withdraws/reactivates them without duplicates.
Deleting a sales record does not delete its independently retained deliveries.

**Add past delivery** records missed deliveries without creating a sale. Corrections and voids require
a reason. **Sales History** filters by account, machine, and date. **Potential Spares machine wise** shows
compatible products, green sent status, and the latest delivery date. A delivery without a machine
counts for every compatible machine at that account; an assigned delivery counts only for that machine.
**Create PDF** downloads all matching results, not just the current page. Sales History exports account,
machine, date-range and withdrawn-entry filters; Potential Spares machine wise exports compatible
parts for the selected account and machine, including delivery status and last-sent dates.

ProductTrackr's **Master Product's List** tab lists product names, part numbers, and notes for the selected
model, including Common products, with **Create PDF** for the complete selected model's list.
**Order in hand shortage** is available in both workspaces (above Low stock
in ProductTrackr); its sales breakdown links show company names.

New sale parts require positive whole-unit quantities. Manual past deliveries may record a known
quantity but never change current stock. Historical unknown quantities remain unknown.
Free-text alternatives to catalog parts are not supported.

Combined backups include models, machines, structured sales parts, and every delivery/audit version.
The existing ZIP structure and format numbers are unchanged. The Products workbook adds **Models**,
showing model names or Common, plus **Stock quantity** and **Minimum stock quantity**, while retaining
its existing Drive links and managed file identity. Combined backups also preserve inventory source
positions and stock movement audit; restoration does not replay those movements.

For extensive synthetic data, use the local emulator/shared configuration above with separate namespaces:

```powershell
$env:FIRESTORE_PROJECT_ID = "demo-machine-sales-local"
$env:PRODUCTTRACKR_FIRESTORE_PROJECT_ID = "demo-machine-sales-local"
$env:FIRESTORE_WORKSPACE_ID = "machine-sales-preview"
$env:PRODUCTTRACKR_WORKSPACE_ID = "machine-products-preview"
$env:DEMO_DATA_DIR = ".local-data\machine-sales-preview"
$env:PRODUCTTRACKR_BACKUP_STORAGE = "local"
$env:GOOGLE_TOKEN_JSON = ""
.\.venv\Scripts\python.exe -m scripts.seed_machine_sales_demo
```

The seeder refuses non-local emulator targets and nonempty workspaces. A completed preview is retained
on subsequent runs. Its `scenarios.json` identifies draft/closed/corrected/reopened/withdrawn/reinstated
sales, late-added parts, manual corrections/voids, and retained history after source deletion.
Existing local workspaces and production data are not changed.

The inventory preview uses `scripts.seed_inventory_demo` with project `demo-inventory-local`,
Sales workspace `inventory-sales-local`, Product workspace `inventory-products-local`, and local
data directory `.local-data\inventory-local`. It requires the same credential-free localhost
emulator configuration. It includes competing orders, repeated machine lines, stock thresholds,
manufacturing output and blocked undo, corrections/re-dispatch, manual counts, historical deliveries,
and deleted-source retention. A combined backup and directory workbook are generated locally.
Existing nonempty workspaces are never overwritten.

## ProductTrackr local workspace

ProductTrackr is an independent manufacturing workspace at `/manufacturing`,
with Products, Operations, Suppliers and Contacts. Products use **Part number** (`partNumber`)
throughout the UI, API, stored entities, backups and spreadsheets. Stable internal IDs remain unchanged.
Each Product defines ordered operations
and linked supplier choices, with exactly one primary when links exist. An optional contact
must belong to that supplier's company. Individuals are suppliers directly, and in-house work
uses an internal company supplier. These process links do not start real work.

Manufacturing records at `/manufacturing/records` track individual jobs. Create a record from a
saved Product to copy its operation route, then explicitly assign/start and complete each
operation in sequence. Multiple jobs for the same Product advance independently. Copied routes
and actual assignment labels stay unchanged by later directory edits; new assignments use the
source step's currently linked suppliers. The overview shows work in process, waiting jobs,
current suppliers and completed jobs. New jobs require a quantity; only final completion credits
finished stock. Reasoned quantity corrections reconcile the difference; undoing completion is blocked
if it would make stock negative. Costing and supplier notifications are not included.

**Supplier work**, directly below Manufacturing records in navigation, opens a searchable,
paginated sheet at `/manufacturing/supplier-work`. It shows one entry per supplier, with counts of
in-progress jobs and products. Open a supplier to see its current manufacturing jobs, including
contact, product, part number, current operation and a link to each job.
Awaiting-assignment, completed and cancelled jobs are excluded. A preview appears below Current
work on the overview.

Reassignment and cancellation require a reason. The last completion can be undone only before
the next operation starts (or to reopen the final operation of a completed record). Corrections
append to paginated activity history rather than erasing earlier events. Actual work dates
cannot be future-dated or contradict the operation sequence. Saves use optimistic versions and
idempotent receipts, including record-number allocation and append-only events.

ProductTrackr is an opt-in, separate manufacturing workspace. For local testing, start the
localhost emulator described above, then add these variables before starting the application:

```powershell
$env:APP_MODE = "demo"
$env:FIRESTORE_EMULATOR_HOST = "127.0.0.1:8085"
$env:PRODUCTTRACKR_ENABLED = "true"
$env:STORAGE_BACKEND = "firestore"
$env:FIRESTORE_PROJECT_ID = "demo-producttrackr-local"
$env:PRODUCTTRACKR_FIRESTORE_PROJECT_ID = "demo-producttrackr-local"
$env:PRODUCTTRACKR_WORKSPACE_ID = "producttrackr"
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:create_app --factory --host 127.0.0.1 --port 8000
```

For production, follow [ProductTrackr's first-deployment setup](docs/cloud-run-deployment.md#producttrackr-production).
The deployment workflow uses SalesTrackr's `(default)` database with a separate `producttrackr`
workspace and SalesTrackr's shared private Drive root. Both workspaces share the database's free quota.
Local backups and spreadsheet exports remain on disk; production backups and exports use Drive.

These settings do not change SalesTrackr's storage backend, project or workspace. Manufacturing
uses dedicated collections, API routes, generation headers and idempotent write receipts.
Product step IDs remain stable when reordered. Mutations atomically validate relationships
and reject stale versions. No manufacturing master-record deletion is exposed.

Data survives browser refreshes and application restarts while the same emulator data remains.
Resetting or stopping an ephemeral emulator can erase it. Shared backups include SalesTrackr
and ProductTrackr together at `/settings?tab=backup`. The nested ProductTrackr ZIP contains every master entity, product
step, supplier link, manufacturing record, copied route/progress, activity event, numbering
counter, write receipt and workspace preference, including unused directory entries.
New ProductTrackr archives use format version 3 with `suppliers.json`, `supplier-links.json`,
`supplierId` and `partNumber`. Versions 1 and 2 remain readable and are converted only after their
original checksums are verified. Existing workspaces are migrated into a verified dataset with
the current names before an atomic activation; the original dataset is retained and old forms
must reload. Contacts, supplier links, assignment history, part numbers and product Drive links
keep their existing identities and values.
Credentials, environment configuration and backup catalogs are excluded. Configuration uses
`PRODUCTTRACKR_*` variables and request identity uses `X-ProductTrackr-*` headers.

Combined local archives stay under `DEMO_DATA_DIR\shared-backups`. They survive emulator resets
but are not uploaded to Drive. In production, `SalesTrackr / Backups` is the dedicated backup
folder; only combined ZIPs tagged `app=trackr-workspace-backup` are rotated. Legacy and unrelated
files stay untouched. Archives are readable, not encrypted. `GOOGLE_TOKEN_JSON` is the single
Drive authorization, injected from Secret Manager, never committed. Drive failures never fall
back to local storage. Both the shared root and managed files must remain private.
The directory spreadsheet stays directly in SalesTrackr, not inside Backups.

**Product drawings & files** are available from each product page's **Open Drive folder** link.
When `GOOGLE_FOLDER_ID` and `GOOGLE_TOKEN_JSON` are set, adding a product
automatically creates its own folder under `SalesTrackr / Products`. Opening its page also
reconnects or retries that same folder. A Drive outage never rolls back the saved product;
creation returns a `folderWarning` and its product page offers a retry. This is a real Drive
connection even when ZIP backups are local. With no Drive configuration, the page explains
how to enable files rather than creating a local imitation.
Existing app-managed product folders directly under the root are moved into Products while
retaining their folder IDs and contents. Folders initially use `Part number - Product name`
(or the product name alone). Their stable
Drive IDs, workspace/product tags and workspace-level `productFolders` mapping prevent duplicate
folders across refreshes, concurrent opens, lost responses and restores in the same workspace.
Changing a product's name or part number also updates its Drive folder title to
`Part number - Product name`, preserving the same folder ID, link and existing files.
If Drive is unavailable, the product edit remains saved and its page can retry the title update.
If a linked folder was moved, deleted or made inaccessible, restore its location/access;
the application does not silently create a replacement. The root's name is left unchanged.
Combined ZIPs use Trackr names; the spreadsheet retains its ProductTrackr name. No sharing permissions are changed.

Upload drawings, specifications and other files directly in Drive. ZIP backups do **not**
contain these files, and restore/record cleanup never deletes them. New ZIPs include
`product-folders.json`, with each connected product's folder ID, URL, parent and original workspace/root.
Existing workspace-level folder mappings are never replaced by a restore. Verified restored
copies reconnect missing mappings when the product's files are opened, preserving the original
folder ID; matching Drive tags remain a fallback. Legacy child archives remain readable offline.
Keep the same configured project, database, workspace and Drive root when recovering.

Manual and pre-restore backups share one ten-copy retention limit. Rotation follows successful
read-back verification; failed backups retain older recovery copies. No weekly scheduler is
configured. Restore accepts a selected archive or uploaded combined ZIP, validates all
relationships/history/checksums, and requires an expiring preview plus typed `RESTORE`.
Separate single-Trackr archives are rejected. Both workspaces are first backed up and verified;
both restored datasets are staged and read back, then both live pointers/generations switch atomically.
Stale forms cannot save; completed retries reconcile the original operation. Previous datasets
remain archived rather than being activated independently.
Existing flat local collections require no up-front migration.

**Settings > Manufacturing record cleanup** removes only selected completed manufacturing jobs
(up to 50 per page). Active and cancelled jobs are protected; no automatic deletion
runs. Type `DELETE` to confirm. Cleanup first creates and physically verifies a full
workspace backup, then removes each selected job, its copied route, activity events
and corresponding action receipts. Shared directories, product-process links and
the manufacturing numbering counter are unchanged. One small deletion-protection
receipt remains per removed job, preventing old create retries from resurrecting it.
Existing backup ZIPs can still contain deleted jobs until ten-copy rotation removes
them; recovery requires a full workspace restore, not a per-job undo.

Interrupted cleanup is persisted and can be resumed from the same Settings tab,
including after restarting the app. Writes, new backups and restores are fenced
until a partially completed cleanup finishes. Every deletion batch is checked
against its verified backup and current dataset/maintenance ownership. This frees
database storage, not a configured application RAM limit. SalesTrackr is unaffected.

**Settings > Products spreadsheet export** creates or updates one `ProductTrackr Directory.xlsx`.
The Products-first workbook includes ordered Product operations, all Supplier links,
Operations, Suppliers and Contacts, plus export information. Stable internal keys and
clickable cross-sheet references preserve relationships, including primary suppliers,
optional company contacts, repeated operations and unlinked directory entries.
The Products sheet includes **Models** (Common or linked model names) and a clickable **Drive folder** column for saved folder
connections, including restored links. Products without a connected folder have a blank cell;
exporting never creates folders or downloads their contents.
Part number is user-entered; internal keys disambiguate duplicate labels.
Phone numbers and formula-like text are exported literally, not executed as formulas.
Product operations also lists every available supplier name, with the primary marked;
Supplier links retains one row per option. Technical key columns remain in the XLSX but
are hidden by default. They are stable join IDs, not additional user-entered part numbers.

**Open spreadsheet** opens the managed Google Drive file in a new browser tab.
There is no in-app spreadsheet viewer. The button is enabled only after a verified
Drive export; local-only exports keep it visible but disabled and provide a download instead. The app also explains
the individual key columns.

Local backup mode produces a downloadable workbook under
`.local-data\producttrackr-spreadsheets` (or the configured demo data directory).
Drive backup mode updates one app-tagged XLSX in the same configured private
SalesTrackr root, retaining its file ID. The workbook can be opened in Excel or
Google Sheets; it is not a native Google Sheets document. No Sheets API or new OAuth
scope is required. The export never updates unrelated files or changes sharing.
Missing, moved, replaced or publicly shared managed Drive workbooks fail closed.
An export is serialized with recovery/cleanup and verified by physical readback.

The workbook is a one-way snapshot: the next export replaces all workbook contents,
including manual edits. Manufacturing jobs, events, numbering and write receipts
are excluded; use ZIP backups for full recovery. A report is limited to 200,000 cells
and 32 MiB; oversized or unrepresentable values fail without truncating the report.
No scheduled exports run, and the local preview performs no real Drive writes.

Deploy only after the existing Drive files are migrated and their links verified. This local
preview does not move production files, revoke authorization or provision scheduling.

After building the frontend, run real manufacturing browser checks with the emulator running:

```powershell
Set-Location frontend
npm exec -- playwright test --config=playwright.manufacturing.config.ts
```

The manufacturing suite starts its own server on port 8014 and uses a unique demo workspace.
The standard browser suite continues to work without an emulator; its workspace-layout tests
mock only the manufacturing read endpoints.

## Deployment status

The application and deployment assets are implemented. Local API/browser checks run without cloud credentials. Actual Drive access, an image build with Docker/Cloud Build, and an IAP-protected cloud deployment require your Google project and credentials and are **not** implied by local demo success. Review the local UI before deployment.

The agreed scope and original implementation sequence are in [PLAN.md](PLAN.md).
