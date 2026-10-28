import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import type { ManufacturingDetail } from '../src/manufacturing-types';

async function create(page: Page, kind: string, name: string, extra: () => Promise<void> = async () => {}) {
  const singular = kind === 'partners' || kind === 'companies' ? 'supplier' : kind.slice(0, -1);
  await page.goto(`/manufacturing/${kind}`);
  await page.getByRole('button', { name: `New ${singular}`, exact: true }).click();
  const form = page.getByRole('dialog');
  await form.getByLabel(`${singular === 'contact' ? 'Contact' : singular[0].toUpperCase() + singular.slice(1)} name`, { exact: true }).fill(name);
  await extra();
  await form.getByRole('button', { name: `Create ${singular}`, exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  return page.url();
}
async function addStep(page: Page, name: string) {
  await page.getByRole('button', { name: 'Add operation', exact: true }).click();
  await page.getByRole('dialog').getByLabel('Operation', { exact: true }).selectOption({ label: name });
  await page.getByRole('dialog').getByRole('button', { name: 'Add operation', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
}
async function assign(page: Page, operation: string, partner: string, contact?: string) {
  const step = page.locator('.bt-step').filter({ has: page.getByRole('link', { name: operation, exact: true }) });
  await step.getByRole('button', { name: 'Assign supplier', exact: true }).click();
  await page.getByLabel('Manufacturing supplier', { exact: true }).selectOption({ label: partner });
  if (contact) await page.getByLabel('Company contact (optional)', { exact: true }).selectOption({ label: contact });
  await page.getByRole('dialog').getByRole('button', { name: 'Assign supplier', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
}
async function snapshot(request: APIRequestContext, url: string) {
  const id = new URL(url).pathname.split('/').at(-1);
  return await (await request.get(`/api/manufacturing/products/${id}`)).json() as ManufacturingDetail;
}

test('a product shows its part number and a retryable one-click Drive folder link', async ({ page, context }) => {
  const driveUrl = 'https://drive.google.com/drive/folders/test-product-folder';
  let unavailable = true;
  await page.route('**/api/manufacturing/products/*/drive-folder', async route => {
    if (unavailable) return route.fulfill({ status: 503, json: { error: { message: 'Reconnect the product folder.' } } });
    await route.fulfill({ json: {
      workspaceId: route.request().headers()['x-producttrackr-workspace'],
      generation: route.request().headers()['x-producttrackr-generation'],
      configured: true, url: driveUrl,
    } });
  });
  await context.route(driveUrl, route => route.fulfill({ contentType: 'text/html', body: '<title>Product files</title>' }));
  await create(page, 'products', 'Drawing component', async () => {
    await page.getByLabel('Part number', { exact: true }).fill('DRAW-042');
  });
  await expect(page.getByRole('heading', { name: 'Drawing component', exact: true })).toBeVisible();
  await expect(page.getByText('Reconnect the product folder.', { exact: true })).toBeVisible();
  unavailable = false;
  await page.getByRole('button', { name: 'Retry folder connection', exact: true }).click();
  const link = page.getByRole('link', { name: 'Open Drive folder', exact: true });
  await expect(link).toHaveAttribute('href', driveUrl);
  const popup = page.waitForEvent('popup');
  await link.click();
  const drive = await popup;
  await expect(drive).toHaveURL(driveUrl);
  await drive.close();
  await page.reload();
  await expect(link).toHaveAttribute('href', driveUrl);
  await expect(page.getByRole('region', { name: 'Product files' })).toContainText('not drawings or uploaded files');
});

test('connected directories, product-specific operations, primary replacement and persistence', async ({ page, request }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  const operations = ['Raw material procurement', 'Laser cutting', 'Logo printing', 'Galvanization', 'Inspection'];
  for (const name of operations) await create(page, 'operations', name);
  const companyA = await create(page, 'partners', 'Company A');
  await create(page, 'partners', 'Company B');
  await create(page, 'partners', 'Independent specialist', async () => {
    await page.getByLabel('Supplier type', { exact: true }).selectOption('individual');
    await page.getByLabel('Phone', { exact: true }).fill('100200300');
  });
  await create(page, 'partners', 'JSD', async () => { await page.getByLabel('Relationship', { exact: true }).selectOption('internal'); });
  const contact = await create(page, 'contacts', 'Alex at A', async () => {
    await page.getByLabel('Company supplier', { exact: true }).selectOption({ label: 'Company A' });
  });
  const gear = await create(page, 'products', 'Gear');
  for (const name of operations) await addStep(page, name);
  await assign(page, 'Laser cutting', 'Company A', 'Alex at A');
  await assign(page, 'Laser cutting', 'Company B');
  await assign(page, 'Inspection', 'JSD (in-house)');
  await assign(page, 'Logo printing', 'Independent specialist (individual)');
  const laser = page.locator('.bt-step').filter({ has: page.getByRole('link', { name: 'Laser cutting', exact: true }) });
  await expect(laser.locator('tr').filter({ hasText: 'Company A' })).toContainText('Primary');
  await laser.locator('tr').filter({ hasText: 'Company B' }).getByRole('button', { name: 'Make primary' }).click();
  await expect(laser.locator('tr').filter({ hasText: 'Company B' })).toContainText('Primary');
  await laser.getByRole('button', { name: 'Remove Company B assignment' }).click();
  await expect(page.getByRole('button', { name: 'Remove assignment', exact: true })).toBeDisabled();
  await page.getByLabel('Replacement primary supplier').selectOption({ label: 'Company A' });
  await page.getByRole('button', { name: 'Remove assignment', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(laser.locator('tr').filter({ hasText: 'Company A' })).toContainText('Primary');
  const before = await snapshot(request, gear);
  await page.getByRole('button', { name: 'Move Laser cutting up', exact: true }).click();
  await expect(page.locator('.bt-step').first()).toContainText('Laser cutting');
  await page.reload();
  const after = await snapshot(request, gear);
  expect(after.steps.map(s => s.id).sort()).toEqual(before.steps.map(s => s.id).sort());
  expect(after.assignments).toEqual(before.assignments);
  await expect(page.locator('.bt-step')).toHaveCount(5);
  await page.screenshot({ path: test.info().outputPath('gear-desktop.png'), fullPage: true });
  for (const width of [320, 390, 768, 1024]) {
    await page.setViewportSize({ width, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: test.info().outputPath('gear-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const bracket = await create(page, 'products', 'Bracket');
  await addStep(page, 'Laser cutting');
  await assign(page, 'Laser cutting', 'Company B');
  expect((await snapshot(request, bracket)).partners.map(p => p.name)).toEqual(['Company B']);
  expect((await snapshot(request, gear)).assignments).toEqual(before.assignments);
  await page.goto(companyA);
  await expect(page.getByRole('heading', { name: 'Company contacts', exact: true })).toBeVisible();
  await expect(page.locator('section').filter({ has: page.getByRole('heading', { name: 'Company contacts', exact: true }) }).getByRole('link', { name: 'Alex at A', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Gear', exact: true })).toBeVisible();
  const references = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Connected product operations', exact: true }) });
  await expect(references.getByRole('columnheader')).toHaveText(['Product', 'Operation', 'Contact', 'Assignment']);
  await expect(references.locator('tbody tr').first().getByRole('cell').nth(0)).toHaveText('Gear');
  await expect(references.locator('tbody tr').first().getByRole('cell').nth(1).getByRole('link', { name: 'Laser cutting', exact: true })).toBeVisible();
  await page.goto(contact);
  await expect(page.getByRole('link', { name: 'Gear', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader')).toHaveText(['Product / operation', 'Supplier', 'Contact', 'Assignment']);
  await page.goto('/manufacturing/contacts');
  await page.getByLabel('Filter contacts by company').selectOption({ label: 'Company B' });
  await expect(page.getByText('No contacts match', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('a lost save response retries the same write without duplicating a product', async ({ page, request }) => {
  let dropped = false;
  await page.route('**/api/manufacturing/products', async route => {
    if (route.request().method() === 'POST' && !dropped) {
      dropped = true; const committed = await route.fetch(); expect(committed.ok()).toBe(true); await route.abort();
    } else await route.continue();
  });
  await page.goto('/manufacturing/products');
  await page.getByRole('button', { name: 'New product', exact: true }).click();
  await page.getByLabel('Product name', { exact: true }).fill('Retry-safe component');
  await page.getByRole('button', { name: 'Create product', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('retry the same change');
  await expect(page.getByLabel('Product name', { exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Retry same save', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Retry-safe component', exact: true })).toBeVisible();
  const list = await (await request.get('/api/manufacturing/products?search=retry-safe')).json();
  expect(list.total).toBe(1);
});

test('manufacturing edits retain unsaved-change and stale-version protection', async ({ page, request }) => {
  const product = await create(page, 'products', 'Concurrent component');
  await page.getByRole('button', { name: 'Add operation', exact: true }).click();
  await page.getByLabel('Product-specific instructions').fill('Keep this draft');
  page.once('dialog', d => d.dismiss());
  await page.getByRole('dialog').getByRole('link', { name: 'Operations directory' }).click();
  await expect(page).toHaveURL(product);
  await expect(page.getByLabel('Product-specific instructions')).toHaveValue('Keep this draft');
  page.once('dialog', d => d.accept());
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
  await page.getByRole('button', { name: 'Edit product', exact: true }).click();
  await page.getByLabel('Product name', { exact: true }).fill('Unsaved revision');
  page.once('dialog', d => d.dismiss());
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  const current = await snapshot(request, product);
  const response = await request.put(`/api/manufacturing/products/${current.entity.id}`, {
    headers: { 'X-SalesTrackr-Request': '1', 'X-ProductTrackr-Workspace': current.workspaceId, 'X-ProductTrackr-Generation': current.generation },
    data: { id: current.entity.id, writeId: crypto.randomUUID().replaceAll('-', ''), baseVersion: current.entity.version, data: { name: 'Newer revision' } },
  });
  expect(response.ok()).toBe(true);
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('changed');
  await expect(page.getByLabel('Product name', { exact: true })).toHaveValue('Unsaved revision');
  expect((await snapshot(request, product)).entity.name).toBe('Newer revision');
});

test('create an operation inside a product, preserve the draft and reuse it on another product', async ({ page, request }) => {
  const product = await create(page, 'products', 'Inline operation component');
  await page.getByRole('button', { name: 'Add operation', exact: true }).click();
  await page.getByLabel('Product-specific instructions').fill('Keep this product-specific drawing reference.');
  await page.getByLabel('Operation', { exact: true }).selectOption('__create__');
  const creation = page.getByRole('dialog', { name: 'New operation', exact: true });
  await expect(creation).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await creation.getByLabel('Operation name', { exact: true }).fill('Precision deburring');
  await creation.getByLabel('Description', { exact: true }).fill('Remove sharp edges before finishing.');
  await creation.getByRole('button', { name: 'Create operation', exact: true }).click();
  const step = page.getByRole('dialog', { name: 'Add product operation', exact: true });
  await expect(step).toBeVisible();
  await expect(page).toHaveURL(product);
  const selected = await step.getByLabel('Operation', { exact: true }).inputValue();
  await expect(step.getByRole('option', { name: 'Precision deburring', exact: true })).toHaveJSProperty('selected', true);
  await expect(step.getByLabel('Product-specific instructions')).toHaveValue('Keep this product-specific drawing reference.');
  expect((await snapshot(request, product)).steps).toHaveLength(0);
  page.once('dialog', dialog => dialog.dismiss());
  await step.getByRole('button', { name: 'Close dialog', exact: true }).click();
  await expect(step).toBeVisible();
  await step.getByRole('button', { name: 'Add operation', exact: true }).click();
  await expect(page.locator('.bt-step')).toContainText('Precision deburring');
  await expect(page.locator('.bt-step')).toContainText('Keep this product-specific drawing reference.');
  await page.reload();
  expect((await snapshot(request, product)).steps[0].operationId).toBe(selected);
  const operation = await (await request.get(`/api/manufacturing/operations/${selected}`)).json();
  expect(operation.entity.description).toBe('Remove sharp edges before finishing.');
  const secondProduct = await create(page, 'products', 'Another inline component');
  await addStep(page, 'Precision deburring');
  expect((await snapshot(request, secondProduct)).steps[0].operationId).toBe(selected);
  expect((await snapshot(request, secondProduct)).steps[0].instructions).toBe('');
});

test('cancelling inline operation creation keeps the product draft and previous selection', async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await create(page, 'operations', 'Existing inline activity');
  const product = await create(page, 'products', 'Cancel inline creation component');
  await page.getByRole('button', { name: 'Add operation', exact: true }).click();
  await page.getByLabel('Operation', { exact: true }).selectOption({ label: 'Existing inline activity' });
  const selected = await page.getByLabel('Operation', { exact: true }).inputValue();
  await page.getByLabel('Product-specific instructions').fill('Do not lose these instructions.');
  await page.getByLabel('Operation', { exact: true }).selectOption('__create__');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByLabel('Operation name', { exact: true }).fill('Cancelled inline operation');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByLabel('Operation name', { exact: true })).toHaveValue('Cancelled inline operation');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Add product operation', exact: true })).toBeVisible();
  await expect(page.getByLabel('Operation', { exact: true })).toHaveValue(selected);
  await expect(page.getByLabel('Product-specific instructions')).toHaveValue('Do not lose these instructions.');
  expect((await snapshot(request, product)).steps).toHaveLength(0);
  const cancelled = await (await request.get('/api/manufacturing/operations?search=cancelled%20inline')).json();
  expect(cancelled.total).toBe(0);
  await page.getByRole('dialog').getByRole('button', { name: 'Add operation', exact: true }).click();
  await expect(page.locator('.bt-step')).toContainText('Existing inline activity');
});

test('an unconfirmed inline operation retries without losing the product draft or duplicating the operation', async ({ page, request }) => {
  await create(page, 'products', 'Retry inline operation component');
  let dropped = false;
  await page.route('**/api/manufacturing/operations', async route => {
    if (route.request().method() === 'POST' && !dropped) {
      dropped = true;
      const committed = await route.fetch(); expect(committed.ok()).toBe(true);
      await route.abort();
    } else await route.continue();
  });
  await page.getByRole('button', { name: 'Add operation', exact: true }).click();
  await page.getByLabel('Product-specific instructions').fill('Preserve through an interrupted save.');
  await page.getByLabel('Operation', { exact: true }).selectOption('__create__');
  await page.getByLabel('Operation name', { exact: true }).fill('Retry-safe inline finishing');
  await page.getByRole('button', { name: 'Create operation', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('retry the same change');
  await expect(page.getByLabel('Operation name', { exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Retry same save', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Add product operation', exact: true })).toBeVisible();
  await expect(page.getByLabel('Product-specific instructions')).toHaveValue('Preserve through an interrupted save.');
  await expect(page.getByRole('option', { name: 'Retry-safe inline finishing', exact: true })).toHaveJSProperty('selected', true);
  await page.getByRole('dialog').getByRole('button', { name: 'Add operation', exact: true }).click();
  await expect(page.locator('.bt-step')).toContainText('Retry-safe inline finishing');
  const operations = await (await request.get('/api/manufacturing/operations?search=retry-safe%20inline')).json();
  expect(operations.total).toBe(1);
});

test('products show editable Part numbers and search by either name or ID', async ({ page, request }) => {
  const product = await create(page, 'products', 'Searchable Precision Gear', async () => {
    await page.getByLabel('Part number', { exact: true }).fill('PID-572');
  });
  await expect(page.locator('.bt-master-details')).toContainText('Part number');
  await expect(page.locator('.bt-master-details')).toContainText('PID-572');
  await page.goto('/manufacturing/products');
  await expect(page.getByRole('columnheader', { name: 'Part number', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'Reference', exact: true })).toHaveCount(0);
  const search = page.getByRole('textbox', { name: 'Search products', exact: true });
  await expect(search).toHaveAttribute('placeholder', 'Search by name or part number...');
  for (const term of ['precision', 'pId-572', '572']) {
    await search.fill(term);
    await expect(page.locator('tbody tr')).toHaveCount(1);
    await expect(page.locator('tbody tr')).toContainText('Searchable Precision Gear');
    await expect(page.locator('tbody tr')).toContainText('PID-572');
  }
  await page.getByRole('link', { name: 'Searchable Precision Gear', exact: true }).click();
  await page.getByRole('button', { name: 'Edit product', exact: true }).click();
  await expect(page.getByLabel('Part number', { exact: true })).toHaveValue('PID-572');
  await page.getByLabel('Part number', { exact: true }).fill('PID-975');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const stored = await snapshot(request, product);
  expect(stored.entity.id).toBe(new URL(product).pathname.split('/').at(-1));
  await page.goto('/manufacturing/products');
  await search.fill('PID-572');
  await expect(page.getByText('No products match', { exact: true })).toBeVisible();
  await search.fill('pid-975');
  await expect(page.locator('tbody tr')).toContainText('Searchable Precision Gear');
  await page.reload();
  await search.fill('975');
  await expect(page.locator('tbody tr')).toContainText('PID-975');
});
