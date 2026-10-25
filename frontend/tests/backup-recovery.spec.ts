import { expect, test, type Page, type Request, type Route } from '@playwright/test';
import type { Backup, BackupSettings } from '../src/types';

const original: Backup = {
  id: 'original', name: 'original-migration.zip', createdAt: '2026-10-20T08:00:00Z', source: 'migration',
  protected: false, counts: { account: 10, contact: 8, record: 25, revisions: 70 }, sizeBytes: 2048,
};
const manual: Backup = { ...original, id: 'manual', name: 'manual-backup.zip', source: 'manual', protected: false };
const settings = (patch: Partial<BackupSettings> = {}): BackupSettings => ({
  mode: 'demo', storageBackend: 'firestore', location: 'Local simulated backup folder', folderUrl: null,
  schedule: { enabled: false, active: false, weekday: 'Sunday', time: '02:00', timezone: 'Asia/Kolkata' },
  retention: 10, backups: [original], lastBackupAt: original.createdAt, lastError: null,
  canBackup: true, canRestore: true, generation: 'generation-one', ...patch,
});
const preview = (backup = original, patch = {}) => ({
  token: 'preview-token', backup, generation: 'generation-one', warnings: ['This archive contains historical revisions.'],
  expiresAt: new Date(Date.now() + 600_000).toISOString(), ...patch,
});
type Mocks = { state: BackupSettings; requests: Request[]; handle?: (route: Route, path: string) => Promise<boolean> };
async function mockWorkspace(page: Page, patch: Partial<BackupSettings> = {}): Promise<Mocks> {
  const mock: Mocks = { state: settings(patch), requests: [] };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace('/api', '');
    mock.requests.push(request);
    if (await mock.handle?.(route, path)) return;
    if (path === '/me') return route.fulfill({ json: { email: 'local@example.test', mode: mock.state.mode, today: '2026-10-25', storageBackend: mock.state.storageBackend, generation: mock.state.generation } });
    if (path === '/status') return route.fulfill({ json: { complete: true, conflictCount: 0, syncedAt: original.createdAt, storageBackend: mock.state.storageBackend, generation: mock.state.generation } });
    if (path === '/settings/backup-recovery') {
      if (request.method() === 'POST') mock.state.schedule.enabled = request.postDataJSON().weeklyEnabled;
      return route.fulfill({ json: mock.state });
    }
    if (path === '/accounts/matches') return route.fulfill({ json: { name: new URL(request.url()).searchParams.get('name'), total: 0, exactCount: 0, items: [], complete: true, conflictCount: 0 } });
    if (path === '/backups/inspect') return route.fulfill({ json: preview() });
    if (path === '/backups') {
      mock.state = { ...mock.state, backups: [manual, original], lastBackupAt: manual.createdAt };
      return route.fulfill({ json: { backup: manual } });
    }
    if (path === '/dashboard') return route.fulfill({ json: { counts: {}, complete: true, conflictCount: 0, syncedAt: original.createdAt } });
    if (path.startsWith('/accounts') || path.startsWith('/records') || path.startsWith('/contacts') || path === '/conflicts') return route.fulfill({ json: { items: [], total: 0, complete: true, conflictCount: 0 } });
    return route.fulfill({ status: 404, json: { error: { message: `Unexpected mock request: ${path}`, code: 'not_found' } } });
  });
  return mock;
}
const restores = (mock: Mocks) => mock.requests.filter(request => request.url().endsWith('/api/backups/restore'));

test('lower-left Settings navigation, tabs, mobile navigation and honest local simulation', async ({ page }) => {
  await mockWorkspace(page);
  await page.goto('/');
  const gear = page.locator('.sidebar-bottom').getByRole('link', { name: 'Settings', exact: true });
  await expect(gear).toBeVisible();
  await expect(gear).toHaveText('');
  expect((await gear.boundingBox())!.width).toBeLessThanOrEqual(32);
  await expect(page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Settings', exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Backup and recovery' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('Local simulation only.', { exact: false })).toBeVisible();
  await page.getByRole('tab', { name: 'General', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Workspace storage' })).toBeVisible();
  await page.getByRole('tab', { name: 'Backup and recovery' }).click();
  await expect(page.getByRole('heading', { name: 'Weekly backups', exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(page.locator('.sidebar')).toHaveClass(/open/);
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await expect(page.locator('.sidebar')).not.toHaveClass(/open/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('manual readable backup, Drive discovery, ten total copies and weekly preference', async ({ page }) => {
  const mock = await mockWorkspace(page, { mode: 'production', location: 'Private Drive folder', folderUrl: 'https://drive.google.com/drive/folders/private' });
  await page.goto('/settings');
  await expect(page.getByRole('link', { name: 'Open private Drive folder' })).toHaveAttribute('href', mock.state.folderUrl!);
  await expect(page.getByText('Protected · never rotated out')).toHaveCount(0);
  await expect(page.getByText('This list comes from your private Drive backup folder.', { exact: false })).toBeVisible();
  for (const name of ['accounts.json', 'contacts.json', 'sales-records.json']) {
    await expect(page.getByText(name, { exact: true })).toBeVisible();
  }
  await page.getByRole('button', { name: 'Back up now to Drive' }).click();
  await expect(page.getByText('Backup created directly in your private Google Drive folder.')).toBeVisible();
  await expect(page.getByText('manual-backup.zip', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: 'manual-backup.zip ZIP' })).toHaveAttribute('href', '/api/backups/manual/download');
  const request = mock.requests.find(request => request.method() === 'POST' && request.url().endsWith('/api/backups'))!;
  expect(request.postDataJSON().writeId).toMatch(/^[a-f0-9]{32}$/);
  expect(Object.keys(request.postDataJSON())).toEqual(['writeId']);
  await page.getByLabel('Enable weekly backups').check();
  await expect(page.getByText('Weekly schedule preference saved.', { exact: false })).toBeVisible();
  expect(mock.state.schedule.enabled).toBe(true);
  await expect(page.getByText('Keep only the 10 newest backups in total', { exact: false })).toBeVisible();
});

test('backup double clicks and interrupted response keep one exact operation ID', async ({ page }) => {
  const mock = await mockWorkspace(page);
  let attempts = 0;
  mock.handle = async (route, path) => {
    if (path !== '/backups') return false;
    attempts++;
    if (attempts === 1) { await route.abort(); return true; }
    await route.fulfill({ json: { backup: manual } }); return true;
  };
  await page.goto('/settings');
  await page.getByRole('button', { name: 'Create simulated backup' }).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  await expect(page.getByRole('button', { name: 'Retry same backup' })).toBeVisible();
  expect(attempts).toBe(1);
  await expect(page.getByLabel('Enable weekly backups')).toBeDisabled();
  await expect(page.getByText('Local simulated backup created.', { exact: false })).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry same backup' }).click();
  await expect(page.getByText('Nothing was uploaded to Google Drive.', { exact: false })).toBeVisible();
  const writes = mock.requests.filter(request => request.url().endsWith('/api/backups'));
  expect(writes).toHaveLength(2);
  expect(writes[0].postData()).toBe(writes[1].postData());
});

test('Drive selection only previews, typed confirmation, pending progress and confirmed restore forces reload', async ({ page }) => {
  const mock = await mockWorkspace(page, { mode: 'production' });
  let finish: (() => void) | undefined;
  mock.handle = async (route, path) => {
    if (path !== '/backups/restore') return false;
    await new Promise<void>(resolve => { finish = resolve; });
    mock.state.generation = 'generation-two';
    await route.fulfill({ json: { restored: true, generation: 'generation-two', counts: original.counts, recoveryBackup: manual } });
    return true;
  };
  await page.goto('/settings');
  await page.getByLabel('Choose a Drive backup').selectOption('original');
  expect(restores(mock)).toHaveLength(0);
  expect(mock.requests.filter(request => request.url().endsWith('/inspect'))).toHaveLength(0);
  await page.getByRole('button', { name: 'Preview and validate backup' }).click();
  await expect(page.getByLabel('Restore preview')).toContainText('70');
  await expect(page.getByText('This archive contains historical revisions.')).toBeVisible();
  await expect(page.getByText('Full rollback, not a merge.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Restore entire workspace' })).toBeDisabled();
  await page.getByLabel('Type RESTORE to confirm full rollback').fill('restore');
  await expect(page.getByRole('button', { name: 'Restore entire workspace' })).toBeDisabled();
  await page.getByLabel('Type RESTORE to confirm full rollback').fill('RESTORE');
  const identities = () => mock.requests.filter(request => request.url().endsWith('/api/me')).length;
  const before = identities();
  await page.getByRole('button', { name: 'Restore entire workspace' }).click();
  await expect(page.getByText('Restoring and verifying your workspace.', { exact: false })).toBeVisible();
  await expect(page.getByLabel('Choose a Drive backup')).toBeDisabled();
  expect(identities()).toBe(before);
  await expect.poll(() => !!finish).toBe(true);
  finish!();
  await expect.poll(identities).toBeGreaterThan(before);
  await expect(page.getByLabel('Type RESTORE to confirm full rollback')).toHaveCount(0);
  const body = restores(mock)[0].postDataJSON();
  expect(body).toMatchObject({ backupId: 'original', previewToken: 'preview-token', confirmation: 'RESTORE' });
  expect(body.writeId).toMatch(/^[a-f0-9]{32}$/);
});

test('ZIP upload validates extension and size, clears preview, sends raw bytes and exact retry headers', async ({ page }) => {
  const mock = await mockWorkspace(page);
  let attempts = 0;
  mock.handle = async (route, path) => {
    if (path !== '/backups/restore') return false;
    attempts++;
    if (attempts === 1) await route.fulfill({ status: 503, json: { error: { message: 'Restore is still in progress.', code: 'restore_in_progress' } } });
    else await route.fulfill({ status: 409, json: { error: { message: 'Live data changed. Inspect again.', code: 'stale_preview' } } });
    return true;
  };
  await page.goto('/settings');
  const upload = page.getByLabel('Or upload a ZIP or legacy JSON archive');
  await upload.setInputFiles({ name: 'backup.txt', mimeType: 'text/plain', buffer: Buffer.from('{}') });
  await expect(page.getByText('Choose a .zip or legacy .json backup archive.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Preview and validate backup' })).toBeDisabled();
  await upload.evaluate((input: HTMLInputElement) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array(64 * 1024 * 1024 + 1)], 'too-big.zip', { type: 'application/zip' }));
    input.files = transfer.files; input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await expect(page.getByText('Backup archive exceeds the 64 MiB file size limit.')).toBeVisible();
  const bytes = Buffer.from('PK-plain-ZIP-archive');
  await upload.setInputFiles({ name: 'backup.zip', mimeType: 'application/zip', buffer: bytes });
  expect(restores(mock)).toHaveLength(0);
  await page.getByRole('button', { name: 'Preview and validate backup' }).click();
  await expect(page.getByLabel('Restore preview')).toBeVisible();
  const inspect = mock.requests.filter(request => request.url().endsWith('/inspect')).at(-1)!;
  expect(inspect.headers()['content-type']).toBe('application/zip');
  expect(inspect.postDataBuffer()).toEqual(bytes);
  await page.getByLabel('Choose a simulated backup').selectOption('original');
  await expect(page.getByLabel('Restore preview')).toHaveCount(0);
  await upload.setInputFiles({ name: 'backup.zip', mimeType: 'application/zip', buffer: bytes });
  await page.getByRole('button', { name: 'Preview and validate backup' }).click();
  await page.getByLabel('Type RESTORE to confirm full rollback').fill('RESTORE');
  await page.getByRole('button', { name: 'Restore local demo workspace' }).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  await expect(page.getByRole('button', { name: 'Retry same restore' })).toBeVisible();
  expect(attempts).toBe(1);
  await expect(upload).toBeDisabled();
  await expect(page.getByText('Restore confirmed.', { exact: false })).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry same restore' }).click();
  await expect(page.getByText('Live data changed. Inspect again.')).toBeVisible();
  await expect(page.getByLabel('Restore preview')).toHaveCount(0);
  const writes = restores(mock);
  expect(writes).toHaveLength(2);
  for (const write of writes) {
    expect(write.postDataBuffer()).toEqual(bytes);
    expect(write.headers()).toMatchObject({
      'content-type': 'application/zip', 'x-salestrackr-restore-token': 'preview-token', 'x-salestrackr-restore-confirm': 'RESTORE',
    });
    expect(write.headers()['x-salestrackr-write-id']).toMatch(/^[a-f0-9]{32}$/);
  }
  expect(writes[0].headers()['x-salestrackr-write-id']).toBe(writes[1].headers()['x-salestrackr-write-id']);
});

test('expired previews and invalid server archives fail closed; demo schedule does not claim a scheduler', async ({ page }) => {
  const mock = await mockWorkspace(page, { lastError: 'Last weekly backup failed.' });
  let inspected = 0;
  mock.handle = async (route, path) => {
    if (path !== '/backups/inspect') return false;
    inspected++;
    if (inspected === 1) await route.fulfill({ status: 422, json: { error: { message: 'Invalid ZIP archive.', code: 'invalid_archive' } } });
    else await route.fulfill({ json: preview(original, { expiresAt: '2020-01-01T00:00:00Z' }) });
    return true;
  };
  await page.goto('/settings');
  await expect(page.getByText('Last backup error: Last weekly backup failed.')).toBeVisible();
  await page.getByLabel('Enable weekly backups').check();
  await expect(page.getByText('Schedule preference saved locally. No cloud scheduler is running.')).toBeVisible();
  await page.getByLabel('Choose a simulated backup').selectOption('original');
  await page.getByRole('button', { name: 'Preview and validate backup' }).click();
  await expect(page.getByText('Invalid ZIP archive.')).toBeVisible();
  expect(restores(mock)).toHaveLength(0);
  await page.getByRole('button', { name: 'Preview and validate backup' }).click();
  await expect(page.getByText('This preview has expired or the workspace has changed.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Restore local demo workspace' })).toBeDisabled();
});

test('Drive production migration disabled and settings load failures never fake success', async ({ page }) => {
  const mock = await mockWorkspace(page, { mode: 'production', storageBackend: 'drive', generation: null, canBackup: false, canRestore: false });
  await page.goto('/settings');
  await expect(page.getByRole('tabpanel', { name: 'Backup and recovery' }).getByText('Firestore migration is not enabled.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Back up now to Drive' })).toBeDisabled();
  await expect(page.getByLabel('Enable weekly backups')).toBeDisabled();
  await expect(page.getByLabel('Choose a Drive backup')).toBeDisabled();
  mock.handle = async (route, path) => {
    if (path !== '/settings/backup-recovery') return false;
    await route.fulfill({ status: 500, json: { error: { message: 'Backup storage unavailable.', code: 'unavailable' } } }); return true;
  };
  await page.getByRole('button', { name: 'Refresh backup status' }).click();
  await expect(page.getByText('Backup storage unavailable.', { exact: false })).toBeVisible();
  expect(restores(mock)).toHaveLength(0);
});

test('initial identity gates forms, prepares and exact save retries bind the original generation', async ({ page }) => {
  const mock = await mockWorkspace(page);
  let ready: (() => void) | undefined;
  let firstSave = true;
  const revision = { id: 'account-revision', entityId: 'account-id', entityType: 'account', parents: [], createdAt: original.createdAt, actor: 'local', action: 'created', reason: '', data: { name: 'Generation company', address: '', taxReference: '', notes: '' } };
  mock.handle = async (route, path) => {
    if (path === '/me') {
      await new Promise<void>(resolve => { ready = resolve; });
      await route.fulfill({ json: { mode: 'demo', today: '2026-10-25', storageBackend: 'firestore', generation: 'generation-one' } }); return true;
    }
    if (path === '/writes/prepare') { await route.fulfill({ json: { writeId: 'a'.repeat(32), entityId: 'account-id' } }); return true; }
    if (path === '/accounts' && route.request().method() === 'POST') {
      if (firstSave) { firstSave = false; await route.abort(); }
      else await route.fulfill({ json: { revision, complete: true, conflictCount: 0, warning: null, revisionIsCurrent: true, syncedAt: original.createdAt } });
      return true;
    }
    if (path === '/entities/account/account-id') { await route.fulfill({ json: { revision, complete: true, conflictCount: 0 } }); return true; }
    return false;
  };
  await page.goto('/accounts/new');
  await expect.poll(() => !!ready).toBe(true);
  await expect(page.getByLabel('Company name *')).toHaveCount(0);
  ready!();
  await page.getByLabel('Company name *').fill('Generation company');
  await expect(page.getByRole('button', { name: 'Create account', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByText('Connection lost.', { exact: false })).toBeVisible();
  await page.getByLabel('Account notes', { exact: true }).fill('Changed unknown save');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByText('The previous save is unconfirmed.', { exact: false })).toBeVisible();
  await page.getByLabel('Account notes', { exact: true }).fill('');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Generation company', exact: true })).toBeVisible();
  const writes = mock.requests.filter(request => request.url().endsWith('/api/accounts') && request.method() === 'POST');
  expect(writes).toHaveLength(2);
  expect(writes[0].postData()).toBe(writes[1].postData());
  const prepare = mock.requests.find(request => request.url().endsWith('/writes/prepare'))!;
  expect(prepare.headers()['x-salestrackr-generation']).toBe('generation-one');
  expect(writes[0].headers()['x-salestrackr-generation']).toBe('generation-one');
});

test('open forms never rebind after a generation switch; unknown Firestore generation blocks prepares', async ({ page }) => {
  const mock = await mockWorkspace(page);
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *').fill('Open before restore');
  await expect(page.getByRole('button', { name: 'Create account', exact: true })).toBeEnabled();
  mock.state.generation = 'generation-two';
  await page.getByRole('button', { name: 'Refresh data', exact: true }).click();
  await expect(page.getByText('The workspace changed.', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByText('Refresh this page and reopen the form before saving.', { exact: false })).toBeVisible();
  expect(mock.requests.filter(request => request.url().endsWith('/writes/prepare'))).toHaveLength(0);
  mock.state.generation = null;
  await page.reload();
  await page.getByLabel('Company name *').fill('Unknown generation');
  await expect(page.getByRole('button', { name: 'Create account', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByText('generation is unknown.', { exact: false })).toBeVisible();
  expect(mock.requests.filter(request => request.url().endsWith('/writes/prepare'))).toHaveLength(0);
});

test('existing contact edits retain browser UUID optimization and generation-bound exact retries', async ({ page }) => {
  const mock = await mockWorkspace(page);
  const account = { id: 'account-version', entityId: 'account-id', entityType: 'account', data: { name: 'Existing company' } };
  const contact = { id: 'contact-version', entityId: 'contact-id', entityType: 'contact', parents: [], data: { accountId: 'account-id', name: 'Existing contact', email: '', phone: '', designation: '' } };
  let attempts = 0;
  mock.handle = async (route, path) => {
    if (path === '/entities/account/account-id') { await route.fulfill({ json: { revision: account, complete: true, conflictCount: 0 } }); return true; }
    if (path === '/contacts') { await route.fulfill({ json: { items: [contact] } }); return true; }
    if (path === '/contacts/contact-id') {
      attempts++;
      if (attempts === 1) await route.fulfill({ status: 503, json: { error: { message: 'Contact save unconfirmed.', code: 'save_unknown' } } });
      else {
        contact.data.designation = 'Updated designation';
        await route.fulfill({ json: { revision: { ...contact, id: 'contact-version-two', parents: [contact.id] }, complete: true, revisionIsCurrent: true, conflictCount: 0, warning: null, syncedAt: original.createdAt } });
      }
      return true;
    }
    return false;
  };
  await page.goto('/accounts/account-id');
  await page.getByRole('button', { name: 'Edit Existing contact', exact: true }).click();
  await page.getByLabel('Designation', { exact: true }).fill('Updated designation');
  await page.getByRole('button', { name: 'Save contact', exact: true }).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  await expect(page.getByText('Contact save unconfirmed.')).toBeVisible();
  expect(attempts).toBe(1);
  await page.getByRole('button', { name: 'Save contact', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('Updated designation', { exact: true })).toBeVisible();
  const writes = mock.requests.filter(request => request.url().endsWith('/api/contacts/contact-id'));
  expect(writes).toHaveLength(2);
  expect(writes[0].postData()).toBe(writes[1].postData());
  expect(writes[0].postDataJSON()).toMatchObject({ entityId: 'contact-id', baseRevision: 'contact-version' });
  expect(writes[0].postDataJSON().writeId).toMatch(/^[a-f0-9]{32}$/);
  expect(writes.every(request => request.headers()['x-salestrackr-generation'] === 'generation-one')).toBe(true);
  expect(mock.requests.filter(request => request.url().endsWith('/writes/prepare'))).toHaveLength(0);
});

test('conflict prepares and resolutions retain original generation and exact retry body', async ({ page }) => {
  const mock = await mockWorkspace(page);
  let attempts = 0;
  let resolved = false;
  mock.handle = async (route, path) => {
    if (path === '/conflicts') {
      await route.fulfill({ json: { items: resolved ? [] : [{ entityId: 'conflicted-account', entityType: 'account', versions: ['version-one', 'version-two'].map(id => ({ id, createdAt: original.createdAt, data: { name: id } })) }] } }); return true;
    }
    if (path === '/writes/prepare') { await route.fulfill({ json: { writeId: 'b'.repeat(32), entityId: 'conflicted-account' } }); return true; }
    if (path === '/entities/account/conflicted-account/resolve') {
      attempts++;
      if (attempts === 1) await route.abort();
      else { resolved = true; await route.fulfill({ json: { complete: true } }); }
      return true;
    }
    return false;
  };
  await page.goto('/conflicts');
  await page.getByLabel('Resolution reason').fill('Reviewed both versions');
  await page.getByRole('button', { name: 'Keep selected version', exact: true }).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  await expect(page.getByText('Connection lost.', { exact: false })).toBeVisible();
  expect(attempts).toBe(1);
  await page.getByLabel('Resolution reason').fill('Cannot change an unknown save');
  await page.getByRole('button', { name: 'Keep selected version', exact: true }).click();
  await expect(page.getByText('Retry the original resolution before changing it.')).toBeVisible();
  expect(attempts).toBe(1);
  await page.getByLabel('Resolution reason').fill('Reviewed both versions');
  await page.getByRole('button', { name: 'Keep selected version', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'No conflicting versions' })).toBeVisible();
  const writes = mock.requests.filter(request => request.url().endsWith('/resolve'));
  expect(writes).toHaveLength(2);
  expect(writes[0].postData()).toBe(writes[1].postData());
  const prepares = mock.requests.filter(request => request.url().endsWith('/writes/prepare'));
  expect(prepares).toHaveLength(1);
  expect([...writes, ...prepares].every(request => request.headers()['x-salestrackr-generation'] === 'generation-one')).toBe(true);
});

test('ordinary production users can back up but cannot restore or change the schedule', async ({ page }) => {
  await mockWorkspace(page, { mode: 'production', canRestore: false });
  await page.goto('/settings');
  await expect(page.getByText('Restore and schedule changes require a recovery administrator.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Back up now to Drive' })).toBeEnabled();
  await expect(page.getByLabel('Enable weekly backups')).toBeDisabled();
  await expect(page.getByLabel('Choose a Drive backup')).toBeDisabled();
  await expect(page.getByLabel('Or upload a ZIP or legacy JSON archive')).toBeDisabled();
  await expect(page.getByText('Scheduled backups are not active.', { exact: false })).toBeVisible();
});

test('legacy JSON archives use raw archive transport and restore headers, not multipart or encoded data', async ({ page }) => {
  const mock = await mockWorkspace(page, { storageBackend: 'drive', generation: null });
  const bytes = Buffer.from('{"format":"legacy-validated-backup"}');
  mock.handle = async (route, path) => {
    if (path === '/backups/inspect') { await route.fulfill({ json: preview(original, { generation: 'local-generation' }) }); return true; }
    if (path === '/backups/restore') {
      await route.fulfill({ status: 422, json: { error: { message: 'Legacy archive validation failed.', code: 'invalid_archive' } } }); return true;
    }
    return false;
  };
  // A local Drive-like demo may simulate recovery when the backend advertises the capability.
  mock.state.generation = 'local-generation';
  await page.goto('/settings');
  await page.getByLabel('Or upload a ZIP or legacy JSON archive').setInputFiles({ name: 'legacy.json', mimeType: 'application/json', buffer: bytes });
  await page.getByRole('button', { name: 'Preview and validate backup' }).click();
  await page.getByLabel('Type RESTORE to confirm full rollback').fill('RESTORE');
  await page.getByRole('button', { name: 'Restore local demo workspace' }).click();
  await expect(page.getByText('Legacy archive validation failed.')).toBeVisible();
  for (const request of mock.requests.filter(request => /\/backups\/(inspect|restore)$/.test(request.url()))) {
    expect(request.headers()['content-type']).toBe('application/zip');
    expect(request.postDataBuffer()).toEqual(bytes);
    if (request.url().endsWith('/restore')) expect(request.headers()['x-salestrackr-restore-token']).toBe('preview-token');
  }
  await expect(page.getByLabel('Restore preview')).toHaveCount(0);
});
