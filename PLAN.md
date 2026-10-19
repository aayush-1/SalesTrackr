# SalesTrackr: simple implementation plan

## 1. What we are building

A single-user web app that replaces moving sales entries between Excel tabs.

Create an account once. For each new enquiry, create one sales record linked to that account. Keep that same record from New lead through Closed, showing dated green stages, product notes, follow-ups, and payment details. Different pages show filtered views of the same records.

Build the UI first, then the APIs, then Google Drive connectivity, and finally deploy on Google Cloud Run. Use Google Drive for durable business data and protected Google sign-in for app access.

This revised plan replaces the earlier team/performance-focused plan. At planning time, the repository contained only `README.md`. The user subsequently authorized implementation; the UI, APIs, storage adapters, operator tools, tests, and Cloud Run deployment files are now implemented.

**Current status:** local demo and automated checks are available. Live Google Drive integration, a Docker/Cloud Build image build, and IAP-protected deployment still require the owner's project/credentials and release verification. Deployment is intentionally deferred until the local UI is reviewed. See `README.md` and `docs\cloud-run-deployment.md`.

## 2. What we took from the Excel workbook

Reviewed the relevant sheets in the supplied `22092026 JSD KOMOR Master Products List .xlsx` locally.

| Workbook area | Corresponding app feature |
| --- | --- |
| `LEADS` | New leads current-stage view |
| `FOLL UP` and older follow-up sheets | Follow-up current-stage view, notes, next follow-up date, and optional quotation number |
| `O I HAND` | Confirmed orders waiting for dispatch |
| `disp.` | Currently dispatched records, filterable by lead-acquired date |
| Payment area in `C PLATFORM` | Payment pending and payment received information on each sales record |
| `PERFOR.-JYOTI` | Reference for date-based working; no separate performance dashboard |
| `WO BILL` | Reviewed, but billing tracking is outside this version |

The workbook often uses multiple product rows for an enquiry. As requested, the app will use one free-text product-notes section instead of product entities or item rows.

Use the workbook as a workflow reference only. Do not import it or include its customer/contact data in the repository. Its mixed date formats and continuation rows would require a separate reviewed import project.

## 3. The records

### Account and contacts

An account represents a customer company. Store its name, optional address, optional GST/tax reference, and notes. Keep its contact people with names, phone numbers, email addresses, and optional designations.

Each account has a stable ID, can have several contacts, and can have many separate sales records. Changing a company name must not break its sales history. The Accounts page uses a searchable table/list, not cards.

Provide a dedicated **New Account** page and an account detail page with contact editing and all related sales records. Also allow account creation from the New Sales Record form. Search existing accounts first and warn about likely duplicates.

### Sales record

| Field | Purpose |
| --- | --- |
| Record ID | Stable identifier for the entire enquiry-to-closure journey |
| Account and optional contact | Identify the customer without retyping company details |
| Display heading | Linked company name with its location/address below; no enquiry-title field |
| Product notes | Free text describing all relevant parts, quantities, and requirements |
| General notes | Other context and remarks |
| Lead acquired date | Actual date the enquiry was received; defaults to today |
| Stage dates | Dates when each of the six stages was reached |
| Current status | Derived from the last reached stage |
| Follow-up entries | Dated notes and the next follow-up date |
| Quotation number | Optional when moving to Follow-up; may also be added, edited, or cleared later |
| Payment fields | Details note, received confirmation, and received date; no monetary inputs |
| Discard fields | `discarded` flag, required `discardNote`, and automatic `discardedOn` date; original journey is retained |
| History | Stage/payment/date corrections and other recorded changes |
| Created/updated timestamps | Automatic system timestamps, separate from business dates |

One record can describe several products, but it belongs to one account. A later enquiry for the same account creates a new record.

## 4. Six green stages with dates

```text
New lead -> Follow-up -> Order in hand
         -> Dispatched -> Delivered -> Closed
```

The record detail page displays these stages horizontally on larger screens and vertically on smaller screens.

- Reached stages are green with their dates. The current stage is also clearly labelled. Future stages are grey.
- Green means "this stage was reached." Follow-up is the next dated stage after New lead; there is no separate Quotation sent stage.
- Offer an optional quotation number in the Follow-up transition form and save it with the new stage/date. A blank quotation number never blocks progress.
- Creating a record marks New lead green. Its date is the lead-acquired date, not a separately editable duplicate.
- Provide a **Mark next stage reached** action. Normal progress is one stage at a time.
- On creation or stage advancement, accept an optional actual business date. If omitted, use today's date in `Asia/Kolkata`.
- Save a separate automatic timestamp showing when the entry was actually recorded. This distinguishes an older business event entered today.
- Once recorded, dates stay fixed. Opening the page, editing notes, or saving an unrelated field must not change them.
- Keep every stage-change event, including its previous/new stage, effective date, recorded timestamp, and correction reason where applicable.
- A correction/backtrack requires a reason. Reset later stage indicators as appropriate, but retain their previous dates in history. Do not silently erase the earlier journey.
- Validate that reached-stage dates are chronological; allow several stages on the same date. Future dates belong in follow-up reminders, not actual completed-stage dates.
- Do not count a date correction as another new order or dispatch.

Legacy Quotation pending records appear as New lead, while Quotation sent records appear as Follow-up. Preserve an existing Follow-up date; otherwise reuse the retired Quotation sent date. Older records already at Order in hand or later keep their progress. If neither old date exists, show the Follow-up date as not recorded. Original stage dates remain in saved history. New records use workflow version 3. The internal `order_received` key stays stable while its display name is Order in hand.

## 5. Payment is separate from the six stages

Use these fields on the same sales record:

```text
paymentReceived: receipt confirmed Yes / No
paymentReceivedOn: date receipt was confirmed
paymentNote: optional text
```

Show one payment-details box and a **Received** button. Save details independently when needed. There are no order amounts, received amounts, balances, partial-payment inputs, or payment columns on sales tables.

- Enable Received after Order in hand. It confirms receipt and records today's date without requiring a reason.
- If the sale is already Delivered, Received also marks Closed in the same save.
- Otherwise keep the current stage; never skip dispatch or delivery. Once delivered, use Received or the final Closed action to close.
- Pending/received views and overview cards use confirmation counts, not monetary totals.
- Undo receipt before delivery/closure if needed; reopen a Closed record before undoing receipt.
- Notes-only changes retain receipt and stage dates. Stage backtracking still requires a reason.

This replaces the earlier partial-payment/amount-based UI. Original financial fields remain in saved history, but are no longer operational inputs or calculations. Refunds and accounting remain outside this version.

## 6. Pages and everyday views

### Main pages

1. **Overview:** simple current-stage counts and links to new leads, follow-ups, orders in hand, and pending payments. No staff performance, targets, rankings, or assignment features.
2. **Accounts:** searchable list, dedicated New Account page, and account/contact details.
3. **New Sales Record:** select/create an account, select a contact if needed, enter product notes and optional lead-acquired date. Use the company's name and location/address as the display heading.
4. **Sales Records:** reusable table with the saved views below and an All Records view.
5. **Record Detail:** customer, notes, green stage timeline, payments, follow-ups, and full history. Keep the top Edit details action and add a separate Edit button beside product/requirement notes that changes only that field. Offer Discard record at any stage.

### Saved views of the same records

Every date/day/month/range selection uses **Lead acquired date**. The selected group is shown with its current status today, not its historical status on the selected date. This replaces the earlier per-view milestone-date filtering.

| View | Which records appear |
| --- | --- |
| All Sales Records | All enquiries acquired in the selected period, including discarded records |
| New Leads | Current status is New lead only |
| Follow-ups | Current status is Follow-up only, with optional next-reminder due/overdue filters |
| Order in Hand | Current status is Order in hand only |
| Dispatched | Current status is Dispatched only |
| Delivered | Current status is Delivered only |
| Payment Pending | Confirmed orders without receipt confirmation, including delivered orders |
| Payment Received | Records with payment receipt confirmed |
| Closed | Current status is Closed only |
| Discarded | Records with the discarded flag, showing the discard remark/date and their last reached stage |

**Stage tabs are exact current-status queues, not milestone history.** Remove the quotation and Awaiting delivery tabs. Keep the payment tabs independent so a record can appear in its current-stage tab and the applicable payment tab.

Discarded records are excluded from every stage/payment tab and the corresponding dashboard counts, while remaining in All Sales Records and Discarded.

For example, a lead acquired in September and closed in October appears in All Sales Records and Closed when filtering September acquisitions, but no longer appears in Dispatched or Delivered. The month filter always means acquisition month.

### Shared table behavior

- Display company name with location/address below, product notes, lead-acquired date, current sales stage, latest stage date, and next follow-up where relevant. No enquiry title or payment columns are required.
- Sort every sales table by lead-acquired date, newest first, before pagination. Show a descending arrow on the column and a newest-first caption. Break same-date ties by stable record ID; unrelated edits do not reorder records.
- Offer company/location/product search, applicable current-stage filtering, and a **Lead acquired date** selector with All dates / Today / Selected date / Selected month / Custom range.
- Always filter by acquisition date, including order/payment views; remove the selectable "Date refers to" / event-date field.
- Hide the redundant stage selector on each single-stage tab. All Sales Records and payment views offer only relevant current stages.
- Working queues default to All dates, so old unpaid orders or unanswered enquiries do not disappear.
- All Sales Records shows every current stage for the selected acquisition period. Selecting a saved view or current stage narrows this group, not the meaning of the date.
- Use local calendar dates in `Asia/Kolkata`; use inclusive start/exclusive end when comparing stored timestamps.
- Clicking any row opens the same record detail page. Updating it changes which views it appears in; no copying between lists.
- Keep an explicit Refresh action and show last synchronization/error information.

### Follow-up behavior

Follow-up is the stage between New lead and Order in hand; its quotation number is optional. Separately, log dated follow-up notes and optional reminders before or after quotation. Repeated notes remain separate history entries. Logging a note alone does not change stages or payment confirmation; use the next-stage action to enter Follow-up.

### Discard behavior

Discard record is available at any stage, including Closed, and opens a form requiring a remark. Save `discarded=true`, `discardNote`, and today's `discardedOn` date in a new audited revision. Keep all stage dates, receipt information, follow-up entries, and previous versions. It is not deletion, a stage transition, or a refund.

Discarded records become read-only. Display their remark/date prominently and retain the last reached stage in the timeline. Do not count them as active leads, orders, payments, or closed sales. A legacy record without discard fields defaults to not discarded. Normal edits cannot bypass the explicit discard action, and retry/stale-write protections still apply. Restoring discarded records is deferred.

## 7. Simple technical architecture

```text
Browser -> Google sign-in / IAP -> One Cloud Run service
                                  React UI + FastAPI
                                         |
                                  Google Drive JSON
```

- **UI:** React, TypeScript, and Vite; build table/form screens first using mock data.
- **API:** Python/FastAPI validates accounts, contacts, dates, stage progress, and payments.
- **Hosting:** one container serves the built UI and API on the same origin.
- **Business storage:** a new app-created folder in the designated Workspace account's My Drive.
- **Credentials:** Secret Manager stores the Drive OAuth credential, not business records.
- **Access:** only the authorized operator can access the app through IAP.
- **Repository components:** `frontend\`, `backend\`, `scripts\`, a Dockerfile, ignore files, and concise setup documentation.

### APIs

Create account/contact list, detail, create, and edit endpoints. Add sales-record list/create/edit/detail endpoints, plus explicit stage, payment, follow-up, and history actions. A normal record edit must not bypass stage or payment validation.

Return field-specific validation errors, stale-edit conflicts, and actionable Drive connection errors. A failed save must never appear successful.

### Drive storage and reliability

Organize the root into `accounts\`, `contacts\`, `sales-records\`, `indexes\`, and `backups\`.

Keep one JSON file per account, contact, or sales record. Saves update that same Drive file. The document embeds a current snapshot and previous immutable versions, retaining stable entity IDs, parent references, audit metadata, and full history. This replaces the original revision-per-file layout.

- Pre-generate one Drive file ID on creation and a separate operation ID for each save. Reconcile retries against embedded history.
- Reject known stale edits and enforce file ETags on replacements using Drive v2. Never silently use the last writer's payment data. Preserve and explicitly resolve any branches inherited from the old layout.
- Keep a rebuildable in-memory reporting index and reuse one shared checkpoint file in Drive. Load it and replay paginated Drive Changes; do not download every historical snapshot for each list page.
- Capture a Changes cursor before a full rebuild and replay intervening changes. Missing/corrupt checkpoints must be recoverable from canonical versions.
- Show stale/incomplete results and unresolved conflicts explicitly; do not silently omit them from apparently complete payment totals.
- Only acknowledge persistence after Drive confirms the save. If the index update then fails, report "saved, synchronization pending" and recover from the stored revision.
- Use bounded retries for transient errors. Revoked credentials, missing permissions, invalid data, and quota failures need explicit messages.
- Cloud Run disk is temporary. Neither a local JSON/SQLite file nor an in-memory cache is the durable store.
- Creating an account and then its first sales record is two saves. If the second fails, retain the account and retry only the sales record.

Drive is not a transactional database. Reports can lag, large embedded histories increase per-save transfer cost, and multi-record transactions are not available. A single-instance setting is not a correctness guarantee. Keep the raw Drive files app-managed and document backup/restore; same-account backups do not protect against losing the entire account. Convert existing roots with a verified backup, paused legacy writers, restartable consolidation, and exact history verification; do not silently replace or delete history.

## 8. Google connection and deployment

### One-time Drive setup

1. Configure a Google Cloud project and enable Drive API. Use an Internal OAuth audience where the Workspace/project organization allows it.
2. Create a separate OAuth client and run a local bootstrap utility. Use only the connection pattern from the supplied `aayush-1/MoM` file: authorized-user credentials, automatic refresh, and Drive v3 client creation.
3. The owner grants offline access once using the narrow `drive.file` scope. Have this app create its root/child folders and retain their IDs.
4. Store the refresh-token credential in Secret Manager and grant the runtime service account access only to that secret.
5. In Cloud Run, refresh access tokens automatically. Do not open a consent browser or write a durable `token.json` inside the container.
6. If authorization is revoked, show reconnect-required, stop mutations, reauthorize locally, and update the secret. Do not silently create a new empty data folder.

"Always connected" means automatic refresh while authorization remains valid, not permanent authentication-free access. App sign-in and the owner's one-time Drive consent are separate.

### Cloud Run steps

1. Enable billing, Cloud Run, Cloud Build, Artifact Registry, Secret Manager, and IAP. Configure budget alerts and least-privilege runtime/build identities.
2. Build one production container containing the UI and API; listen on Cloud Run's `PORT`. Exclude tokens, local data, and secrets from Git and build contexts.
3. Validate the container locally, then deploy using `gcloud run deploy --source .` with the runtime identity, folder configuration, and pinned secret version.
4. Require authentication and enable IAP directly on the service. Give the IAP service agent invocation permission and grant IAP access only to the intended operator.
5. Validate signed IAP identity in the backend; do not trust plain identity headers. Protect mutations against cross-origin requests.
6. Verify real create/update/payment flows, denial for unauthorized users, refresh after restart, credential replacement, and backup recovery. Keep a documented rollback path.

Do not assume hosting, builds, secrets, logging, or storage will always be free. Project-specific commands belong in the implementation's deployment guide.

## 9. Implementation todos

Track execution status in the session SQL database, not Markdown checkboxes. Application implementation is complete locally; the cloud-deployment step remains gated on UI review and Google configuration.

| ID | Work | Prerequisites |
| --- | --- | --- |
| publish-plan | Add this approved plan as repository `PLAN.md`; documentation only | Plan approval |
| domain-contracts | Define account/contact/sales-record schemas, six stages, date rules, payment rules, and view filters | None |
| mock-ui | Build account pages, record form/detail, dated green stages, and all filtered tables using mock data | domain-contracts |
| domain-api | Implement validation and APIs, including explicit stage/payment/follow-up actions and history | domain-contracts |
| drive-storage | Connect Drive, update one file per entity with embedded history, and support conditional saves/retries | domain-api |
| reporting-integration | Replace mocks, build the reporting index, and wire status/date/month/payment views | mock-ui, domain-api, drive-storage |
| acceptance-hardening | Verify business rules, histories, filters, save failures, concurrency/recovery, and container behavior | reporting-integration |
| cloud-deployment | Configure secrets/IAP and deploy/verify the protected Cloud Run app | acceptance-hardening |
| operating-docs | Update README scope and finish connection, deployment, reconnect, backup/restore, and rollback instructions | cloud-deployment |

Preferred execution order remains UI -> APIs -> Drive -> Cloud Run. Shared contracts come first; UI and API can be developed independently after them.

## 10. What must work before release

- Creating an account and a linked record persists correctly; later enquiries reuse the account without reusing the same sales record.
- New lead starts green. Each next-stage action turns exactly that stage green and records its date.
- Missing business dates default to today only when recording the event, not when reopening or editing unrelated fields. Supplied dates survive unchanged.
- System timestamps remain distinct from effective business dates; corrections preserve previous values.
- Received closes an already-delivered record in one save, but never skips dispatch/delivery on earlier records. No amount inputs or totals appear in the UI, and receipt confirmation requires no reason.
- Move directly from New lead to Follow-up with or without a quotation number; Order in hand requires no order amount. Older quoted records map to Follow-up using their saved date, while original histories remain readable.
- Old unpaid orders stay visible by default; Orders in Hand excludes dispatched records; every selected acquisition month shows the records' current progress, including later closure.
- Each stage tab shows only its current-stage records; Dispatched and Delivered exclude later stages. Follow-up notes do not alter stage dates.
- Product-only edits preserve every other field, and the top full-record edit remains available on active records.
- Discarding requires a remark at any stage, preserves the journey/history, survives reload, and removes the record from stage/payment queues without deleting it.
- All sales lists visibly sort by lead-acquired date, newest first, including filtered and paginated results.
- Date filters and list counts use the same definitions, including local day/month boundaries.
- Retrying an ambiguous save does not duplicate records; two-tab conflicts cannot silently lose data.
- Restarts and interrupted indexing recover from Drive; credentials and customer contents never leak into browser bundles or logs.
- Exercise paginated Drive results and a representative dataset of 12,000 records with revision history. Warm list requests must not scan all historical files. If the Drive-only approach fails the scale/recovery check, present the limitation rather than silently adding a database.

## 11. Not included

No Excel import, product catalogue or item rows, inventory/manufacturing, billing status or dispatch-without-bill workflow, quotation/invoice generation, attachments, automatic messages, performance dashboard, team assignments, monetary balance/partial-payment tracking, payment transaction/refund ledger, or hard deletion.

Only the six agreed stages are in scope. Discard is a separate audited flag, not a seventh stage. Restore, separate Lost/Cancelled stages, and financial cancellation/refund processing are deferred.

## References

- Connection reference only: https://github.com/aayush-1/MoM/blob/main/google_docs_service.py
- Drive authorization: https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- Retry-safe file IDs: https://developers.google.com/workspace/drive/api/guides/create-file
- Changes/checkpoint synchronization: https://developers.google.com/workspace/drive/api/guides/manage-changes
- Cloud Run IAP: https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run
- Cloud Run secrets: https://docs.cloud.google.com/run/docs/configuring/services/secrets
- Cloud Run source deployment: https://docs.cloud.google.com/run/docs/deploying-source-code
