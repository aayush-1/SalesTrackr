import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import type { ManufacturingDetail, ManufacturingRecord, ManufacturingRecordPreview } from '../src/manufacturing-types';

const identifier = () => crypto.randomUUID().replaceAll('-', '');
async function write(request: APIRequestContext, path: string, data: object, baseVersion: number | null = null, id = identifier()) {
  const meta = await (await request.get('/api/manufacturing/meta')).json();
  const response = await request.post(`/api/manufacturing${path}`, {
    headers: { 'X-SalesTrackr-Request': '1', 'X-ProductTrackr-Workspace': meta.workspaceId, 'X-ProductTrackr-Generation': meta.generation },
    data: { writeId: identifier(), id, baseVersion, data },
  });
  expect(response.ok(), await response.text()).toBe(true); return id;
}
async function record(request: APIRequestContext, id: string) {
  return (await (await request.get(`/api/manufacturing/records/${id}`)).json()).record as ManufacturingRecord;
}
async function action(request: APIRequestContext, current: ManufacturingRecord, name: string, data: object) {
  await write(request, `/records/${current.id}/${name}`, data, current.version, current.id);
  return record(request, current.id);
}
async function seed(request: APIRequestContext) {
  const name = `Cleanup${identifier().slice(0, 8)}`;
  const supplierId = await write(request, '/suppliers', { name: `${name} Workshop` });
  const productId = await write(request, '/products', { name: `${name} Gear`, partNumber: `${name}-042` });
  const operationId = await write(request, '/operations', { name: `${name} Inspection` });
  const product = await (await request.get(`/api/manufacturing/products/${productId}`)).json() as ManufacturingDetail;
  const stepId = await write(request, `/products/${productId}/steps`, { operationId }, product.entity.version);
  await write(request, `/steps/${stepId}/assignments`, { supplierId }, 1);
  const create = async () => {
    const preview = await (await request.get(`/api/manufacturing/records/preview/${productId}`)).json() as ManufacturingRecordPreview;
    return record(request, await write(request, '/records', { productId, sourceToken: preview.sourceToken }));
  };
  let completed = await create();
  completed = await action(request, completed, 'assign', { stepId: completed.operations[0].id, supplierId });
  completed = await action(request, completed, 'complete', { stepId: completed.operations[0].id });
  return { completed, create, productId };
}
async function open(page: Page) {
  await page.goto('/manufacturing/settings?tab=cleanup');
  await expect(page.getByRole('heading', { name: 'Completed record cleanup', exact: true })).toBeVisible();
}
async function select(page: Page, current: ManufacturingRecord) {
  await page.getByRole('checkbox', { name: `Select ${current.number}`, exact: true }).check();
  await page.getByRole('button', { name: 'Delete selected (1)', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Permanently delete manufacturing records?', exact: true })).toBeVisible();
}

test('only selected completed jobs are deleted after confirmation and a verified backup', async ({ page, request }) => {
  const source = await seed(request);
  const waiting = await source.create();
  const cancelled = await action(request, await source.create(), 'cancel', { reason: 'Cancelled jobs are not completed' });
  const productBefore = await (await request.get(`/api/manufacturing/products/${source.productId}`)).json();
  const salesBefore = (await (await request.get('/api/accounts')).json()).items;
  const salesCalls: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', route => {
    if (route.request().url().includes('/api/manufacturing/')) return route.continue();
    if (!/\/api\/(?:me|status|settings\/backup-recovery)(?:\?|$)/.test(route.request().url())) salesCalls.push(route.request().url());
    return route.continue();
  });
  await open(page);
  await expect(page.getByRole('checkbox', { name: `Select ${waiting.number}`, exact: true })).toHaveCount(0);
  await expect(page.getByRole('checkbox', { name: `Select ${cancelled.number}`, exact: true })).toHaveCount(0);
  await select(page, source.completed);
  const remove = page.getByRole('button', { name: 'Permanently delete records', exact: true });
  await expect(remove).toBeDisabled();
  await page.getByLabel('Type DELETE to confirm permanent deletion', { exact: true }).fill('DELETE');
  await remove.click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('Deleted 1 completed job(s)', { exact: false })).toBeVisible();
  expect((await request.get(`/api/manufacturing/records/${source.completed.id}`)).status()).toBe(404);
  expect(await record(request, waiting.id)).toEqual(waiting);
  expect(await record(request, cancelled.id)).toEqual(cancelled);
  expect(await (await request.get(`/api/manufacturing/products/${source.productId}`)).json()).toEqual(productBefore);
  expect((await (await request.get('/api/accounts')).json()).items).toEqual(salesBefore);
  const backups = await (await request.get('/api/settings/backup-recovery')).json();
  expect(backups.backups[0].counts.manufacturingRecords).toBeGreaterThanOrEqual(3);
  for (const width of [320, 390, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(salesCalls).toEqual([]); expect(errors).toEqual([]);
});

test('lost cleanup responses retry the frozen request without deleting or counting twice', async ({ page, request }) => {
  const { completed } = await seed(request);
  await open(page); await select(page, completed);
  const bodies: unknown[] = [];
  await page.route('**/api/manufacturing/settings/record-cleanup', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    bodies.push(route.request().postDataJSON());
    const response = await route.fetch();
    expect(response.ok(), await response.text()).toBe(true);
    if (bodies.length === 1) return route.abort();
    return route.fulfill({ response });
  });
  await page.getByLabel('Type DELETE to confirm permanent deletion', { exact: true }).fill('DELETE');
  await page.getByRole('button', { name: 'Permanently delete records', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Permanently delete manufacturing records?', exact: true });
  await expect(dialog.getByRole('button', { name: 'Retry same cleanup', exact: true })).toBeEnabled();
  await expect(page.getByLabel('Type DELETE to confirm permanent deletion', { exact: true })).toBeDisabled();
  await page.getByRole('tab', { name: 'General', exact: true }).dispatchEvent('click');
  await expect(page.getByRole('tab', { name: 'Manufacturing record cleanup', exact: true })).toHaveAttribute('aria-selected', 'true');
  await dialog.getByRole('button', { name: 'Retry same cleanup', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('Deleted 1 completed job(s)', { exact: false })).toBeVisible();
  expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
  expect((await request.get(`/api/manufacturing/records/${completed.id}`)).status()).toBe(404);
});

test('an unconfirmed backup or cleanup failure cannot silently delete the selected job', async ({ page, request }) => {
  const { completed } = await seed(request);
  await open(page); await select(page, completed);
  let first = true;
  await page.route('**/api/manufacturing/settings/record-cleanup', async route => {
    if (route.request().method() === 'POST' && first) {
      first = false;
      return route.fulfill({ status: 503, json: { error: { code: 'backup_verify', message: 'Safety backup could not be verified.' } } });
    }
    return route.continue();
  });
  await page.getByLabel('Type DELETE to confirm permanent deletion', { exact: true }).fill('DELETE');
  await page.getByRole('button', { name: 'Permanently delete records', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Safety backup could not be verified.');
  expect(await record(request, completed.id)).toEqual(completed);
  await dialog.getByRole('button', { name: 'Retry same cleanup', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect((await request.get(`/api/manufacturing/records/${completed.id}`)).status()).toBe(404);
});

test('a stale confirmation cannot delete a job that was reopened and completed again', async ({ page, request }) => {
  const { completed } = await seed(request);
  await open(page); await select(page, completed);
  let current = await action(request, completed, 'undo', { stepId: completed.operations[0].id, reason: 'Correct completion before cleanup' });
  current = await action(request, current, 'complete', { stepId: current.operations[0].id });
  await page.getByLabel('Type DELETE to confirm permanent deletion', { exact: true }).fill('DELETE');
  await page.getByRole('button', { name: 'Permanently delete records', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('A selected job changed or was removed. Refresh the list.', { exact: true })).toBeVisible();
  expect(await record(request, completed.id)).toEqual(current);
  await expect(page.getByRole('checkbox', { name: `Select ${completed.number}`, exact: true })).not.toBeChecked();
});
