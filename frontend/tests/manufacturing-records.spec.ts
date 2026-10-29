import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import type { ManufacturingDetail, ManufacturingMeta, ManufacturingRecordDetail, ManufacturingRecordPreview } from '../src/manufacturing-types';

const identifier = () => crypto.randomUUID().replaceAll('-', '');
async function write(request: APIRequestContext, path: string, data: object, baseVersion: number | null = null, id = identifier(), method = 'POST') {
  const meta = await (await request.get('/api/manufacturing/meta')).json() as ManufacturingMeta;
  const response = await request.fetch(`/api/manufacturing${path}`, {
    method, headers: { 'X-SalesTrackr-Request': '1', 'X-ProductTrackr-Workspace': meta.workspaceId, 'X-ProductTrackr-Generation': meta.generation },
    data: { writeId: identifier(), id, baseVersion, data },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return id;
}
async function process(request: APIRequestContext, name: string, linked = true) {
  const companyId = await write(request, '/suppliers', { name: `${name} Company` });
  const personId = await write(request, '/suppliers', { name: `${name} Specialist`, type: 'individual' });
  const internalId = await write(request, '/suppliers', { name: `${name} Workshop`, relationship: 'internal' });
  const contactId = await write(request, '/contacts', { name: `${name} Contact`, supplierId: companyId });
  const productId = await write(request, '/products', { name: `${name} Gear`, partNumber: `${name}-042` });
  const getProduct = async () => await (await request.get(`/api/manufacturing/products/${productId}`)).json() as ManufacturingDetail;
  for (const operationName of ['Raw material', 'Laser cutting', 'Inspection']) {
    const operationId = await write(request, '/operations', { name: `${name} ${operationName}` });
    const product = await getProduct();
    const stepId = await write(request, `/products/${productId}/steps`, { operationId, instructions: `Drawing ${name}: ${operationName}` }, product.entity.version);
    if (linked) {
      for (const supplierId of [companyId, personId, internalId]) {
        const step = (await getProduct()).steps.find(s => s.id === stepId)!;
        await write(request, `/steps/${stepId}/assignments`, { supplierId, contactId: supplierId === companyId ? contactId : null }, step.version);
      }
    }
  }
  return { productId, companyId, personId, internalId, contactId, getProduct };
}
async function job(request: APIRequestContext, productId: string) {
  const preview = await (await request.get(`/api/manufacturing/records/preview/${productId}`)).json() as ManufacturingRecordPreview;
  return write(request, '/records', { productId, sourceToken: preview.sourceToken });
}
async function detail(request: APIRequestContext, id: string) {
  return await (await request.get(`/api/manufacturing/records/${id}`)).json() as ManufacturingRecordDetail;
}
async function action(page: Page, title: string, fill: () => Promise<void> = async () => {}) {
  await page.getByRole('button', { name: title, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: title, exact: true });
  await expect(dialog).toBeVisible(); await fill();
  await dialog.getByRole('button', { name: title, exact: true }).click();
  await expect(dialog).toHaveCount(0);
}
async function assign(page: Page, supplierId: string, contactId?: string) {
  await action(page, 'Assign & start', async () => {
    await page.getByLabel('Responsible supplier', { exact: true }).selectOption(supplierId);
    if (contactId) await page.getByLabel('Company contact (optional)', { exact: true }).selectOption(contactId);
  });
  await expect(page.locator('.bt-current-work .bt-record-status')).toHaveText('In progress');
}

test('supplier work sheet shows one supplier entry and opens its current manufacturing jobs', async ({ page, request }) => {
  const source = await process(request, 'SupplierSheet');
  const id = await job(request, source.productId);
  const waiting = await job(request, source.productId);
  await page.goto(`/manufacturing/records/${id}`);
  await assign(page, source.companyId, source.contactId);
  const otherProduct = await write(request, '/products', { name: 'SupplierSheet Housing', partNumber: 'SupplierSheet-043' });
  const operationId = (await source.getProduct()).steps[0].operationId;
  const stepId = await write(request, `/products/${otherProduct}/steps`, { operationId }, 1);
  await write(request, `/steps/${stepId}/assignments`, { supplierId: source.companyId, contactId: source.contactId }, 1);
  const otherId = await job(request, otherProduct);
  const other = (await detail(request, otherId)).record;
  await write(request, `/records/${otherId}/assign`, {
    stepId: other.operations[0].id, supplierId: source.companyId, contactId: source.contactId,
  }, other.version, otherId);
  await page.goto('/manufacturing');
  const sheet = page.getByRole('region', { name: 'Supplier work in progress', exact: true });
  await expect(sheet).toContainText('SupplierSheet Company');
  const supplierRow = sheet.locator('tbody tr').filter({ hasText: 'SupplierSheet Company' });
  await expect(supplierRow).toHaveCount(1);
  await expect(supplierRow.getByRole('cell', { name: '2', exact: true })).toHaveCount(2);
  await page.getByRole('navigation', { name: 'Manufacturing navigation' }).getByRole('link', { name: 'Supplier work', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Supplier work', exact: true })).toBeVisible();
  await page.getByLabel('Search supplier work').fill('SupplierSheet');
  await expect(sheet.locator('tbody tr')).toHaveCount(1);
  await sheet.getByRole('link', { name: 'SupplierSheet Company', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`supplier=${source.companyId}`));
  const works = page.getByRole('region', { name: 'Supplier manufacturing work', exact: true });
  await expect(works.locator('tbody tr')).toHaveCount(2);
  await expect(works).toContainText('SupplierSheet Contact');
  await expect(works).toContainText('SupplierSheet Raw material');
  await expect(works).toContainText('SupplierSheet-042');
  await expect(works).toContainText('SupplierSheet-043');
  await expect(works.getByRole('link', { name: (await detail(request, waiting)).record.number, exact: true })).toHaveCount(0);
  for (const width of [320, 390, 768, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  const current = (await detail(request, id)).record;
  await write(request, `/records/${id}/complete`, { stepId: current.operations[0].id }, current.version, id);
  const currentOther = (await detail(request, otherId)).record;
  await write(request, `/records/${otherId}/complete`, { stepId: currentOther.operations[0].id }, currentOther.version, otherId);
  await page.getByRole('button', { name: 'Refresh supplier work', exact: true }).click();
  await expect(page.getByText('No current manufacturing work', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'All suppliers in progress', exact: true }).click();
  await page.getByLabel('Search supplier work').fill('SupplierSheet');
  await expect(page.getByText('No supplier work matches', { exact: true })).toBeVisible();
});

test('independent jobs run sequentially from product creation through completed work', async ({ page, request }) => {
  const errors: string[] = [], sales: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', route => {
    if (route.request().url().includes('/api/manufacturing/')) return route.continue();
    sales.push(route.request().url()); return route.abort();
  });
  const source = await process(request, 'Sequential');
  await page.goto(`/manufacturing/products/${source.productId}`);
  await page.getByRole('link', { name: 'New manufacturing record', exact: true }).first().click();
  await expect(page.getByLabel('Product process', { exact: true })).toHaveValue(source.productId);
  await expect(page.locator('.bt-route-preview li')).toHaveCount(3);
  await page.getByLabel('Job notes', { exact: true }).fill('Synthetic browser job');
  await page.getByRole('button', { name: 'Create manufacturing record', exact: true }).click();
  await expect(page).toHaveURL(/\/manufacturing\/records\/[a-z0-9]{32}$/);
  const firstId = new URL(page.url()).pathname.split('/').at(-1)!;
  const secondId = await job(request, source.productId);
  await expect(page.locator('.bt-current-work')).toContainText('Awaiting assignment');
  await assign(page, source.companyId, source.contactId);
  await expect(page.locator('.bt-current-work')).toContainText('Sequential Contact');
  await action(page, 'Complete operation');
  await expect(page.locator('.bt-current-work')).toContainText('Sequential Laser cutting');
  await expect(page.locator('.bt-current-work .bt-record-status')).toHaveText('Awaiting assignment');
  await expect(page.locator('.bt-current-work')).toContainText('No supplier has started');
  await assign(page, source.personId);
  for (const width of [320, 390, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.screenshot({ path: test.info().outputPath('manufacturing-job-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: test.info().outputPath('manufacturing-job-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await action(page, 'Complete operation');
  await expect(page.locator('.bt-current-work')).toContainText('Sequential Inspection');
  await assign(page, source.internalId);
  await expect(page.locator('.bt-current-work')).toContainText('In-house company');
  await action(page, 'Complete operation');
  await expect(page.locator('.bt-current-work')).toContainText('All operations complete');
  await expect(page.locator('.bt-work-step.completed')).toHaveCount(3);
  expect((await detail(request, secondId)).record.status).toBe('ready');
  expect((await detail(request, firstId)).record.completedCount).toBe(3);
  await page.goto(`/manufacturing/records?productId=${source.productId}`);
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await page.getByRole('button', { name: 'Completed', exact: true }).click();
  await expect(page.locator('tbody tr')).toHaveCount(1);
  const first = (await detail(request, firstId)).record;
  await page.getByLabel('Search manufacturing records').fill(first.number.toLowerCase());
  await expect(page.getByRole('link', { name: first.number, exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'All records', exact: true }).click();
  await page.getByLabel('Search manufacturing records').fill('sEquEntial-042');
  await expect(page.locator('tbody tr')).toHaveCount(2);
  expect(errors).toEqual([]); expect(sales).toEqual([]);
});

test('reassignment, safe correction and reasoned cancellation retain history', async ({ page, request }) => {
  const source = await process(request, 'Corrections');
  const id = await job(request, source.productId);
  await page.goto(`/manufacturing/records/${id}`);
  await assign(page, source.companyId);
  await action(page, 'Reassign supplier', async () => {
    await page.getByLabel('Responsible supplier', { exact: true }).selectOption(source.personId);
    await page.getByLabel('Reason', { exact: true }).fill('Move to specialist');
  });
  await expect(page.locator('.bt-current-work')).toContainText('Corrections Specialist');
  await action(page, 'Complete operation');
  await expect(page.locator('.bt-current-work .bt-record-status')).toHaveText('Awaiting assignment');
  await action(page, 'Undo last completion', async () => { await page.getByLabel('Reason', { exact: true }).fill('Incorrect completion entry'); });
  await expect(page.locator('.bt-current-work .bt-record-status')).toHaveText('In progress');
  await expect(page.locator('.bt-current-work')).toContainText('Corrections Specialist');
  await expect(page.locator('.bt-work-step.completed')).toHaveCount(0);
  await expect(page.locator('.bt-record-history')).toContainText('Incorrect completion entry');
  await action(page, 'Complete operation');
  await expect(page.locator('.bt-current-work .bt-record-status')).toHaveText('Awaiting assignment');
  await assign(page, source.companyId);
  await expect(page.getByRole('button', { name: 'Undo last completion', exact: true })).toBeDisabled();
  await action(page, 'Cancel record', async () => { await page.getByLabel('Reason', { exact: true }).fill('Sample job withdrawn'); });
  await expect(page.locator('.bt-current-work .bt-record-status')).toHaveText('Cancelled');
  await expect(page.getByRole('button', { name: 'Complete operation', exact: true })).toHaveCount(0);
  await expect(page.locator('.bt-record-history')).toContainText('Sample job withdrawn');
  await expect(page.locator('.bt-record-history')).toContainText('Move to specialist');
  await page.reload();
  await expect(page.locator('.bt-record-status.cancelled').first()).toBeVisible();
});

test('a ready job gains live suppliers and rejects a removed link from an open dropdown', async ({ page, request }) => {
  const source = await process(request, 'LiveLinks', false);
  const id = await job(request, source.productId);
  await page.goto(`/manufacturing/records/${id}`);
  await expect(page.getByRole('button', { name: 'Assign & start', exact: true })).toBeDisabled();
  await expect(page.locator('.bt-current-work')).toContainText('No suppliers are linked');
  let sourceStep = (await source.getProduct()).steps[0];
  const linkId = await write(request, `/steps/${sourceStep.id}/assignments`, { supplierId: source.companyId }, sourceStep.version);
  await page.getByRole('button', { name: 'Refresh manufacturing record', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Assign & start', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Assign & start', exact: true }).click();
  await page.getByLabel('Responsible supplier', { exact: true }).selectOption(source.companyId);
  sourceStep = (await source.getProduct()).steps[0];
  await write(request, `/steps/${sourceStep.id}/assignments/${linkId}/remove`, {}, sourceStep.version, linkId);
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Assign & start', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('not currently linked');
  page.once('dialog', d => d.accept());
  await dialog.getByRole('button', { name: 'Close dialog', exact: true }).click();
  sourceStep = (await source.getProduct()).steps[0];
  await write(request, `/steps/${sourceStep.id}/assignments`, { supplierId: source.personId }, sourceStep.version);
  await page.getByRole('button', { name: 'Refresh manufacturing record', exact: true }).click();
  await assign(page, source.personId);
  expect((await detail(request, id)).record.currentSupplierId).toBe(source.personId);
});

test('lost create, completion and cancellation responses retry exact writes even after refresh', async ({ page, request }) => {
  const source = await process(request, 'RetryJobs');
  const drop = async (pattern: string) => {
    let dropped = false;
    await page.route(pattern, async route => {
      if (route.request().method() === 'POST' && !dropped) {
        dropped = true; expect((await route.fetch()).ok()).toBe(true); await route.abort();
      } else await route.continue();
    });
  };
  await drop('**/api/manufacturing/records');
  await page.goto(`/manufacturing/records/new?productId=${source.productId}`);
  await expect(page.locator('.bt-route-preview li')).toHaveCount(3);
  await page.getByLabel('Job notes', { exact: true }).fill('Retry job notes');
  await page.getByRole('button', { name: 'Create manufacturing record', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('retry the same change');
  await expect(page.getByLabel('Job notes', { exact: true })).toBeDisabled();
  await page.locator('footer').getByRole('button', { name: 'Refresh manufacturing data', exact: true }).click();
  await page.getByRole('button', { name: 'Retry same save', exact: true }).click();
  await expect(page).toHaveURL(/\/manufacturing\/records\/[a-z0-9]{32}$/);
  const id = new URL(page.url()).pathname.split('/').at(-1)!;
  expect((await (await request.get(`/api/manufacturing/records?view=all&productId=${source.productId}`)).json()).total).toBe(1);
  await assign(page, source.companyId);
  await drop(`**/api/manufacturing/records/${id}/complete`);
  await page.getByRole('button', { name: 'Complete operation', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Complete operation', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('retry the same change');
  await expect(dialog.getByLabel('Completed on', { exact: true })).toBeDisabled();
  // Simulate a background refresh without dismissing the uncertain action.
  await page.locator('footer').getByRole('button', { name: 'Refresh manufacturing data', exact: true }).dispatchEvent('click');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Retry same save', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('.bt-current-work .bt-record-status')).toHaveText('Awaiting assignment');
  expect((await detail(request, id)).record.completedCount).toBe(1);
  expect((await (await request.get(`/api/manufacturing/records/${id}/history`)).json()).total).toBe(3);
  await drop(`**/api/manufacturing/records/${id}/cancel`);
  await page.getByRole('button', { name: 'Cancel record', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByLabel('Reason', { exact: true }).fill('Retry cancellation');
  await dialog.getByRole('button', { name: 'Cancel record', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('retry the same change');
  await expect(dialog.getByLabel('Reason', { exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Retry same save', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('.bt-current-work .bt-record-status')).toHaveText('Cancelled');
  expect((await (await request.get(`/api/manufacturing/records/${id}/history`)).json()).total).toBe(4);
});

test('changed creation previews are rejected without losing notes and can be reviewed again', async ({ page, request }) => {
  const source = await process(request, 'PreviewJobs');
  await page.goto(`/manufacturing/records/new?productId=${source.productId}`);
  await expect(page.locator('.bt-route-preview li')).toHaveCount(3);
  await page.getByLabel('Job notes', { exact: true }).fill('Keep this job draft');
  const firstStep = (await source.getProduct()).steps[0];
  await write(request, `/steps/${firstStep.id}`, { operationId: firstStep.operationId, instructions: 'Revised drawing for this preview' }, firstStep.version, firstStep.id, 'PUT');
  await page.getByRole('button', { name: 'Create manufacturing record', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Product process changed');
  await expect(page.getByLabel('Job notes', { exact: true })).toHaveValue('Keep this job draft');
  await page.getByRole('button', { name: 'Reload process preview', exact: true }).click();
  await expect(page.locator('.bt-route-preview')).toContainText('Revised drawing for this preview');
  await page.getByRole('button', { name: 'Create manufacturing record', exact: true }).click();
  await expect(page).toHaveURL(/\/manufacturing\/records\/[a-z0-9]{32}$/);
  await expect(page.locator('.bt-record-notes')).toContainText('Keep this job draft');
  await expect(page.locator('.bt-execution-timeline')).toContainText('Revised drawing for this preview');
});
