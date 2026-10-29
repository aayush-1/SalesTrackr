import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import type { ManufacturingDetail, ManufacturingMeta, ManufacturingRecordPreview } from '../src/manufacturing-types';

const identifier = () => crypto.randomUUID().replaceAll('-', '');
async function headers(request: APIRequestContext) {
  const meta = await (await request.get('/api/manufacturing/meta')).json() as ManufacturingMeta;
  return { 'X-SalesTrackr-Request': '1', 'X-ProductTrackr-Workspace': meta.workspaceId, 'X-ProductTrackr-Generation': meta.generation };
}
async function write(request: APIRequestContext, path: string, data: object, baseVersion: number | null = null) {
  const id = identifier();
  const response = await request.post(`/api/manufacturing${path}`, {
    headers: await headers(request), data: { writeId: identifier(), id, baseVersion, data },
  });
  expect(response.ok(), await response.text()).toBe(true); return id;
}
async function seed(request: APIRequestContext, name: string) {
  const supplierId = await write(request, '/suppliers', { name: `${name} Supplier` });
  const contactId = await write(request, '/contacts', { name: `${name} Contact`, supplierId });
  const operationId = await write(request, '/operations', { name: `${name} Operation` });
  const productId = await write(request, '/products', { name: `${name} Gear`, partNumber: `${name}-042` });
  const product = await (await request.get(`/api/manufacturing/products/${productId}`)).json() as ManufacturingDetail;
  const stepId = await write(request, `/products/${productId}/steps`, { operationId, instructions: 'Preserve this complete route' }, product.entity.version);
  await write(request, `/steps/${stepId}/assignments`, { supplierId, contactId }, 1);
  const preview = await (await request.get(`/api/manufacturing/records/preview/${productId}`)).json() as ManufacturingRecordPreview;
  const recordId = await write(request, '/records', { productId, sourceToken: preview.sourceToken, notes: 'Preserve this job and its history' });
  return { productId, recordId };
}
async function status(request: APIRequestContext) {
  return await (await request.get('/api/settings/backup-recovery')).json();
}
async function open(page: Page) {
  await page.goto('/manufacturing/settings?tab=recovery');
  await expect(page).toHaveURL(/\/settings\?tab=backup$/);
  await expect(page.getByRole('heading', { name: 'Backups · Local simulation', exact: true })).toBeVisible();
}
async function createBackup(page: Page) {
  await page.getByRole('button', { name: 'Create simulated backup', exact: true }).click();
  await expect(page.getByText('Local simulated backup created.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create simulated backup', exact: true })).toBeEnabled();
}

test('one backup downloads, previews and restores both Trackrs together', async ({ page, request }) => {
  const source = await seed(request, `Recovery${Date.now()}`);
  const salesBefore = await (await request.get('/api/accounts')).json();
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await open(page);
  await expect(page.getByText('Both Trackrs are included.', { exact: true })).toBeVisible();
  await createBackup(page);
  const saved = (await status(request)).backups[0];
  const download = page.waitForEvent('download');
  await page.getByRole('link', { name: `${saved.name} ZIP`, exact: true }).first().click();
  expect((await download).suggestedFilename()).toContain('Trackr-');
  const extraId = await write(request, '/products', { name: 'Created after browser backup' });
  const me = await (await request.get('/api/me')).json();
  const extraAccount = identifier();
  const salesWrite = await request.post('/api/accounts', {
    headers: { 'X-SalesTrackr-Request': '1', 'X-SalesTrackr-Generation': me.generation },
    data: { writeId: identifier(), entityId: extraAccount, baseRevision: null, data: { name: 'Created after combined backup' } },
  });
  expect(salesWrite.ok(), await salesWrite.text()).toBe(true);
  await page.getByLabel('Choose a simulated backup', { exact: true }).selectOption(saved.id);
  await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
  await expect(page.locator('.restore-preview')).toContainText('Products');
  await expect(page.locator('.restore-preview')).toContainText('Manufacturing records');
  await expect(page.getByRole('button', { name: 'Restore local demo workspace', exact: true })).toBeDisabled();
  await page.getByLabel('Type RESTORE to confirm full rollback', { exact: true }).fill('RESTORE');
  const navigation = page.waitForEvent('load');
  await page.getByRole('button', { name: 'Restore local demo workspace', exact: true }).click();
  await navigation;
  await expect(page.getByRole('heading', { name: 'Backups · Local simulation', exact: true })).toBeVisible();
  expect((await request.get(`/api/manufacturing/products/${extraId}`)).status()).toBe(404);
  const record = await (await request.get(`/api/manufacturing/records/${source.recordId}`)).json();
  expect(record.record.notes).toBe('Preserve this job and its history');
  expect((await status(request)).backups.some((row: { source: string }) => row.source === 'pre-restore')).toBe(true);
  expect((await (await request.get('/api/accounts')).json()).items).toEqual(salesBefore.items);
  expect((await (await request.get('/api/me')).json()).generation).not.toBe(me.generation);
  expect(errors).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('producttrackr-recovery-mobile.png'), fullPage: true });
});

test('unconfirmed backup retries the exact request and blocks navigation', async ({ page, request }) => {
  await seed(request, `Retry${Date.now()}`);
  await open(page);
  const before = (await status(request)).backups.length;
  const ids: string[] = [];
  await page.route('**/api/backups', async route => {
    ids.push(route.request().postDataJSON().writeId);
    const result = await route.fetch();
    if (ids.length === 1) return route.abort();
    return route.fulfill({ response: result });
  });
  await page.getByRole('button', { name: 'Create simulated backup', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Retry same backup', exact: true })).toBeEnabled();
  await page.getByRole('tab', { name: 'General', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Backup and recovery', exact: true })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('button', { name: 'Refresh backup status', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Retry same backup', exact: true })).toBeEnabled();
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'All sales records', exact: true }).click();
  await expect(page).toHaveURL(/settings\?tab=backup$/);
  await expect(page.getByText('Keep this page open until the current operation is confirmed.', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Retry same backup', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Create simulated backup', exact: true })).toBeEnabled();
  expect(ids).toHaveLength(2); expect(ids[1]).toBe(ids[0]);
  expect((await status(request)).backups.length).toBe(Math.min(10, before + 1));
});

test('uploaded archive restores reconcile a lost successful response', async ({ page, request }) => {
  await seed(request, `Upload${Date.now()}`);
  await open(page); await createBackup(page);
  const saved = (await status(request)).backups[0];
  const bytes = await (await request.get(`/api/backups/${saved.id}/download`)).body();
  await page.getByLabel('Or upload a combined ZIP archive', { exact: true }).setInputFiles({ name: 'Trackr-complete.zip', mimeType: 'application/zip', buffer: bytes });
  await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
  await expect(page.locator('.restore-preview')).toBeVisible();
  await page.getByLabel('Type RESTORE to confirm full rollback', { exact: true }).fill('RESTORE');
  const attempts: { id: string; body: Buffer | null }[] = [];
  await page.route('**/api/backups/restore', async route => {
    attempts.push({ id: route.request().headers()['x-salestrackr-write-id'], body: route.request().postDataBuffer() });
    const response = await route.fetch();
    expect(response.ok(), await response.text()).toBe(true);
    if (attempts.length === 1) return route.abort();
    return route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Restore local demo workspace', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Retry same restore', exact: true })).toBeEnabled();
  await expect(page.getByLabel('Or upload a combined ZIP archive', { exact: true })).toBeDisabled();
  const navigation = page.waitForEvent('load');
  await page.getByRole('button', { name: 'Retry same restore', exact: true }).click();
  await navigation;
  await expect(page.getByRole('heading', { name: 'Backups · Local simulation', exact: true })).toBeVisible();
  expect(attempts).toHaveLength(2); expect(attempts[1]).toEqual(attempts[0]);
});

test('invalid uploads and stale previews leave live data untouched', async ({ page, request }) => {
  await seed(request, `Validate${Date.now()}`);
  await open(page);
  await page.getByLabel('Or upload a combined ZIP archive', { exact: true }).setInputFiles({
    name: 'not-a-producttrackr.zip', mimeType: 'application/zip', buffer: Buffer.from('not a ZIP'),
  });
  await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
  await expect(page.getByText('Select a complete combined SalesTrackr + ProductTrackr ZIP.', { exact: false })).toBeVisible();
  await expect(page.locator('.restore-preview')).toHaveCount(0);
  await createBackup(page);
  const saved = (await status(request)).backups[0];
  await page.getByLabel('Choose a simulated backup', { exact: true }).selectOption(saved.id);
  await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
  await expect(page.locator('.restore-preview')).toBeVisible();
  const extraId = await write(request, '/products', { name: 'Must survive stale restore' });
  await page.getByLabel('Type RESTORE to confirm full rollback', { exact: true }).fill('RESTORE');
  await page.getByRole('button', { name: 'Restore local demo workspace', exact: true }).click();
  await expect(page.getByText('A workspace changed. Inspect the backup again.', { exact: true })).toBeVisible();
  expect((await request.get(`/api/manufacturing/products/${extraId}`)).ok()).toBe(true);
  await expect(page.locator('.restore-preview')).toHaveCount(0);
});

test('another session restore fences stale forms and exposes an explicit reload', async ({ page, request }) => {
  const source = await seed(request, `OtherSession${Date.now()}`);
  await page.goto(`/manufacturing/products/${source.productId}`);
  const row = (await (await request.post('/api/backups', {
    headers: await headers(request), data: { writeId: identifier() },
  })).json()).backup;
  const preview = await (await request.post('/api/backups/inspect', {
    headers: await headers(request), data: { backupId: row.id },
  })).json();
  const restored = await request.post('/api/backups/restore', {
    headers: await headers(request), data: { backupId: row.id, previewToken: preview.token, writeId: identifier(), confirmation: 'RESTORE' },
  });
  expect(restored.ok(), await restored.text()).toBe(true);
  await page.getByRole('button', { name: 'Refresh manufacturing data', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Reload ProductTrackr', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Reload ProductTrackr', exact: true }).click();
  await expect(page.getByRole('heading', { name: /OtherSession.*Gear/ })).toBeVisible();
});
