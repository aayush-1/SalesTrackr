import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

const id = () => crypto.randomUUID().replaceAll('-', '');
async function manufacturing(request: APIRequestContext, path: string, data: object, baseVersion: number | null = null, entityId = id()) {
  const meta = await (await request.get('/api/manufacturing/meta')).json();
  const response = await request.post(`/api/manufacturing${path}`, {
    headers: { 'X-SalesTrackr-Request': '1', 'X-ProductTrackr-Workspace': meta.workspaceId, 'X-ProductTrackr-Generation': meta.generation },
    data: { writeId: id(), id: entityId, baseVersion, data },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return entityId;
}
async function sales(request: APIRequestContext, kind: string, data: object) {
  const meta = await (await request.get('/api/me')).json();
  const response = await request.post(`/api/${kind}s`, {
    headers: { 'X-SalesTrackr-Request': '1', 'X-SalesTrackr-Generation': meta.generation },
    data: { writeId: id(), entityId: id(), baseRevision: null, data },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).revision;
}
async function advance(page: Page, stage: string) {
  await page.getByRole('button', { name: stage, exact: true }).click();
  await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.stages .current strong')).toHaveText(stage);
}

test('real quantities, combined shortages, Dispatch retry/reversal, production and manual stock', async ({ page, request }) => {
  test.setTimeout(240_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const name = `Inventory UI bearing ${Date.now()}`;
  await page.goto('/manufacturing/products');
  await page.getByRole('button', { name: 'New product', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Product name', { exact: true }).fill(name);
  await dialog.getByLabel('Part number', { exact: true }).fill('UI-INV');
  await expect(dialog.getByLabel('Stock quantity *', { exact: true })).toHaveValue('');
  await expect(dialog.getByLabel('Minimum stock quantity *', { exact: true })).toHaveValue('');
  await dialog.getByRole('button', { name: 'Create product', exact: true }).click();
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('Stock quantity *', { exact: true }).fill('10');
  await dialog.getByLabel('Minimum stock quantity *', { exact: true }).fill('5');
  await dialog.getByRole('button', { name: 'Create product', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const productId = page.url().split('/').at(-1)!;
  const product = async () => (await (await request.get(`/api/manufacturing/products/${productId}`)).json()).entity;
  const operationId = await manufacturing(request, '/operations', { name: `${name} inspection` });
  const supplierId = await manufacturing(request, '/suppliers', { name: `${name} workshop`, relationship: 'internal' });
  const stepId = await manufacturing(request, `/products/${productId}/steps`, { operationId }, (await product()).version);
  await manufacturing(request, `/steps/${stepId}/assignments`, { supplierId }, 1);
  const account = await sales(request, 'account', { name: `${name} customer` });
  await page.goto(`/records/new?accountId=${account.entityId}`);
  await page.getByRole('button', { name: 'Add part', exact: true }).click();
  await page.getByLabel('Product 1 *', { exact: true }).selectOption(productId);
  await expect(page.getByLabel('Quantity 1 *', { exact: true })).toHaveValue('');
  await page.getByLabel('Quantity 1 *', { exact: true }).fill('4');
  await page.getByRole('button', { name: 'Create sales record', exact: true }).click();
  await expect(page.getByRole('heading', { name: account.data.name, exact: true })).toBeVisible();
  const firstId = page.url().split('/').at(-1)!;
  const remaining = [];
  for (let index = 0; index < 2; index++) remaining.push(await sales(request, 'record', {
    accountId: account.entityId, parts: [{ id: id(), productId, quantity: 4 }],
  }));
  await page.goto('/order-shortages');
  const shortage = page.getByRole('table', { name: 'Order in hand shortage', exact: true }).getByRole('row').filter({ hasText: name });
  await expect(shortage).toContainText('12');
  await expect(shortage.getByRole('cell', { name: '10', exact: true })).toBeVisible();
  await expect(shortage.getByRole('cell', { name: '2', exact: true })).toBeVisible();
  await shortage.getByRole('button', { name: '3 sales records' }).click();
  await expect(page.getByRole('dialog').getByRole('link')).toHaveCount(3);
  await expect(page.getByRole('dialog').getByRole('link', { name: account.data.name, exact: true })).toHaveCount(3);
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
  await page.goto('/manufacturing/order-shortages');
  const nav = page.getByRole('navigation', { name: 'Manufacturing navigation', exact: true });
  const titles = await nav.getByRole('link').allTextContents();
  expect(titles.indexOf('Order in hand shortage')).toBeLessThan(titles.indexOf('Low stock'));
  await expect(shortage).toContainText('12');
  await shortage.getByRole('button', { name: '3 sales records' }).click();
  await expect(page.getByRole('dialog').getByRole('link', { name: account.data.name, exact: true })).toHaveCount(3);
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
  await page.locator('footer').getByRole('button', { name: 'Refresh manufacturing data', exact: true }).click();
  await expect(shortage.getByRole('cell', { name: '10', exact: true })).toBeVisible();
  await page.goto(`/records/${firstId}`);
  await expect(page.getByRole('region', { name: 'Stock update', exact: true })).toHaveCount(0);
  await advance(page, 'Follow-up'); await advance(page, 'Order in hand');
  let lose = true;
  const attempts: unknown[] = [];
  await page.route(`**/api/records/${firstId}/stage`, async route => {
    attempts.push(route.request().postDataJSON());
    const response = await route.fetch();
    expect(response.ok(), await response.text()).toBe(true);
    if (lose) { lose = false; return route.abort(); }
    return route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Dispatched', exact: true }).click();
  await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toBeVisible();
  await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(attempts).toHaveLength(2); expect(attempts[0]).toEqual(attempts[1]);
  expect((await product()).stockQuantity).toBe(6);
  await page.unroute(`**/api/records/${firstId}/stage`);
  for (const [index, row] of remaining.entries()) {
    await page.goto(`/records/${row.entityId}`);
    await advance(page, 'Follow-up'); await advance(page, 'Order in hand');
    if (index === 0) await advance(page, 'Dispatched');
  }
  await page.getByRole('button', { name: 'Dispatched', exact: true }).click();
  await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('Insufficient stock');
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
  await expect(page.locator('.stages .current strong')).toHaveText('Order in hand');
  expect((await product()).stockQuantity).toBe(2);
  await page.goto('/order-shortages');
  await expect(shortage.getByRole('cell', { name: '4', exact: true })).toBeVisible();
  await shortage.getByRole('link', { name: 'Create manufacturing record', exact: true }).click();
  await expect(page.getByLabel('Quantity *', { exact: true })).toHaveValue('2');
  await page.getByRole('button', { name: 'Create manufacturing record', exact: true }).click();
  await expect(page).toHaveURL(/\/manufacturing\/records\/[a-z0-9]{32}$/);
  const jobId = page.url().split('/').at(-1)!;
  await page.getByRole('button', { name: 'Assign & start', exact: true }).click();
  await page.getByLabel('Responsible supplier', { exact: true }).selectOption(supplierId);
  await page.getByRole('dialog').getByRole('button', { name: 'Assign & start', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const complete = async () => {
    await page.getByRole('button', { name: 'Complete operation', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Complete operation', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  };
  await complete();
  expect((await product()).stockQuantity).toBe(4);
  await page.goto('/order-shortages');
  await expect(page.getByText('No order shortages', { exact: true })).toBeVisible();
  await page.goto('/manufacturing/low-stock');
  const low = page.getByRole('table', { name: 'Low stock', exact: true }).getByRole('row').filter({ hasText: name });
  await expect(low.getByRole('cell', { name: '1', exact: true })).toBeVisible();
  await page.goto(`/manufacturing/records/${jobId}`);
  await page.getByRole('button', { name: 'Correct quantity', exact: true }).click();
  await page.getByLabel('Quantity *', { exact: true }).fill('3');
  await page.getByLabel('Reason', { exact: true }).fill('One additional finished bearing');
  await page.getByRole('dialog').getByRole('button', { name: 'Correct quantity', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await product()).stockQuantity).toBe(5);
  await page.goto(`/records/${remaining[1].entityId}`);
  await advance(page, 'Dispatched');
  expect((await product()).stockQuantity).toBe(1);
  await page.goto(`/manufacturing/records/${jobId}`);
  await page.getByRole('button', { name: 'Undo last completion', exact: true }).click();
  await page.getByLabel('Reason', { exact: true }).fill('Attempt to correct consumed output');
  await page.getByRole('dialog').getByRole('button', { name: 'Undo last completion', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert').filter({ hasText: 'Insufficient stock' })).toBeVisible();
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
  await page.goto(`/manufacturing/products/${productId}`);
  await page.getByRole('button', { name: 'Edit stock', exact: true }).click();
  await page.getByLabel('Stock quantity *', { exact: true }).fill('10');
  await page.getByLabel('Reason *', { exact: true }).fill('Physical stock count');
  await page.getByRole('button', { name: 'Save stock', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('table', { name: 'Stock history', exact: true })).toContainText('Physical stock count');
  expect((await product()).stockQuantity).toBe(10);
  await page.goto(`/records/${firstId}`);
  await advance(page, 'Delivered');
  await page.getByRole('button', { name: 'Received', exact: true }).click();
  await expect(page.locator('.stages .current strong')).toHaveText('Closed');
  const correct = async (target: string) => {
    await page.getByRole('button', { name: 'Correct a stage or date', exact: true }).click();
    await page.getByLabel('Stage to correct / return to', { exact: true }).selectOption(target);
    await page.getByLabel('Correction reason *', { exact: true }).fill('Correct premature stage');
    await page.getByRole('button', { name: 'Save changes', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  };
  await correct('dispatched'); expect((await product()).stockQuantity).toBe(10);
  await correct('order_received'); expect((await product()).stockQuantity).toBe(14);
  await advance(page, 'Dispatched'); expect((await product()).stockQuantity).toBe(10);
  for (const path of ['/order-shortages', '/manufacturing/low-stock', `/manufacturing/products/${productId}`]) {
    await page.goto(path);
    await page.setViewportSize({ width: 390, height: 844 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(errors).toEqual([]);
});
