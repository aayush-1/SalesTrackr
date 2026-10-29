import { expect, test, type Page, type Request } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Backup, BackupSettings, Me, RecordWorkspace } from '../src/types';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts\\python.exe' : 'bin/python');

async function editRequirement(page: Page, value: string) {
  await page.getByRole('button', { name: 'Edit product / requirement notes', exact: true }).click();
  await page.getByLabel('Product / requirement notes', { exact: true }).fill(value);
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.note-block').first()).toContainText(value);
}

test('real Firestore demo backs up, downloads, restores selected and uploaded archives, and reopens live records', async ({ page, baseURL }, testInfo) => {
  test.setTimeout(180_000);
  const requests: Request[] = [];
  const errors: string[] = [];
  let documents = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/')) requests.push(request);
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents++;
  });
  const identity = async () => {
    const response = await page.request.get('/api/me');
    expect(response.ok()).toBe(true);
    return await response.json() as Me;
  };
  const status = async () => {
    const response = await page.request.get('/api/settings/backup-recovery');
    expect(response.ok()).toBe(true);
    return await response.json() as BackupSettings;
  };
  const settings = async () => {
    await page.getByRole('link', { name: 'Settings', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
    await page.getByRole('tab', { name: 'Backup and recovery', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Create simulated backup', exact: true })).toBeVisible();
  };
  const inspect = async (backup: Backup) => {
    const response = page.waitForResponse(response => response.url().endsWith('/api/backups/inspect'));
    await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
    const inspected = await response;
    expect(inspected.status()).toBe(200);
    await expect(page.getByLabel('Restore preview')).toBeVisible();
    const values = page.getByLabel('Restore preview').locator('.backup-summary dd');
    for (const [index, key] of (['account', 'contact', 'record', 'revisions'] as const).entries()) {
      await expect(values.nth(index + 2)).toHaveText(String(backup.counts[key]));
    }
  };
  const restore = async (previousGeneration: string) => {
    const before = documents;
    const response = page.waitForResponse(response => response.url().endsWith('/api/backups/restore'));
    await page.getByLabel('Type RESTORE to confirm full rollback').fill('RESTORE');
    await page.getByRole('button', { name: 'Restore local demo workspace', exact: true }).click();
    const restored = await response;
    expect(restored.status()).toBe(200);
    await expect.poll(() => documents).toBeGreaterThan(before);
    await expect(page.getByRole('button', { name: 'Create simulated backup', exact: true })).toBeVisible();
    await expect(page.getByLabel('Restore preview')).toHaveCount(0);
    const current = await identity();
    expect(current.generation).toBeTruthy();
    expect(current.generation).not.toBe(previousGeneration);
    expect((await status()).backups.some(row => row.source === 'pre-restore')).toBe(true);
    return current.generation!;
  };

  await page.goto('/accounts/new');
  const initial = await identity();
  expect(initial.storageBackend).toBe('firestore');
  expect(initial.mode).toBe('demo');
  expect(initial.generation).toMatch(/^[a-f0-9-]{32,36}$/);
  const generation = initial.generation!;
  const company = 'Firestore browser recovery company';
  const originalNotes = 'Original enquiry retained by the portable backup.';
  await page.getByLabel('Company name *', { exact: true }).fill(company);
  await page.getByLabel('Location / address', { exact: true }).fill('Local emulator only');
  await expect(page.getByRole('button', { name: 'Create account', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  const accountUrl = page.url();
  await page.getByRole('button', { name: 'Add contact', exact: true }).click();
  await page.getByLabel('Contact name *', { exact: true }).fill('Recovery contact');
  await page.getByRole('button', { name: 'Save contact', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('Recovery contact', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'New sales record', exact: true }).click();
  await page.getByLabel('Contact person (optional)', { exact: true }).selectOption({ label: 'Recovery contact' });
  await page.getByLabel('Product / requirement notes', { exact: true }).fill(originalNotes);
  await page.getByRole('button', { name: 'Create sales record', exact: true }).click();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  await expect(page.locator('.note-block').first()).toContainText(originalNotes);
  const recordUrl = page.url();
  const recordId = new URL(recordUrl).pathname.split('/').at(-1)!;
  const beforeBackup = await (await page.request.get(`/api/records/${recordId}/detail`)).json() as RecordWorkspace;
  await settings();
  await expect(page.getByText('Local simulation only.', { exact: false })).toBeVisible();
  const initialSettings = await status();
  expect(initialSettings).toMatchObject({
    storageBackend: 'firestore', generation, canBackup: true, canRestore: true,
    schedule: { active: false },
  });
  await page.getByLabel('Enable weekly backups', { exact: true }).setChecked(!initialSettings.schedule.enabled);
  await expect(page.getByText('Schedule preference saved locally. No cloud scheduler is running.')).toBeVisible();
  expect((await status()).schedule).toMatchObject({ enabled: !initialSettings.schedule.enabled, active: false });
  const created = page.waitForResponse(response => response.url().endsWith('/api/backups') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Create simulated backup', exact: true }).click();
  const backupResponse = await created;
  expect(backupResponse.status()).toBe(200);
  await expect(page.getByText('Local simulated backup created.', { exact: false })).toBeVisible();
  const backup = (await status()).backups.find(row => row.source === 'manual')!;
  expect(backup).toBeTruthy();
  expect(backup.source).toBe('manual');
  expect(backup.counts).toMatchObject({ account: 1, contact: 1, record: 1 });
  expect(backup.sizeBytes).toBeGreaterThan(0);
  await expect(page.getByText('Local simulated backup created.', { exact: false })).toBeVisible();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: `${backup.name} ZIP`, exact: true }).click();
  const download = await downloadPromise;
  const archivePath = testInfo.outputPath('manual-backup.zip');
  await download.saveAs(archivePath);
  expect(await download.failure()).toBeNull();
  expect(download.suggestedFilename()).toMatch(/\.zip$/);
  const archive = await readFile(archivePath);
  expect(archive.subarray(0, 2).toString()).toBe('PK');
  expect((await status()).backups.some(row => row.id === backup.id)).toBe(true);

  await page.goto(recordUrl);
  await editRequirement(page, 'Changed after backup; selected restore must roll this back.');
  await settings();
  const restoreCount = requests.filter(request => request.url().endsWith('/api/backups/restore')).length;
  await page.getByLabel('Choose a simulated backup', { exact: true }).selectOption(backup.id);
  expect(requests.filter(request => request.url().endsWith('/api/backups/restore'))).toHaveLength(restoreCount);
  await inspect(backup);
  expect((await identity()).generation).toBe(generation);
  await page.getByLabel('Type RESTORE to confirm full rollback').fill('restore');
  await expect(page.getByRole('button', { name: 'Restore local demo workspace', exact: true })).toBeDisabled();
  const selectedRestoreGeneration = await restore(generation);
  const initialWrites = requests.filter(request => request.method() !== 'GET' &&
    !/\/api\/(?:backups|settings)\b/.test(new URL(request.url()).pathname));
  expect(initialWrites.length).toBeGreaterThanOrEqual(7);
  expect(initialWrites.every(request => request.headers()['x-salestrackr-generation'] === generation)).toBe(true);

  await page.goto(recordUrl);
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  await expect(page.locator('.note-block').first()).toContainText(originalNotes);
  await expect(page.getByText('Recovery contact', { exact: true })).toBeVisible();
  const selectedRestored = await (await page.request.get(`/api/records/${recordId}/detail`)).json() as RecordWorkspace;
  expect(selectedRestored.revision.id).toBe(beforeBackup.revision.id);
  expect(selectedRestored.generation).toBe(selectedRestoreGeneration);
  for (const staleGeneration of [null, generation]) {
    const rejected = await page.request.post('/api/writes/prepare', {
      headers: { 'X-SalesTrackr-Request': '1', Origin: baseURL!, ...(staleGeneration ? { 'X-SalesTrackr-Generation': staleGeneration } : {}) },
      data: { entityType: 'record', entityId: recordId },
    });
    expect(rejected.status()).toBe(409);
  }
  await editRequirement(page, 'Saved in the selected-restore generation; upload must roll this back.');
  const edit = requests.filter(request => request.url().endsWith(`/records/${recordId}/product-notes`)).at(-1)!;
  expect(edit.headers()['x-salestrackr-generation']).toBe(selectedRestoreGeneration);
  await page.goto(accountUrl);
  await page.getByRole('button', { name: 'Add contact', exact: true }).click();
  await page.getByLabel('Contact name *', { exact: true }).fill('Contact created after selected restore');
  await page.getByRole('button', { name: 'Save contact', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const renewedPrepare = requests.filter(request => request.url().endsWith('/writes/prepare')).at(-1)!;
  expect(renewedPrepare.headers()['x-salestrackr-generation']).toBe(selectedRestoreGeneration);
  await settings();
  await page.getByLabel('Or upload a ZIP or legacy JSON archive', { exact: true }).setInputFiles(archivePath);
  await inspect(backup);
  expect((await identity()).generation).toBe(selectedRestoreGeneration);
  const uploadInspection = requests.filter(request => request.url().endsWith('/api/backups/inspect')).at(-1)!;
  expect(uploadInspection.headers()['content-type']).toBe('application/zip');
  expect((await uploadInspection.allHeaders())['content-length']).toBe(String(archive.length));
  const uploadedRestoreGeneration = await restore(selectedRestoreGeneration);
  const uploadedRestore = requests.filter(request => request.url().endsWith('/api/backups/restore')).at(-1)!;
  const uploadedHeaders = await uploadedRestore.allHeaders();
  expect(uploadedHeaders).toMatchObject({
    'content-type': 'application/zip',
    'x-salestrackr-restore-confirm': 'RESTORE', 'x-salestrackr-request': '1', origin: baseURL,
  });
  expect(uploadedHeaders['x-salestrackr-restore-token']).toBeTruthy();
  expect(uploadedHeaders['x-salestrackr-write-id']).toMatch(/^[a-f0-9]{32}$/);
  expect(uploadedHeaders['content-length']).toBe(String(archive.length));
  await page.goto(recordUrl);
  await expect(page.locator('.note-block').first()).toContainText(originalNotes);
  await expect(page.getByText('Recovery contact', { exact: true })).toBeVisible();
  await editRequirement(page, 'Live write after uploaded ZIP restore.');
  expect(requests.filter(request => request.url().endsWith(`/records/${recordId}/product-notes`)).at(-1)!.headers()['x-salestrackr-generation']).toBe(uploadedRestoreGeneration);
  await page.goto(accountUrl);
  await expect(page.getByText('Contact created after selected restore', { exact: true })).toHaveCount(0);

  const legacyPath = testInfo.outputPath('legacy-backup.json');
  execFileSync(python, ['-c', [
    'import hashlib,json,sys',
    'from backend.app.backups import read_archive',
    'with open(sys.argv[1],"rb") as source:',
    ' revisions,_=read_archive(source.read())',
    'encoded=json.dumps(revisions,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()',
    'with open(sys.argv[2],"w",encoding="utf-8") as target:',
    ' json.dump({"format":1,"revisions":revisions,"checksum":hashlib.sha256(encoded).hexdigest()},target)',
  ].join('\n'), archivePath, legacyPath], { cwd: root });
  await settings();
  await page.getByLabel('Or upload a ZIP or legacy JSON archive', { exact: true }).setInputFiles(legacyPath);
  await inspect(backup);
  expect((await identity()).generation).toBe(uploadedRestoreGeneration);
  const legacyRestoreGeneration = await restore(uploadedRestoreGeneration);
  await page.goto(recordUrl);
  await expect(page.locator('.note-block').first()).toContainText(originalNotes);
  expect((await identity()).generation).toBe(legacyRestoreGeneration);
  expect((await status()).backups.some(row => row.source === 'pre-restore')).toBe(true);
  const mutationHeaders = await Promise.all(requests.filter(request => request.method() !== 'GET').map(request => request.allHeaders()));
  expect(mutationHeaders.every(headers => headers['x-salestrackr-request'] === '1' && headers.origin === baseURL)).toBe(true);
  expect(errors).toEqual([]);
});
