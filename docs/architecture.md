# Architecture and operating boundaries

## Request path

Browser -> IAP -> FastAPI -> Drive API. FastAPI also serves the Vite production assets.

`APP_MODE=demo` uses synthetic local JSON storage and a localhost-only identity. `APP_MODE=production` requires Drive configuration, IAP audience, and an HTTPS public origin. Cloud Run rejects demo mode at startup.

Sources:

- `backend\app\domain.py`: models, dates, workflow, payment, and follow-up rules.
- `backend\app\main.py`: validated mutation/read APIs and static serving.
- `backend\app\storage.py`: one-file documents, embedded versions, conditional saves, Drive Changes/checkpoint.
- `backend\app\queries.py`: the shared predicates for cards and lists.
- `frontend\src\pages`: account, sales, collection, history, and conflict screens.

## Fetch and save efficiency

`GET /api/records/{id}/detail` returns the sales record and its linked account/contact after one forced synchronization. Missing or conflicting linked entities are reported individually without hiding the sale. Historical versions still load through the separately validated history endpoint.

Mutation responses indicate whether the saved revision is still current and include linked details for sales records. The UI can reuse a complete, warning-free confirmation to update the matching detail and synchronization status without immediately fetching them again. Superseded retries, incomplete synchronization, warnings, unknown response flags, and manual refresh all retain normal server revalidation. Lists and history still refresh after saves; an unrelated or newer detail is never replaced with an older save result.

Existing-entity edits generate a cryptographic operation UUID in the browser instead of calling `/writes/prepare`. New entities still prepare and reserve a Drive file ID; browsers without UUID support keep the preparation flow. The same operation ID and unchanged body are retained across uncertain retries, and repeated clicks cannot submit overlapping saves. Server-side actor, fingerprint, base-revision and duplicate-operation checks remain authoritative.

Drive versioned reads obtain the resource ETag and Drive's content checksum together, then verify the raw downloaded bytes against that checksum before validating the canonical document. This saves the second metadata request without substituting the unrelated media ETag for the resource ETag. If no checksum is available, the original before/after ETag check remains. Updates still use v2 `If-Match` with automatic retries disabled.

Drive change replay downloads a repeated file only once for the same reported Drive version within that replay. A successful write's returned file/version can also supply the exact just-written document during the next replay, avoiding its redundant download. This is bounded to one pending document, consumed by one replay, and discarded even if synchronization fails or the change is not yet visible. Different or missing versions and removal/restoration require normal downloads; every reused document still passes folder, history and checksum validation. Pre-save and post-save synchronization, maintenance checks, the existing read-cache interval, and immutable-history validation remain in place.

Privacy-safe `save_timing` events use the existing Uvicorn error logger hierarchy, so they are visible without enabling access logs. They contain entity type, static action, outcome/error code, total time, lock wait, pre/post synchronization, consistent-read and write timings, Drive API execution count, and confirmed-document reuse count. Timings overlap where one stage contains another; API executions may themselves retry. No names, IDs, actors, request bodies, notes, URLs or credentials are logged.

## One sales record, many views

Sales records display the linked account's current company name and location/address, not a separate enquiry title. Multiple records for the same company remain separate enquiries; their product notes, acquired dates, and IDs distinguish them. Updating the account updates these display values without rewriting sales history. Legacy titles remain readable in stored revisions, but are not required or edited by the app.

Business dates are date-only values interpreted in Asia/Kolkata. Recording timestamps use UTC. Missing creation/stage/payment dates are defaulted only when writing the event. A normal notes edit cannot change stage/payment fields.

The product/requirement section has its own Edit button; the top Edit details form remains available. `PATCH /api/records/{id}/product-notes` accepts only `productNotes`, preserving contacts, general notes, quotation references, stage dates, payment state, and follow-up data. It uses the common revision, stale-edit, and retry safeguards.

The six current stages are New lead, Follow-up, Order in hand, Dispatched, Delivered, and Closed. Each stage follows its predecessor. The Follow-up transition accepts an optional quotation number, which may also be added, changed, or cleared through normal editing. Reached stages remain green. A stage correction can return to an earlier stage with a reason, removing later stages from the current view while keeping old versions. Orders with confirmed payment cannot lose order confirmation until receipt is undone; closed records must be reopened before undoing receipt.

Payment is a details note plus a received flag/date. Notes save independently, without a reason. Received confirms receipt and atomically adds Closed if the record is already Delivered. Earlier receipt never invents dispatch/delivery dates. After a receipt recorded in advance, the final stage action (or Received on the delivered record) closes the sale. The UI and reporting APIs no longer calculate or display monetary amounts or balances.

New records explicitly use `workflowVersion=3`. Snapshots without a version use version 1; version 2 is the earlier workflow with both Quotation sent and Follow-up. Legacy Quotation pending maps to New lead, while Quotation sent maps to Follow-up. Keep an existing Follow-up date or reuse the original quotation date if there is none. Later stages remain current. Read-time stage/date helpers apply the same mapping in backend queries, the dashboard, and frontend tables/timelines without altering stored revision content or checksums. Old records with neither date still display that gap as not recorded. The `order_received` storage key remains stable but is labelled Order in hand.

Retired financial fields and the original Quotation pending/sent dates remain in immutable historical snapshots inside each entity's file. Financial fields are accepted as archived data when reading old snapshots, but cannot be edited through the new payment/stage actions. Payment receipt no longer depends on those amounts. Legacy history is not rewritten; changing an older record creates a new version with normalized stage dates, so removed stages cannot reappear after a backtrack.

Every stage tab filters the **current status only**: New leads, Follow-ups, Order in hand, Dispatched, Delivered, and Closed. Dispatch and delivery tabs exclude records now at later stages. Payment pending/received remain cross-stage receipt-confirmation views. Dashboard counts use the same predicates as the lists. Retired quotation and Awaiting delivery tabs are removed; the APIs reject their old view keys. All-date defaults keep old outstanding work visible.

Every date/day/month/range filter uses **Lead acquired date**, never quotation, order, dispatch, payment, or last-edit dates. The date selects a group of enquiries; each row shows its **current sales stage**, not its stage back on the selected date. All sales records includes the whole acquired group, while each saved view narrows it using that view's normal rules. For example, a September lead closed in October appears in All sales records and Closed for September acquisitions, but not in Dispatched or Delivered. Fixed-stage views have no redundant stage selector. Follow-up due-today/overdue filters operate on the next reminder within that stage; logging a note alone does not move the stage.

All sales tables use descending lead-acquired date, then descending stable entity ID to break ties, before pagination. The column exposes `aria-sort="descending"` and a visible arrow; list captions say newest first. This includes account-linked sales, the overview's recent records, and Discarded. Last-update time does not affect ordering.

## Discarded records

`POST /api/records/{id}/discard` requires a nonblank `reason`. It saves `discarded=true`, `discardNote`, and today's India-local `discardedOn`, with the actor, recorded timestamp, and reason in a new immutable revision. No stage, payment confirmation, reminder, or historical version is deleted. The action works at any stage, including Closed; it is not financial cancellation/refund processing.

Snapshots lacking the new fields default to not discarded. Discarded records remain visible in All sales records and Discarded (including acquisition-date filters), but are excluded from every stage/payment queue and corresponding dashboard count. Their badge says Discarded, the last stage remains visible, and the Discarded table also shows the discard date and remark.

Discarded records are read-only through normal mutation endpoints; restoring them is not implemented. The existing common write guard rejects stale edits and changes after discard while still acknowledging retries of the same successful operation. A concurrent conflicting branch is handled by the existing explicit conflict-resolution flow, not silently overwritten. The UI warns before abandoning unsaved payment details on discard.

## Storage

The version-2 root contains fixed child folders and `settings.json` with their IDs and the preallocated shared checkpoint ID. Each account, contact, and sales record occupies exactly one `<entity-id>.json` file. Its Drive file ID stays unchanged on edits. The document contains `schemaVersion=2`, the stable entity ID/type, the `current` revision, a `history` array of previous revisions, and a checksum. Each embedded revision retains its original ID, parents, actor, action, timestamp, reason, fingerprint, and full validated data snapshot. Migration never normalizes or rewrites historical business values.

The client prepares a unique write ID before mutation and retains it for retries within the form. A retry looks up that ID and checks its fingerprint and actor before returning the prior result. Do not change an unconfirmed save's form values before reconciling it. Closing a browser after an ambiguous failure can lose this in-memory retry ticket; inspect the record list/history before creating again.

The API checks the expected current revision, then reads a consistent document/ETag and conditionally replaces the same file. File ETags and `If-Match` updates use Drive v2: live verification confirmed that v3 ignored the update precondition, while v2 returned HTTP 412 for stale ETags. A losing overlapping save returns `409 stale_edit` and must be refreshed, not silently retried against new data. An ambiguous network result remains unconfirmed until the same operation is found in embedded history. New entity files use preallocated Drive IDs; write IDs identify operations, not additional files.

Legacy conflicting branches are retained in one document with `current=null`. They remain excluded from definitive totals until explicitly resolved; choosing a branch adds a resolution snapshot inside that same file. Local demo saves use a cross-process lock and atomic file replacement. Existing demo revision files are consolidated on load.

The index retains revision metadata, entity-to-file mappings, and full current heads, not every old data snapshot. History loads the entity document on demand. Every 25 newly processed versions, the same `indexes\checkpoint.json` is updated with the projection, Changes cursor, and checksum. Startup replays all pages after that checkpoint. Missing/corrupt checkpoints fall back to a full scan; revoked access is never treated as an empty dataset.

Fewer files does not mean less history or identical performance. Each save reads and rewrites the entity's growing full-history JSON. Very long-lived records can require an explicit history-archival design in the future. There are no cross-record transactions, and Drive latency, quota, connectivity, credential expiry/revocation, and account loss remain operating risks. Conditional writes protect individual entity files, not the whole workspace atomically. Keep backups; do not infer database guarantees from a one-file layout.

Checksums detect accidental alteration, not an attacker with write access to the same Drive account. Treat the owner's account and raw data folder as privileged.

The complete initial scan captures a Changes token first and replays intervening changes. A very large full rebuild can exceed a web request budget; run the operator rebuild utility first. No background thread is assumed to run after a Cloud Run request finishes.

## Backup, rebuild, and restore

Operate during a maintenance window so the backup's recorded boundary is clear. Use the same OAuth client as the app. Set the credential from a protected local file without printing it:

```powershell
$env:GOOGLE_TOKEN_JSON = Get-Content -Raw ".secrets\drive-token.json"

.\.venv\Scripts\python.exe -m scripts.drive_admin backup --root "<source-root-id>"
.\.venv\Scripts\python.exe -m scripts.drive_admin rebuild --root "<source-root-id>"
```

Backup scans canonical documents and creates a read-back-verified, checksummed manifest of all embedded revisions in the root's `backups` folder. It prints the backup file ID. Run backups with writers stopped: a scan of several files is not a cross-record transactional snapshot. Backup is explicit, not a new file on every save. Never interpret a Drive-reported failure as a successful backup.

For a restore rehearsal, use the bootstrap script to create a **new empty root** with the same OAuth client, then:

```powershell
.\.venv\Scripts\python.exe -m scripts.drive_admin restore `
  --root "<source-root-id>" --backup-id "<backup-file-id>" --target-root "<empty-target-root-id>"
```

Restore allocates one new Drive file per entity while retaining all original entity IDs, revision IDs, parent references, and historical values. It accepts both legacy and version-2 backup manifests and verifies the rebuilt projection. It refuses an existing populated target. If restoration is interrupted, preserve that partial root for diagnosis and retry into a different empty root; it does not claim transactional restoration. Review the restored accounts, totals, conflicts, and history before changing production's folder ID.

## Migrating an existing revision-file workspace

Deploy the migration-aware release to all traffic first. It deliberately pauses CRM access on a version-1 root; liveness remains available. Do not route traffic back to an old image during or after migration.

```powershell
$env:GOOGLE_TOKEN_JSON = Get-Content -Raw ".secrets\drive-token.json"
.\.venv\Scripts\python.exe -m scripts.drive_admin migrate --root "<source-root-id>"
.\.venv\Scripts\python.exe -m scripts.drive_admin migrate --root "<source-root-id>" `
  --apply --writers-stopped --drain-seconds 300
```

The first command only plans. Applying sets maintenance mode, waits for old requests to drain, creates and verifies a complete source backup, then reuses one existing file per entity and embeds every historical version. It verifies exact version hashes before moving redundant files to Trash, reuses one checkpoint, verifies restart/replay, and clears maintenance only after success. It retains the verified backup ID in settings. Source changes or verification failures stop the migration with maintenance and backup retained.

The operation is restartable after an interrupted conversion, cleanup, or checkpoint write: rerun the same command against the same root. A completed rerun is a no-op. This is a controlled maintenance operation, not a transaction; do not manually change source files or allow legacy writers during it. Old revision files and superseded checkpoints go to Trash rather than being permanently deleted. A code-only rollback to the version-1 storage implementation is not compatible with the migrated root.

Remove the credential from the process after operating:

```powershell
Remove-Item Env:\GOOGLE_TOKEN_JSON
```

Drive revision history and same-account backups do not survive loss of that account. An off-account backup policy requires a separate storage decision. Automatic history pruning and backup-retention cleanup are not implemented.

## Verification boundary

Automated tests cover domain rules, default dates/corrections, references, idempotency, conditional-write races, ambiguous retries, local locking/restart recovery, legacy conflict preservation, interrupted/resumed migrations, exact backup/restore history preservation, one entity file after 100 edits, shared checkpoint reuse, IAP rejection, Drive pagination/cursor replay/error handling, and a 12,000-record/120,000-version synthetic projection. Browser checks cover a full sales journey and responsive navigation.

The synthetic scale check verifies bounded current-record projections and no history-file downloads for warm list queries. It does not measure real Google network latency or quotas. Live Drive integration, the container build, and IAP deployment must be verified with the owner's project before production release.
