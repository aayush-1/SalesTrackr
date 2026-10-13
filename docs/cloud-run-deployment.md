# Deploy after reviewing the local UI

The repository contains one multi-stage Dockerfile: Node builds the frontend, and a non-root Python process serves the UI and API. Cloud Run needs no writable business-data disk.

## 1. Prepare the project

Install the Google Cloud CLI and sign in. Choose a project and region. These commands are PowerShell templates; replace the placeholders.

```powershell
$ProjectId = "<project-id>"
$Region = "<region>"
$Service = "salestrackr"
$OperatorEmail = "<authorized-google-email>"
$RootId = "<GOOGLE_FOLDER_ID-from-bootstrap>"
$SecretVersion = "1"

gcloud auth login
gcloud config set project $ProjectId
gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
  artifactregistry.googleapis.com secretmanager.googleapis.com drive.googleapis.com `
  iap.googleapis.com cloudresourcemanager.googleapis.com

$ProjectNumber = gcloud projects describe $ProjectId --format="value(projectNumber)"
$RuntimeAccount = "salestrackr-runtime@$ProjectId.iam.gserviceaccount.com"
```

Enable billing and budget alerts. Confirm the deployer has the required Cloud Run, source-build, service-account-user, and IAP configuration permissions. The source-build identity needs its appropriate build role; do not grant broad project Owner to the runtime identity.

If the project is outside your Workspace organization, follow Google's custom IAP OAuth setup in the Console before first use. IAP configuration is separate from the Desktop OAuth client used for Drive.

## 2. Runtime identity and secret

Create these only if they do not already exist:

```powershell
gcloud iam service-accounts create salestrackr-runtime --display-name="SalesTrackr runtime"
gcloud secrets create salestrackr-drive-oauth --replication-policy=automatic
gcloud secrets versions add salestrackr-drive-oauth --data-file=".secrets\drive-token.json"
gcloud secrets add-iam-policy-binding salestrackr-drive-oauth `
  --member="serviceAccount:$RuntimeAccount" --role=roles/secretmanager.secretAccessor
```

Set `$SecretVersion` to the numeric version created. Never paste the token JSON into a deploy command. Cloud IAM permission to read the secret does not itself grant access to the owner's My Drive.

## 3. Validate and deploy privately

First run the local checks in README. If Docker is available, build locally:

```powershell
docker build -t salestrackr .
```

Cloud Build can build from source without a local Docker installation. From the repository root:

```powershell
$Audience = "/projects/$ProjectNumber/locations/$Region/services/$Service"

gcloud run deploy $Service --source . --region $Region `
  --service-account $RuntimeAccount `
  --no-allow-unauthenticated --iap `
  --memory=1Gi --cpu=1 --concurrency=8 --min-instances=0 --max-instances=1 `
  --timeout=300 `
  --set-env-vars="APP_MODE=production,GOOGLE_FOLDER_ID=$RootId,IAP_AUDIENCE=$Audience,PUBLIC_ORIGIN=https://setup.invalid" `
  --update-secrets="GOOGLE_TOKEN_JSON=salestrackr-drive-oauth:$SecretVersion"
```

The temporary origin blocks browser writes until the next step. No anonymous access is enabled during setup.

Configure the stable, deterministic service URL shown by Cloud Run:

```powershell
$ServiceUrl = "https://${Service}-${ProjectNumber}.${Region}.run.app"
gcloud run services update $Service --region $Region --update-env-vars="PUBLIC_ORIGIN=$ServiceUrl"

gcloud run services add-iam-policy-binding $Service --region $Region `
  --member="serviceAccount:service-$ProjectNumber@gcp-sa-iap.iam.gserviceaccount.com" `
  --role=roles/run.invoker

gcloud iap web add-iam-policy-binding --region $Region --resource-type=cloud-run `
  --service=$Service --member="user:$OperatorEmail" --role=roles/iap.httpsResourceAccessor
```

Use `$ServiceUrl` consistently when opening the app. Cloud Run also exposes a hash-based URL, but browser writes from that alternate origin are rejected unless it is the configured `PUBLIC_ORIGIN`.

Direct service-level IAP protects the default Cloud Run URL. Do not add `allUsers`, disable IAP, or enable demo mode to fix a sign-in problem. The backend verifies the signed IAP token, audience, issuer, and expiry; unsigned email headers are not accepted.

`/healthz` is a minimal container liveness endpoint and does not validate Drive. Configure it as the startup/liveness probe if using HTTP probes. CRM routes remain protected; successful liveness alone is not release acceptance.

The instance limits are cost controls, not locks. Immutable-version conflict handling works across overlapping instances.

## 4. Release checks

Before adding real business data:

- An authorized browser can open the app; an unauthorized identity is denied.
- The UI does not display the local demo banner.
- `/api/status` reports a complete synchronized index, not a Drive authorization error.
- Create an account/contact and sales record. Reach all stages and mark payment independently.
- Verify an old pending payment remains visible by default. Select the lead's acquisition month and confirm it shows its current stage, even if dispatch/payment/closure happened later.
- Retry a save and exercise two-tab stale-edit handling.
- Restart/redeploy and verify the same Drive data loads through checkpoint replay.
- Verify backups/restoration in a separate root.

The UI is intentionally shown locally before these deployment steps. No cloud project, secret, or service is created automatically by running the demo or browser tests.

## 5. Operation

- Inspect redacted application error logs and set alerts for failures, quotas, revoked consent, and storage corruption. Default Uvicorn access logging is disabled in the container to avoid logging search query strings.
- Reconnect using a new secret version and the existing root, then roll forward.
- Retain the previous image and configuration for rollback. Rolling back an image does not undo Drive data; check schema compatibility first.
- Do not directly edit/delete raw record versions or prune checkpoints without a retention/recovery plan.
- Check actual Drive quotas and Google Cloud costs. Scale-to-zero saves idle resources but can introduce a cold start.
- A missing/corrupt checkpoint may require an operator rebuild. At large volumes run the rebuild utility outside the request lifecycle before release.

## Repository-driven updates

The [SalesTrackr CI and Deploy workflow](https://github.com/aayush-1/SalesTrackr/actions/workflows/deploy.yml) maintains the existing service. It does not create test services, replace the Drive folder, re-bootstrap OAuth, or copy local demo data into production.

- Open a pull request targeting `main`. Every PR runs backend lint/tests and the built UI's Chromium checks, including documentation-only PRs so required checks never remain pending.
- After application changes merge into `main`, the same checks run before production deployment. Documentation-only pushes do not start a deployment.
- For a manual redeploy, open **Actions -> SalesTrackr CI and Deploy -> Run workflow**, select **main**, then confirm **Run workflow**. A manual run on another branch runs checks but cannot deploy.
- Failed checks prevent deployment. Main-branch runs are serialized rather than cancelling an in-progress production rollout. Inspect the workflow log and deployment summary for the result and app link.

Images are built on the GitHub runner, not by starting another Cloud Build job. The workflow pushes a commit-tagged image to the existing Artifact Registry repository and deploys its immutable digest. It retains the service's environment variables, Secret Manager references, runtime identity and IAP setting, and enforces zero minimum instances and a one-instance limit. Build-runner usage and stored images can still incur charges; there is no always-on deployment service.

The workflow requires these **repository Actions variables**, not secrets:

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | Existing Google Cloud project ID |
| `GCP_REGION` | Existing Cloud Run and Artifact Registry region |
| `CLOUD_RUN_SERVICE` | Existing service name, normally `salestrackr` |
| `ARTIFACT_REGISTRY_REPOSITORY` | Existing image repository, normally `cloud-run-source-deploy` |
| `WIF_PROVIDER` | Full Workload Identity Federation provider resource name |
| `DEPLOY_SERVICE_ACCOUNT` | Dedicated GitHub deployment service-account email |

The `production` GitHub environment must allow deployment only from `main`. Configure the Google OIDC provider for `https://token.actions.githubusercontent.com`, with conditions matching the repository's numeric ID and owner ID, `refs/heads/main`, this workflow's exact `workflow_ref`, the `production` environment, and the `push` or `workflow_dispatch` event. PRs and forked repositories must not satisfy those conditions.

Grant the federated repository principal `roles/iam.workloadIdentityUser` on the dedicated deployer account. Give that account Artifact Registry Writer on the one image repository, Cloud Run Developer on the existing service, Service Account User on the runtime account, and Service Usage Consumer on the project. Do not give it a downloadable key, project Owner, IAP policy administration, or direct Secret Manager access. Deployment authority still permits running new code as the runtime identity, so protect `main` and review changes to the workflow.

Require pull requests and the `Checks` status on `main`; zero required approvals permits a sole maintainer to merge their own PR after checks pass. No force pushes or branch deletion are needed. The initial repository bootstrap can be pushed before enabling those rules.

The Google Cloud and repository permissions are one-time setup. Drive reconnection remains an operator action: add a new Secret Manager version and update the service's secret reference; subsequent workflow deployments preserve that reference.

Authoritative references:

- https://docs.cloud.google.com/run/docs/deploying-source-code
- https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run
- https://docs.cloud.google.com/run/docs/configuring/services/secrets
- https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
