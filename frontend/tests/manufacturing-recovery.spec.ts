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
  const partnerId = await write(request, '/partners', { name: `${name} Supplier` });
  const contactId = await write(request, '/contacts', { name: `${name} Contact`, partnerId });
  const operationId = await write(request, '/operations', { name: `${name} Operation` });
  const productId = await write(request, '/products', { name: `${name} Gear`, reference: `${name}-042` });
  const product = await (await request.get(`/api/manufacturing/products/${productId}`)).json() as ManufacturingDetail;
  const stepId = await write(request, `/products/${productId}/steps`, { operationId, instructions: 'Preserve this complete route' }, product.entity.version);
  await write(request, `/steps/${stepId}/assignments`, { partnerId, contactId }, 1);
  const preview = await (await request.get(`/api/manufacturing/records/preview/${productId}`)).json() as ManufacturingRecordPreview;
  const recordId = await write(request, '/records', { productId, sourceToken: preview.sourceToken, notes: 'Preserve this job and its history' });
  return { productId, recordId };
}
async function status(request: APIRequestContext) {
  return await (await request.get('/api/manufacturing/settings/backup-recovery')).json();
}
async function open(page: Page) {
  await page.goto('/manufacturing/settings?tab=recovery');
  await expect(page.getByRole('heading', { name: 'ProductTrackr backups', exact: true })).toBeVisible();
}
async function createBackup(page: Page) {
  await page.getByRole('button', { name: 'Create local backup', exact: true }).click();
  await expect(page.getByText('ProductTrackr backup verified locally.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create local backup', exact: true })).toBeEnabled();
}

test('complete backup downloads, previews and restores without touching SalesTrackr', async ({ page, request }) => {
  const source = await seed(request, `Recovery${Date.now()}`);
  const salesBefore = await (await request.get('/api/accounts')).json();
  const salesCalls: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', route => {
    if (route.request().url().includes('/api/manufacturing/')) return route.continue();
    salesCalls.push(route.request().url()); return route.abort();
  });
  await open(page);
  await expect(page.getByText('10 newest verified backups total', { exact: true })).toBeVisible();
  await createBackup(page);
  const saved = (await status(request)).backups[0];
  const download = page.waitForEvent('download');
  await page.getByRole('link', { name: `ZIP ${saved.name}`, exact: true }).first().click();
  expect((await download).suggestedFilename()).toContain('ProductTrackr-');
  const extraId = await write(request, '/products', { name: 'Created after browser backup' });
  await page.getByLabel('Choose a ProductTrackr backup', { exact: true }).selectOption(saved.id);
  await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
  await expect(page.locator('.restore-preview')).toContainText('Activity events');
  await expect(page.locator('.restore-preview')).toContainText('Number counters');
  await expect(page.getByRole('button', { name: 'Restore ProductTrackr workspace', exact: true })).toBeDisabled();
  await page.getByLabel('Type RESTORE to confirm full rollback', { exact: true }).fill('RESTORE');
  const navigation = page.waitForEvent('load');
  await page.getByRole('button', { name: 'Restore ProductTrackr workspace', exact: true }).click();
  await navigation;
  await expect(page.getByRole('heading', { name: 'ProductTrackr backups', exact: true })).toBeVisible();
  expect((await request.get(`/api/manufacturing/products/${extraId}`)).status()).toBe(404);
  const record = await (await request.get(`/api/manufacturing/records/${source.recordId}`)).json();
  expect(record.record.notes).toBe('Preserve this job and its history');
  expect((await status(request)).backups.some((row: { source: string }) => row.source === 'pre-restore')).toBe(true);
  expect((await (await request.get('/api/accounts')).json()).items).toEqual(salesBefore.items);
  expect(salesCalls).toEqual([]); expect(errors).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('producttrackr-recovery-mobile.png'), fullPage: true });
});

test('unconfirmed backup retries the exact request and blocks navigation', async ({ page, request }) => {
  await seed(request, `Retry${Date.now()}`);
  await open(page);
  const before = (await status(request)).backups.length;
  const ids: string[] = [];
  await page.route('**/api/manufacturing/backups', async route => {
    ids.push(route.request().postDataJSON().writeId);
    const result = await route.fetch();
    if (ids.length === 1) return route.abort();
    return route.fulfill({ response: result });
  });
  await page.getByRole('button', { name: 'Create local backup', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Retry same backup', exact: true })).toBeEnabled();
  await page.getByRole('tab', { name: 'General', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Data & recovery', exact: true })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('button', { name: 'Refresh manufacturing data', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Retry same backup', exact: true })).toBeEnabled();
  await page.getByRole('navigation', { name: 'Manufacturing navigation' }).getByRole('link', { name: 'Products', exact: true }).click();
  await expect(page).toHaveURL(/settings\?tab=recovery$/);
  await expect(page.getByText('Keep this page open until recovery is confirmed.', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Retry same backup', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Create local backup', exact: true })).toBeEnabled();
  expect(ids).toHaveLength(2); expect(ids[1]).toBe(ids[0]);
  expect((await status(request)).backups.length).toBe(Math.min(10, before + 1));
});

test('uploaded archive restores reconcile a lost successful response', async ({ page, request }) => {
  await seed(request, `Upload${Date.now()}`);
  await open(page); await createBackup(page);
  const saved = (await status(request)).backups[0];
  const bytes = await (await request.get(`/api/manufacturing/backups/${saved.id}/download`)).body();
  await page.getByLabel('Or upload a ProductTrackr ZIP', { exact: true }).setInputFiles({ name: 'ProductTrackr-complete.zip', mimeType: 'application/zip', buffer: bytes });
  await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
  await expect(page.locator('.restore-preview')).toBeVisible();
  await page.getByLabel('Type RESTORE to confirm full rollback', { exact: true }).fill('RESTORE');
  const attempts: { id: string; body: Buffer | null }[] = [];
  await page.route('**/api/manufacturing/backups/restore', async route => {
    attempts.push({ id: route.request().headers()['x-producttrackr-write-id'], body: route.request().postDataBuffer() });
    const response = await route.fetch();
    expect(response.ok(), await response.text()).toBe(true);
    if (attempts.length === 1) return route.abort();
    return route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Restore ProductTrackr workspace', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Retry same restore', exact: true })).toBeEnabled();
  await expect(page.getByLabel('Or upload a ProductTrackr ZIP', { exact: true })).toBeDisabled();
  const navigation = page.waitForEvent('load');
  await page.getByRole('button', { name: 'Retry same restore', exact: true }).click();
  await navigation;
  await expect(page.getByRole('heading', { name: 'ProductTrackr backups', exact: true })).toBeVisible();
  expect(attempts).toHaveLength(2); expect(attempts[1]).toEqual(attempts[0]);
});

test('invalid uploads and stale previews leave live data untouched', async ({ page, request }) => {
  await seed(request, `Validate${Date.now()}`);
  await open(page);
  await page.getByLabel('Or upload a ProductTrackr ZIP', { exact: true }).setInputFiles({
    name: 'not-a-producttrackr.zip', mimeType: 'application/zip', buffer: Buffer.from('not a ZIP'),
  });
  await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
  await expect(page.getByText('Select a complete, supported ProductTrackr backup', { exact: false })).toBeVisible();
  await expect(page.locator('.restore-preview')).toHaveCount(0);
  await createBackup(page);
  const saved = (await status(request)).backups[0];
  await page.getByLabel('Choose a ProductTrackr backup', { exact: true }).selectOption(saved.id);
  await page.getByRole('button', { name: 'Preview and validate backup', exact: true }).click();
  await expect(page.locator('.restore-preview')).toBeVisible();
  const extraId = await write(request, '/products', { name: 'Must survive stale restore' });
  await page.getByLabel('Type RESTORE to confirm full rollback', { exact: true }).fill('RESTORE');
  await page.getByRole('button', { name: 'Restore ProductTrackr workspace', exact: true }).click();
  await expect(page.getByText('Live data changed. Inspect the backup again before restoring.', { exact: true })).toBeVisible();
  expect((await request.get(`/api/manufacturing/products/${extraId}`)).ok()).toBe(true);
  await expect(page.locator('.restore-preview')).toHaveCount(0);
});

test('another session restore fences stale forms and exposes an explicit reload', async ({ page, request }) => {
  const source = await seed(request, `OtherSession${Date.now()}`);
  await page.goto(`/manufacturing/products/${source.productId}`);
  const row = (await (await request.post('/api/manufacturing/backups', {
    headers: await headers(request), data: { writeId: identifier() },
  })).json()).backup;
  const preview = await (await request.post('/api/manufacturing/backups/inspect', {
    headers: await headers(request), data: { backupId: row.id },
  })).json();
  const restored = await request.post('/api/manufacturing/backups/restore', {
    headers: await headers(request), data: { backupId: row.id, previewToken: preview.token, writeId: identifier(), confirmation: 'RESTORE' },
  });
  expect(restored.ok(), await restored.text()).toBe(true);
  await page.getByRole('button', { name: 'Refresh manufacturing data', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Reload ProductTrackr', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Reload ProductTrackr', exact: true }).click();
  await expect(page.getByRole('heading', { name: /OtherSession.*Gear/ })).toBeVisible();
});
