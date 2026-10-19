# Connect Google Drive once

## Requirements

- A Google Cloud project associated with your Workspace organization where possible.
- A designated Workspace account whose My Drive will own the data.
- Permission to enable Drive API and configure OAuth clients.
- Python dependencies installed from the repository.

The operator's IAP sign-in and the owner's Drive authorization are separate. Do not share an OAuth credential with the browser or commit it to Git.

## Bootstrap a new workspace

1. Enable **Google Drive API** in Google Cloud Console.
2. Configure Google Auth Platform branding/audience. Use **Internal** if the project and Workspace organization allow it. Otherwise complete the appropriate production consent configuration. An external consent application left in Testing can issue expiring refresh tokens.
3. Create an OAuth client of type **Desktop app**. Download its JSON into a private directory. This app requests only `https://www.googleapis.com/auth/drive.file`.
4. From the repository root, run:

```powershell
New-Item -ItemType Directory -Force .secrets | Out-Null
.\.venv\Scripts\python.exe -m scripts.bootstrap_drive `
  --credentials ".secrets\credentials.json" `
  --token-out ".secrets\drive-token.json"
```

5. Sign in as the intended Drive owner and consent in the browser. The script verifies a refresh token, saves the credential locally, and creates a private SalesTrackr folder with accounts, contacts, sales records, indexes, backups, and settings.
6. Record the printed `GOOGLE_FOLDER_ID`. The OAuth token itself is not printed.
7. Upload the credential into Secret Manager using the deployment guide. Restrict the local credential directory to your OS user, and remove the local credential files when no longer needed.

The script deliberately refuses to overwrite an existing credential file. It creates a new root each time unless `--reconnect` is specified. If folder creation is interrupted, inspect the owner's Drive and finish/restart setup deliberately; do not point production at an incomplete root.

The narrow scope works because the application creates its files. Pasting an unrelated existing folder ID does not authorize this app to read it. Keep the same OAuth client for future reconnects so app-scoped files/properties remain accessible.

## Reconnect after revocation

Do not bootstrap a replacement empty dataset. Renew only the credential:

```powershell
.\.venv\Scripts\python.exe -m scripts.bootstrap_drive `
  --credentials ".secrets\credentials.json" `
  --token-out ".secrets\drive-token-renewed.json" `
  --reconnect

gcloud secrets versions add salestrackr-drive-oauth `
  --data-file=".secrets\drive-token-renewed.json"
```

Redeploy/update the Cloud Run service to the new **numeric** secret version, keeping the original folder ID. Verify existing records remain visible. A read-only/runtime secret accessor role is sufficient; the app does not write refreshed tokens to disk or Secret Manager.

## Verify real connectivity before release

Use a separate bootstrap-created test root, not production customer records.

1. Set `GOOGLE_TOKEN_JSON` in the operator process from the protected file; never place its contents on the command line.
2. Exercise the Drive repository with a synthetic account, contact, and full sales journey.
3. Verify a duplicate write-ID retry returns the original version; a changed payload with that ID is rejected.
4. Verify two overlapping saves leave the winning version intact and reject the stale update. All successful versions remain in the same entity file.
5. Confirm checkpoint replay after a process restart, stable entity/checkpoint file IDs after repeated saves, and reconnect behavior after using an invalid credential.
6. Run backup/restore into another empty root as described in the architecture guide.

The automated adapter tests mock Google responses. They do not replace these owner-authorized integration checks.
