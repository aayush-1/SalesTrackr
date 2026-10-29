import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

const path = '/api/manufacturing/settings/spreadsheet';
const identifier = () => crypto.randomUUID().replaceAll('-', '');
async function create(request: APIRequestContext, kind: string, data: object) {
  if (kind === 'products') data = { stockQuantity: 1000, minimumStockQuantity: 0, ...data };
  const meta = await (await request.get('/api/manufacturing/meta')).json();
  const id = identifier();
  const response = await request.post(`/api/manufacturing/${kind}`, {
    headers: { 'X-SalesTrackr-Request': '1', 'X-ProductTrackr-Workspace': meta.workspaceId, 'X-ProductTrackr-Generation': meta.generation },
    data: { writeId: identifier(), id, baseVersion: null, data },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return id;
}
async function open(page: Page) {
  await page.goto('/manufacturing/settings?tab=spreadsheet');
  await expect(page.getByRole('heading', { name: 'Directory spreadsheet', exact: true })).toBeVisible();
}

test('one-click export updates the same downloadable workbook and leaves directories and sales unchanged', async ({ page, request }) => {
  const productId = await create(request, 'products', { name: `Export ${identifier()}`, partNumber: 'EX-001' });
  const product = await (await request.get(`/api/manufacturing/products/${productId}`)).json();
  const sales = (await (await request.get('/api/accounts')).json()).items;
  const errors: string[] = [], salesCalls: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', route => {
    if (route.request().url().includes('/api/manufacturing/')) return route.continue();
    if (!/\/api\/(?:me|status|settings\/backup-recovery)(?:\?|$)/.test(route.request().url())) salesCalls.push(route.request().url());
    return route.continue();
  });
  await open(page);
  await expect(page.getByText('Local preview only.', { exact: false })).toBeVisible();
  const action = page.getByRole('button', { name: /^(Create|Update) spreadsheet$/ });
  await action.click();
  await expect(page.getByRole('status')).toContainText('updated and verified locally');
  const first = (await (await request.get(path)).json()).spreadsheet;
  const download = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Download spreadsheet', exact: true }).click();
  expect((await download).suggestedFilename()).toBe('ProductTrackr Directory.xlsx');
  const workbook = await request.get(`${path}/download`);
  expect(workbook.headers()['content-type']).toContain('spreadsheetml.sheet');
  expect((await workbook.body()).subarray(0, 2).toString()).toBe('PK');
  await create(request, 'operations', { name: `Standalone ${identifier()}` });
  await page.getByRole('button', { name: 'Update spreadsheet', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('updated and verified locally');
  const second = (await (await request.get(path)).json()).spreadsheet;
  expect(second.id).toBe(first.id);
  expect(second.counts.operations).toBe(first.counts.operations + 1);
  await page.reload();
  await expect(page.getByRole('link', { name: 'Download spreadsheet', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open spreadsheet', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Open spreadsheet', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Open spreadsheet', exact: true })).toBeDisabled();
  await expect(page.getByText('Open spreadsheet is available after a Google Drive export.', { exact: false })).toBeVisible();
  for (const width of [320, 390, 768, 1024]) {
    await page.setViewportSize({ width, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(await (await request.get(`/api/manufacturing/products/${productId}`)).json()).toEqual(product);
  expect((await (await request.get('/api/accounts')).json()).items).toEqual(sales);
  expect(errors).toEqual([]); expect(salesCalls).toEqual([]);
});

test('open spreadsheet opens the exported Google Drive file in a new tab, never an in-app viewer', async ({ page, context }) => {
  const driveUrl = 'https://drive.google.com/file/d/test-directory-workbook/view';
  await page.route(`**${path}`, async route => {
    const response = await route.fetch();
    const status = await response.json();
    await route.fulfill({ response, json: {
      ...status, provider: 'drive', spreadsheet: {
        id: 'test-directory-workbook', name: status.name, provider: 'drive',
        generation: status.generation, workspaceId: status.workspaceId,
        exportedAt: new Date().toISOString(), url: driveUrl,
        counts: { products: 0, steps: 0, assignments: 0, operations: 0, suppliers: 0, contacts: 0 },
      },
    } });
  });
  await context.route(driveUrl, route => route.fulfill({ contentType: 'text/html', body: '<title>Drive spreadsheet</title>' }));
  await open(page);
  const link = page.getByRole('link', { name: 'Open spreadsheet', exact: true });
  await expect(link).toHaveAttribute('href', driveUrl);
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noreferrer');
  const popup = page.waitForEvent('popup');
  await link.click();
  const drive = await popup;
  await expect(drive).toHaveURL(driveUrl);
  await expect(page).toHaveURL(/\/settings\?tab=spreadsheet$/);
  await drive.close();
});

test('a lost export response retries the same request without creating another workbook', async ({ page, request }) => {
  const attempts: unknown[] = [];
  await page.route(`**${path}`, async route => {
    if (route.request().method() !== 'POST') return route.continue();
    attempts.push(route.request().postDataJSON());
    const response = await route.fetch();
    expect(response.ok(), await response.text()).toBe(true);
    if (attempts.length === 1) return route.abort();
    return route.fulfill({ response });
  });
  await open(page);
  await page.getByRole('button', { name: /^(Create|Update) spreadsheet$/ }).click();
  await expect(page.getByRole('button', { name: 'Retry export', exact: true })).toBeVisible();
  const first = (await (await request.get(path)).json()).spreadsheet;
  await page.getByRole('button', { name: 'Retry export', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('updated and verified locally');
  expect(attempts).toHaveLength(2); expect(attempts[1]).toEqual(attempts[0]);
  expect((await (await request.get(path)).json()).spreadsheet).toEqual(first);
});

test('export errors stay visible and retry without changing the previous workbook', async ({ page }) => {
  let fail = true;
  await page.route(`**${path}`, route => {
    if (route.request().method() === 'POST' && fail) {
      fail = false;
      return route.fulfill({ status: 503, json: { error: { code: 'spreadsheet_verify', message: 'Spreadsheet readback failed. Retry export.' } } });
    }
    return route.continue();
  });
  await open(page);
  await page.getByRole('button', { name: /^(Create|Update) spreadsheet$/ }).click();
  await expect(page.getByText('Spreadsheet readback failed. Retry export.', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Download spreadsheet', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry export', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Download spreadsheet', exact: true })).toBeVisible();
});
