import { expect, test } from '@playwright/test';

test('stock confirmation enables at Order in hand, retries safely, and is required before dispatch', async ({ page, request }) => {
  const headers = { 'X-SalesTrackr-Request': '1' };
  const create = async (kind: string, data: object) => {
    const ticket = await (await request.post('/api/writes/prepare', { headers, data: { entityType: kind } })).json();
    const response = await request.post(`/api/${kind}s`, { headers, data: { ...ticket, baseRevision: null, data } });
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()).revision;
  };
  const name = `Stock example ${Date.now()}`;
  const account = await create('account', { name });
  const original = await create('record', { accountId: account.entityId, productNotes: 'Synthetic dispatch stock confirmation' });
  const stockPath = `/api/records/${original.entityId}/stock`;
  const attempts: unknown[] = [];
  let loseResponse = true;
  await page.route(`**${stockPath}`, async route => {
    attempts.push(route.request().postDataJSON());
    const response = await route.fetch();
    expect(response.ok(), await response.text()).toBe(true);
    if (loseResponse) { loseResponse = false; return route.abort(); }
    return route.fulfill({ response });
  });
  await page.goto(`/records/${original.entityId}`);
  const stock = page.getByRole('region', { name: 'Stock update', exact: true });
  const confirm = stock.getByRole('button', { name: 'Stock updated', exact: true });
  await expect(confirm).toBeDisabled();
  await expect(stock).toContainText('Available after Order in hand.');
  for (const stage of ['Follow-up', 'Order in hand']) {
    await page.getByRole('button', { name: stage, exact: true }).click();
    await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.stages .current strong')).toHaveText(stage);
    if (stage !== 'Order in hand') await expect(confirm).toBeDisabled();
  }
  await expect(confirm).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Dispatched', exact: true })).toBeDisabled();
  const recordUrl = page.url();
  await page.getByRole('navigation').getByRole('link', { name: 'Stock update pending', exact: true }).click();
  await page.getByLabel('Search', { exact: true }).fill(name);
  await expect(page.locator('tbody tr')).toHaveCount(1);
  const collectionLinks = await page.getByRole('navigation').getByRole('link').allTextContents();
  expect(collectionLinks.indexOf('Stock update pending')).toBe(collectionLinks.indexOf('Feedback pending') + 1);
  await page.goto(recordUrl);
  const before = (await (await request.get(`/api/records/${original.entityId}/detail`)).json()).revision;
  await page.getByLabel('Payment details', { exact: true }).fill('Unsaved payment draft survives stock confirmation.');
  await confirm.click();
  await expect(stock.getByRole('alert')).toBeVisible();
  await confirm.click();
  await expect(stock.locator('.badge')).toHaveText('Updated');
  await expect(confirm).toBeDisabled();
  expect(attempts).toHaveLength(2);
  expect(attempts[0]).toEqual(attempts[1]);
  await expect(page.getByLabel('Payment details', { exact: true })).toHaveValue('Unsaved payment draft survives stock confirmation.');
  const saved = (await (await request.get(`/api/records/${original.entityId}/detail`)).json()).revision;
  expect(saved.data.stageDates).toEqual(before.data.stageDates);
  expect(saved.data.paymentReceived).toBe(false);
  expect(saved.data.feedbackReceived).toBe(false);
  await stock.getByRole('button', { name: 'Undo stock updated', exact: true }).click();
  await expect(stock.locator('.badge')).toHaveText('Pending');
  await expect(page.getByRole('button', { name: 'Dispatched', exact: true })).toBeDisabled();
  await confirm.click();
  await expect(stock.locator('.badge')).toHaveText('Updated');
  await expect(page.locator('.stages .current strong')).toHaveText('Order in hand');
  for (const stage of ['Dispatched', 'Delivered']) {
    await page.getByRole('button', { name: stage, exact: true }).click();
    await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.stages .current strong')).toHaveText(stage);
    await expect(stock.getByRole('button', { name: 'Undo stock updated', exact: true })).toHaveCount(0);
  }
  let releasePayment = () => {};
  const paymentResponse = new Promise<void>(resolve => { releasePayment = resolve; });
  await page.route(`**/api/records/${original.entityId}/payment`, async route => {
    const response = await route.fetch();
    await paymentResponse;
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Received', exact: true }).click();
  try {
    await expect(confirm).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Closed', exact: true })).toBeDisabled();
  } finally { releasePayment(); }
  await expect(page.locator('.payment-panel .badge')).toHaveText('Received');
  await expect(stock.locator('.badge')).toHaveText('Updated');
  await expect(page.locator('.stages .current strong')).toHaveText('Closed');
  await expect(stock.getByRole('button', { name: 'Undo stock updated', exact: true })).toHaveCount(0);
  await page.reload();
  await expect(stock.locator('.badge')).toHaveText('Updated');
  await expect(stock).toContainText('Updated on');
  await page.goto('/view/stock-pending');
  await page.getByLabel('Search', { exact: true }).fill(name);
  await expect(page.locator('tbody tr')).toHaveCount(0);
  await page.goto(recordUrl);
  for (const width of [390, 768]) {
    await page.setViewportSize({ width, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
});
