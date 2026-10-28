param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [Parameter(Mandatory = $true)][string]$Region,
    [string]$Service = 'salestrackr',
    [string]$DriveTokenFile = '.secrets\drive-token-producttrackr-authorized.json',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Config = Get-Content -Raw (Join-Path $Root 'producttrackr.production.json') | ConvertFrom-Json
if ($ProjectId.StartsWith('demo-')) {
    throw 'Use the existing production Firestore project.'
}
Write-Output "ProductTrackr: project=$ProjectId database=$($Config.database) root=$($Config.driveRootId)"
if (-not $Apply) {
    Write-Output 'Plan only: reuse the existing database and prepare indexes, runtime IAM and the Drive secret.'
    Write-Output 'No application revision, traffic, SalesTrackr settings or local data will be changed.'
    Write-Output 'Run again with -Apply after reviewing the production deployment guide.'
    return
}

function Invoke-Cloud {
    $Result = & gcloud @args "--project=$ProjectId" --quiet
    if ($LASTEXITCODE -ne 0) { throw "Google Cloud command failed: $($args[0..1] -join ' ')" }
    return $Result
}

$TokenPath = (Resolve-Path $DriveTokenFile).Path
$Existing = Invoke-Cloud run services describe $Service "--region=$Region" --format=json | ConvertFrom-Json
if ($Existing.metadata.annotations.'run.googleapis.com/iap-enabled' -ne 'true') {
    throw 'The existing Cloud Run service must keep IAP enabled.'
}
$Runtime = $Existing.spec.template.spec.serviceAccountName
if (-not $Runtime) { throw 'The existing service must have an explicit runtime service account.' }
$SalesFolders = @($Existing.spec.template.spec.containers[0].env |
    Where-Object { $_.name -in @('GOOGLE_FOLDER_ID', 'BACKUP_DRIVE_FOLDER_ID') } |
    ForEach-Object { $_.value })
if ($Config.driveRootId -in $SalesFolders) { throw 'ProductTrackr must not use a SalesTrackr Drive folder.' }
if ($Config.database -eq '(default)') {
    $SalesProject = ($Existing.spec.template.spec.containers[0].env |
        Where-Object name -eq 'FIRESTORE_PROJECT_ID').value
    $SalesDatabase = ($Existing.spec.template.spec.containers[0].env |
        Where-Object name -eq 'FIRESTORE_DATABASE_ID').value
    if ($SalesProject -ne $ProjectId -or ($SalesDatabase -and $SalesDatabase -ne '(default)')) {
        throw 'The shared default database must be the existing SalesTrackr database.'
    }
}
Invoke-Cloud services enable firestore.googleapis.com secretmanager.googleapis.com firebaserules.googleapis.com |
    Out-Null
$Databases = @(Invoke-Cloud firestore databases list --format=json | ConvertFrom-Json)
$Database = $Databases | Where-Object { $_.name -eq "projects/$ProjectId/databases/$($Config.database)" }
if (-not $Database) {
    if ($Config.database -eq '(default)') { throw 'The existing shared database was not found.' }
    Invoke-Cloud firestore databases create "--database=$($Config.database)" "--location=$Region" `
        --edition=standard --type=firestore-native --delete-protection | Out-Null
} elseif ($Database.type -ne 'FIRESTORE_NATIVE') {
    throw 'The ProductTrackr database must use Firestore Native mode.'
}
$Expression = "resource.name==`"projects/$ProjectId/databases/$($Config.database)`""
$Condition = "expression=$Expression,title=producttrackr-runtime"
$Policy = Invoke-Cloud projects get-iam-policy $ProjectId --format=json | ConvertFrom-Json
$Access = $Policy.bindings | Where-Object {
    $_.role -eq 'roles/datastore.user' -and $_.members -contains "serviceAccount:$Runtime" -and
    (-not $_.condition -or $_.condition.expression -eq $Expression)
}
if (-not $Access) {
    Invoke-Cloud projects add-iam-policy-binding $ProjectId "--member=serviceAccount:$Runtime" `
        --role=roles/datastore.user "--condition=$Condition" | Out-Null
}

$Secrets = @(Invoke-Cloud secrets list --format=json | ConvertFrom-Json)
if (-not ($Secrets | Where-Object { $_.name.EndsWith("/secrets/$($Config.driveSecret)") })) {
    Invoke-Cloud secrets create $Config.driveSecret --replication-policy=automatic | Out-Null
}
$Versions = @(Invoke-Cloud secrets versions list $Config.driveSecret --format=json |
    ConvertFrom-Json)
$Version = $Versions | Where-Object { $_.name.EndsWith("/versions/$($Config.driveSecretVersion)") }
if ($Version) {
    if ($Version.state -ne 'ENABLED') { throw 'The configured secret version is not enabled.' }
} else {
    $Highest = 0
    foreach ($Item in $Versions) { $Highest = [Math]::Max($Highest, [int]($Item.name.Split('/')[-1])) }
    if ([int]$Config.driveSecretVersion -ne ($Highest + 1)) {
        throw 'Set driveSecretVersion to the next numeric version before uploading the credential.'
    }
    $Added = Invoke-Cloud secrets versions add $Config.driveSecret "--data-file=$TokenPath" --format=json |
        ConvertFrom-Json
    if (-not $Added.name.EndsWith("/versions/$($Config.driveSecretVersion)")) {
        throw 'The uploaded version does not match producttrackr.production.json; retain it and review.'
    }
}
Invoke-Cloud secrets add-iam-policy-binding $Config.driveSecret "--member=serviceAccount:$Runtime" `
    --role=roles/secretmanager.secretAccessor | Out-Null

function Index-Key($Index) {
    $Group = if ($Index.collectionGroup) { $Index.collectionGroup } else {
        ($Index.name -split '/collectionGroups/')[1].Split('/')[0]
    }
    $Fields = @($Index.fields | Where-Object { $_.fieldPath -ne '__name__' } |
        ForEach-Object { "$($_.fieldPath):$($_.order)$($_.arrayConfig)" })
    return "$Group/$($Index.queryScope)/$($Fields -join ',')"
}

$Groups = @('products', 'contacts', 'manufacturingRecords', 'manufacturingRecordEvents')
$Definitions = Get-Content -Raw (Join-Path $Root 'firestore.indexes.json') | ConvertFrom-Json
$Indexes = @($Definitions.indexes | Where-Object { $_.collectionGroup -in $Groups })
$Current = @(Invoke-Cloud firestore indexes composite list "--database=$($Config.database)" --format=json |
    ConvertFrom-Json)
$Keys = @($Current | ForEach-Object { Index-Key $_ })
foreach ($Index in $Indexes) {
    if ((Index-Key $Index) -in $Keys) { continue }
    $Arguments = @('firestore', 'indexes', 'composite', 'create',
        "--database=$($Config.database)", "--collection-group=$($Index.collectionGroup)",
        '--query-scope=collection', '--async')
    foreach ($Field in $Index.fields) {
        $Setting = if ($Field.arrayConfig) { 'array-config=contains' } else {
            "order=$($Field.order.ToLowerInvariant())"
        }
        $Arguments += "--field-config=field-path=$($Field.fieldPath),$Setting"
    }
    Invoke-Cloud @Arguments | Out-Null
}
foreach ($Field in ($Definitions.fieldOverrides | Where-Object { $_.collectionGroup -in $Groups })) {
    Invoke-Cloud firestore indexes fields update $Field.fieldPath "--database=$($Config.database)" `
        "--collection-group=$($Field.collectionGroup)" --disable-indexes --async | Out-Null
}
$Deadline = (Get-Date).AddMinutes(15)
do {
    $Current = @(Invoke-Cloud firestore indexes composite list "--database=$($Config.database)" --format=json |
        ConvertFrom-Json)
    if ($Current | Where-Object { $_.state -eq 'NEEDS_REPAIR' }) {
        throw 'A database index needs repair. Review it before deployment.'
    }
    $Ready = @($Current | Where-Object { $_.state -eq 'READY' } | ForEach-Object { Index-Key $_ })
    $Pending = @($Indexes | Where-Object { (Index-Key $_) -notin $Ready })
    if (-not $Pending.Count) { break }
    if ((Get-Date) -ge $Deadline) {
        throw 'Index builds are still pending. Rerun after they finish; existing resources were retained.'
    }
    Write-Output "Waiting for $($Pending.Count) ProductTrackr indexes to become ready..."
    Start-Sleep -Seconds 30
} while ($true)
Write-Output 'Cloud resources prepared. No application deployment or traffic change was performed.'
Write-Output 'The workflow uses PRODUCTTRACKR_ADMIN_EMAILS or the existing recovery admins. Deploy after local checks.'
