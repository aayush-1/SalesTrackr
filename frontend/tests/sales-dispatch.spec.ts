import { expect, test } from '@playwright/test';

test('ordinary sales dispatch without the retired stock confirmation', async ({ page, request }) => {
  const headers = { 'X-SalesTrackr-Request': '1' };
  const create = async (kind: string, data: object) => {
    const ticket = await (await request.post('/api/writes/prepare', { headers, data: { entityType: kind } })).json();
    const response = await request.post(`/api/${kind}s`, { headers, data: { ...ticket, baseRevision: null, data } });
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()).revision;
  };
  const account = await create('account', { name: `Dispatch example ${Date.now()}` });
  const original = await create('record', { accountId: account.entityId });
  await page.goto(`/records/${original.entityId}`);
  await expect(page.getByRole('region', { name: 'Stock update', exact: true })).toHaveCount(0);
  await expect(page.getByRole('navigation').getByRole('link', { name: 'Stock update pending', exact: true })).toHaveCount(0);
  for (const stage of ['Follow-up', 'Order in hand', 'Dispatched', 'Delivered']) {
    await page.getByRole('button', { name: stage, exact: true }).click();
    await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.stages .current strong')).toHaveText(stage);
  }
  await page.getByRole('button', { name: 'Received', exact: true }).click();
  await expect(page.locator('.stages .current strong')).toHaveText('Closed');
});
