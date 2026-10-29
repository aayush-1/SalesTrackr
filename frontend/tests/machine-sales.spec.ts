import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

async function downloadReport(page: Page, endpoint: string, filename: string, filters: Record<string, string>) {
  const requested = page.waitForRequest(request => request.url().includes(`${endpoint}?`));
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Create PDF', exact: true }).click();
  const download = await downloaded, url = new URL((await requested).url());
  for (const [name, value] of Object.entries(filters)) expect(url.searchParams.get(name)).toBe(value);
  expect(url.searchParams.has('offset')).toBe(false);
  expect(url.searchParams.has('limit')).toBe(false);
  expect(download.suggestedFilename()).toBe(filename);
  const file = test.info().outputPath(filename);
  await download.saveAs(file);
  expect((await readFile(file)).subarray(0, 5).toString()).toBe('%PDF-');
}

test('models, repeated machines, sales corrections, manual deliveries and machine coverage', async ({ page, request }) => {
  test.setTimeout(180_000);
  const suffix = Date.now().toString();
  const modelName = `Press model ${suffix}`, company = `Machine customer ${suffix}`;
  const gearName = `Model gear ${suffix}`, bearingName = `Common bearing ${suffix}`;
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));

  await page.goto('/models');
  await page.getByRole('button', { name: 'New model', exact: true }).click();
  await page.getByLabel('Model name *', { exact: true }).fill(modelName);
  await page.getByRole('button', { name: 'Save model', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('table', { name: 'Models', exact: true })).toContainText(modelName);

  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill(company);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  const accountId = page.url().split('/').at(-1)!;
  for (const name of ['Press A', 'Press B']) {
    await page.getByRole('button', { name: 'Add machine', exact: true }).click();
    await page.getByLabel('Machine name *', { exact: true }).fill(name);
    await page.getByLabel('Model *', { exact: true }).selectOption({ label: modelName });
    if (name === 'Press A') {
      await page.getByLabel('Serial number', { exact: true }).fill('DEMO-SERIAL-A');
      await page.getByLabel('Manufacturing year', { exact: true }).fill('2019');
    }
    await page.getByRole('button', { name: 'Save machine', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  }
  await expect(page.getByRole('table', { name: 'Account machines' }).getByRole('row')).toHaveCount(3);

  for (const name of [gearName, bearingName]) {
    await page.goto('/manufacturing/products');
    await page.getByRole('button', { name: 'New product', exact: true }).click();
    await page.getByLabel('Product name', { exact: true }).fill(name);
    await page.getByLabel('Stock quantity *', { exact: true }).fill('100');
    await page.getByLabel('Minimum stock quantity *', { exact: true }).fill('5');
    await page.getByLabel('Part number', { exact: true }).fill(name === gearName ? `G-${suffix}` : `B-${suffix}`);
    await page.getByLabel('Notes', { exact: true }).fill(name === gearName ? 'Grease every 6 months.' : 'For every model.');
    if (name === gearName) await page.getByLabel(modelName, { exact: true }).check();
    await page.getByRole('button', { name: 'Create product', exact: true }).click();
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  }
  await page.goto('/manufacturing/model-parts');
  await expect(page.getByRole('link', { name: "Master Product's List", exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: "Master Product's List", exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create PDF', exact: true })).toBeDisabled();
  await page.getByLabel('Model', { exact: true }).selectOption({ label: modelName });
  const modelParts = page.getByRole('table', { name: "Master Product's List", exact: true });
  await expect(modelParts.getByRole('row').filter({ hasText: gearName })).toContainText(`G-${suffix}`);
  await expect(modelParts.getByRole('row').filter({ hasText: gearName })).toContainText('Grease every 6 months.');
  await expect(modelParts.getByRole('row').filter({ hasText: bearingName })).toContainText('Common');
  await expect(modelParts.getByRole('row').filter({ hasText: bearingName })).toContainText('For every model.');
  await downloadReport(page, '/parts/catalog/pdf', 'master-products-list.pdf', {
    modelId: await page.getByLabel('Model', { exact: true }).inputValue(),
  });
  await page.goto(`/records/new?accountId=${accountId}`);
  await page.getByRole('button', { name: 'Add part', exact: true }).click();
  await page.getByLabel('Machine 1', { exact: true }).selectOption({ label: 'Press A - DEMO-SERIAL-A' });
  await page.getByLabel('Product 1 *', { exact: true }).selectOption({ label: `G-${suffix} - ${gearName}` });
  await page.getByLabel('Quantity 1 *', { exact: true }).fill('2');
  await page.getByRole('button', { name: 'Create sales record', exact: true }).click();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  const recordId = page.url().split('/').at(-1)!;
  await page.getByRole('button', { name: 'Edit parts', exact: true }).click();
  await page.getByRole('button', { name: 'Add part', exact: true }).click();
  await page.getByLabel('Product 2 *', { exact: true }).selectOption({ label: `B-${suffix} - ${bearingName}` });
  await page.getByLabel('Quantity 2 *', { exact: true }).fill('3');
  await page.getByRole('button', { name: 'Save parts', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  for (const stage of ['Follow-up', 'Order in hand', 'Dispatched', 'Delivered']) {
    await page.getByRole('button', { name: stage, exact: true }).click();
    await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
    await expect(page.locator('.stages .current strong')).toHaveText(stage);
  }
  await page.getByRole('button', { name: 'Received', exact: true }).click();
  await expect(page.locator('.stages .current strong')).toHaveText('Closed');

  await page.goto('/machine-parts');
  await expect(page.getByRole('link', { name: 'Potential Spares machine wise', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Potential Spares machine wise', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create PDF', exact: true })).toBeDisabled();
  await page.getByLabel('Account', { exact: true }).selectOption({ label: company });
  await page.getByLabel('Machine', { exact: true }).selectOption({ label: 'Press B' });
  const table = page.getByRole('table', { name: 'Potential Spares machine wise', exact: true });
  await expect(table.getByRole('row').filter({ hasText: gearName })).toContainText('Not sent');
  await expect(table.getByRole('row').filter({ hasText: bearingName })).toHaveClass('delivery-sent');
  let failDownload = true;
  await page.route('**/api/parts/coverage/pdf?**', async route => {
    if (failDownload) {
      failDownload = false;
      return route.fulfill({ status: 503, json: { error: { message: 'PDF temporarily unavailable.' } } });
    }
    return route.continue();
  });
  await page.getByRole('button', { name: 'Create PDF', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('PDF temporarily unavailable.');
  const requested = page.waitForRequest(request => request.url().includes('/parts/coverage/pdf?'));
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Create PDF', exact: true }).click();
  const download = await downloaded;
  const url = new URL((await requested).url());
  expect(url.searchParams.get('accountId')).toBe(accountId);
  expect(url.searchParams.get('machineId')).toBe(await page.getByLabel('Machine', { exact: true }).inputValue());
  expect(download.suggestedFilename()).toBe('potential-spares-machine-wise.pdf');
  const pdfPath = test.info().outputPath('potential-spares-machine-wise.pdf');
  await download.saveAs(pdfPath);
  expect((await readFile(pdfPath)).subarray(0, 5).toString()).toBe('%PDF-');
  await page.screenshot({ path: test.info().outputPath('machine-parts-desktop.png'), fullPage: true });

  await page.goto(`/parts-history?accountId=${accountId}`);
  await expect(page.getByRole('link', { name: 'Sales History', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Sales History', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Add past delivery', exact: true }).click();
  await page.getByLabel('Product *', { exact: true }).selectOption({ label: `G-${suffix} - ${gearName}` });
  const me = await (await request.get('/api/me')).json();
  const date = new Date(`${me.today}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() - 7);
  await page.getByLabel('Delivered date *', { exact: true }).fill(date.toISOString().slice(0, 10));
  await page.getByRole('button', { name: 'Save delivery', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  let manual = page.getByRole('table', { name: 'Sales History', exact: true }).getByRole('row').filter({ hasText: gearName }).filter({ hasText: 'Manual' });
  await manual.getByRole('button', { name: 'Correct', exact: true }).click();
  date.setUTCDate(date.getUTCDate() - 1);
  await page.getByLabel('Delivered date *', { exact: true }).fill(date.toISOString().slice(0, 10));
  await page.getByLabel('Correction reason *', { exact: true }).fill('Correct the old dispatch note date');
  await page.getByRole('button', { name: 'Save correction', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await manual.getByRole('button', { name: 'History', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('2 saved versions');
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
  await manual.getByRole('button', { name: 'Void', exact: true }).click();
  await page.getByLabel('Void reason *', { exact: true }).fill('Wrongly entered delivery');
  await page.getByRole('button', { name: 'Void delivery', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(manual).toHaveCount(0);
  await page.getByLabel('Include withdrawn entries', { exact: true }).check();
  manual = page.getByRole('table', { name: 'Sales History', exact: true }).getByRole('row').filter({ hasText: gearName }).filter({ hasText: 'Manual' });
  await expect(manual).toContainText('Withdrawn');
  const day = date.toISOString().slice(0, 10);
  await page.getByLabel('Machine', { exact: true }).selectOption('unassigned');
  await page.getByLabel('From', { exact: true }).fill(day);
  await page.getByLabel('To', { exact: true }).fill(day);
  await expect(manual).toContainText('Withdrawn');
  await downloadReport(page, '/parts/history/pdf', 'sales-history.pdf', {
    accountId, machineId: 'unassigned', start: day, end: day, includeVoided: 'true',
  });

  await page.goto(`/records/${recordId}`);
  await page.getByRole('button', { name: 'Edit parts', exact: true }).click();
  await page.getByLabel('Machine 1', { exact: true }).selectOption({ label: 'Press B' });
  await page.getByLabel('Correction reason *', { exact: true }).fill('Correct machine assignment on closed sale');
  await page.getByRole('button', { name: 'Save parts', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('table', { name: 'Sales parts', exact: true })).toContainText('Press B');
  await page.goto('/machine-parts');
  await page.getByLabel('Account', { exact: true }).selectOption({ label: company });
  await page.getByLabel('Machine', { exact: true }).selectOption({ label: 'Press B' });
  await expect(page.getByRole('table', { name: 'Potential Spares machine wise', exact: true }).getByRole('row').filter({ hasText: gearName })).toHaveClass('delivery-sent');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: test.info().outputPath('machine-parts-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
