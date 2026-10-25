import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import type { CleanupRequest, CleanupSettings, Me } from '../src/types';

test('cleanup selects only finished records, confirms deletion, retries a lost response and preserves backups', async ({ page, baseURL }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const me = await (await page.request.get('/api/me')).json() as Me;
  expect(me.mode).toBe('demo');
  expect(me.storageBackend).toBe('firestore');
  const headers = { 'X-SalesTrackr-Request': '1', 'X-SalesTrackr-Generation': me.generation!, Origin: baseURL! };
  type Saved = { id: string; entityId: string };
  const write = async (path: string, data: unknown, current?: Saved): Promise<Saved> => {
    const response = await page.request.post(`/api${path}`, {
      headers, data: {
        writeId: randomUUID().replaceAll('-', ''), entityId: current?.entityId || randomUUID().replaceAll('-', ''),
        baseRevision: current?.id || null, data,
      },
    });
    expect(response.status(), await response.text()).toBe(200);
    return (await response.json()).revision;
  };
  const owner = await write('/accounts', { name: `Cleanup browser company ${randomUUID().slice(0, 8)}` });
  const record = async (state: 'complete' | 'closed' | 'discarded' | 'active') => {
    let saved = await write('/records', { accountId: owner.entityId, productNotes: `Browser cleanup sample: ${state}` });
    if (state === 'discarded') return await write(`/records/${saved.entityId}/discard`, { reason: 'Synthetic obsolete enquiry' }, saved);
    if (state === 'active') return saved;
    for (const stage of ['follow_up', 'order_received', 'dispatched', 'delivered']) {
      saved = await write(`/records/${saved.entityId}/stage`, { stage }, saved);
    }
    saved = await write(`/records/${saved.entityId}/payment`, { received: true }, saved);
    return state === 'complete' ? await write(`/records/${saved.entityId}/feedback`, { received: true }, saved) : saved;
  };
  const complete = await record('complete');
  const discarded = await record('discarded');
  const awaiting = await record('closed');
  const active = await record('active');
  const backupResponse = await page.request.post('/api/backups', { headers, data: { writeId: randomUUID().replaceAll('-', '') } });
  expect(backupResponse.ok()).toBe(true);
  const backup = (await backupResponse.json()).backup;
  const backupBytes = await (await page.request.get(`/api/backups/${backup.id}/download`)).body();
  const retained = await Promise.all([active, awaiting].map(async row =>
    (await page.request.get(`/api/records/${row.entityId}/detail`)).json()));
  await page.goto('/settings?tab=cleanup');
  await expect(page.getByRole('tab', { name: 'Record cleanup', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('Closed + feedback received', { exact: true })).toBeVisible();
  await expect(page.getByText('Browser cleanup sample: active', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Browser cleanup sample: closed', { exact: true })).toHaveCount(0);
  const all = page.getByRole('checkbox', { name: 'Select all eligible records', exact: true });
  const remove = page.getByRole('button', { name: /^Delete selected/ });
  await expect(remove).toBeDisabled();
  await page.getByRole('row').filter({ hasText: 'Browser cleanup sample: complete' }).getByRole('checkbox').check();
  await expect(all).toHaveJSProperty('indeterminate', true);
  await all.check();
  await expect(remove).toHaveText('Delete selected (2)');
  await page.screenshot({ path: testInfo.outputPath('record-cleanup-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('record-cleanup-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await remove.click();
  const dialog = page.getByRole('dialog', { name: 'Permanently delete sales records?' });
  await expect(dialog.getByRole('button', { name: 'Permanently delete records', exact: true })).toBeDisabled();
  await dialog.getByLabel('Type DELETE to confirm permanent deletion').fill('delete');
  await expect(dialog.getByRole('button', { name: 'Permanently delete records', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect((await page.request.get(`/api/records/${complete.entityId}/detail`)).status()).toBe(200);
  const requests: CleanupRequest[] = [];
  await page.route('**/api/settings/record-cleanup', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) {
      expect((await route.fetch()).status()).toBe(200);
      await route.abort('failed');
    } else await route.continue();
  });
  await remove.click();
  await dialog.getByLabel('Type DELETE to confirm permanent deletion').fill('DELETE');
  await dialog.getByRole('button', { name: 'Permanently delete records', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Retry same cleanup', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Retry same cleanup', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText(/^Deleted 2 local demo sales records and/)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Nothing to clean up', exact: true })).toBeVisible();
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
  for (const removed of [complete, discarded]) {
    expect((await page.request.get(`/api/records/${removed.entityId}/detail`)).status()).toBe(404);
  }
  for (const [index, kept] of [active, awaiting].entries()) {
    expect(await (await page.request.get(`/api/records/${kept.entityId}/detail`)).json()).toMatchObject({
      revision: retained[index].revision,
    });
  }
  expect(await (await page.request.get(`/api/backups/${backup.id}/download`)).body()).toEqual(backupBytes);
  expect(errors).toEqual([]);
});

test('select all processes more than 500 rows in bounded requests', async ({ page }) => {
  const me = await (await page.request.get('/api/me')).json() as Me;
  let items: CleanupSettings['items'] = Array.from({ length: 501 }, (_, index) => ({
    entityId: `cleanup-record-${String(index).padStart(5, '0')}`, baseRevision: `cleanup-head-${String(index).padStart(5, '0')}`,
    accountName: `Bulk cleanup ${index}`, productNotes: 'Synthetic bulk fixture', discarded: true, closedOn: null,
  }));
  const requests: CleanupRequest[] = [];
  await page.route('**/api/settings/record-cleanup', async route => {
    if (route.request().method() === 'POST') {
      const request = route.request().postDataJSON() as CleanupRequest;
      requests.push(request);
      const ids = request.records.map(row => row.entityId);
      items = items.filter(row => !ids.includes(row.entityId));
      return route.fulfill({ json: { deletedIds: ids, deletedCount: ids.length, savedVersionsRemoved: ids.length * 2, kept: [] } });
    }
    return route.fulfill({ json: {
      items, total: items.length, generation: me.generation, canDelete: true,
      pending: null, cleanupInProgress: false, lastError: null,
    } });
  });
  await page.goto('/settings?tab=cleanup');
  await page.getByRole('checkbox', { name: 'Select all eligible records', exact: true }).check();
  await page.getByRole('button', { name: 'Delete selected (501)', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Type DELETE to confirm permanent deletion').fill('DELETE');
  await dialog.getByRole('button', { name: 'Permanently delete records', exact: true }).click();
  await expect(page.getByText('Deleted 501 local demo sales records and 1002 saved versions.', { exact: false })).toBeVisible();
  expect(requests.map(row => row.records.length)).toEqual([200, 200, 101]);
  expect(new Set(requests.map(row => row.writeId)).size).toBe(3);
  expect(new Set(requests.flatMap(row => row.records.map(item => item.entityId))).size).toBe(501);
});
