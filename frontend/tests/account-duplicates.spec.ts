import { expect, test, type APIRequestContext } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import type { Account, Revision } from '../src/types';

const headers = { 'X-SalesTrackr-Request': '1' };
const reviewLabel = 'I checked these matches; this is a different company.';
let existing: Revision<Account>;

async function createAccount(request: APIRequestContext, name: string) {
  const prepared = await request.post('/api/writes/prepare', { headers, data: { entityType: 'account' } });
  expect(prepared.ok()).toBe(true);
  const response = await request.post('/api/accounts', {
    headers, data: { ...await prepared.json(), baseRevision: null, data: { name, address: 'Synthetic preview city' } },
  });
  expect(response.ok()).toBe(true);
  return (await response.json()).revision as Revision<Account>;
}

test.beforeAll(async ({ request }) => {
  const response = await request.get('/api/accounts?search=G%20M%20Offset');
  const accounts = await response.json();
  existing = accounts.items.find((row: Revision<Account>) => row.data.name === 'G M Offset')
    || await createAccount(request, 'G M Offset');
});

test('normalized duplicate is blocked and existing account can be reused on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let creates = 0;
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/accounts') creates++;
  });
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill('g.m. OFFSET');
  await expect(page.getByText('Account already exists.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create account', exact: true })).toBeDisabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('duplicate-mobile.png'), fullPage: true });
  await page.getByLabel('Account notes', { exact: true }).fill('Do not silently lose these details.');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: 'Use this account', exact: true }).click();
  await expect(page.getByLabel('Account notes', { exact: true })).toHaveValue('Do not silently lose these details.');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Use this account', exact: true }).click();
  await expect(page).toHaveURL(`/accounts/${existing.entityId}`);
  await expect(page.getByRole('heading', { name: 'G M Offset', exact: true })).toBeVisible();
  expect(creates).toBe(0);
});

test('sales form reuses an existing account without losing its enquiry draft', async ({ page }) => {
  await page.goto('/records/new');
  await page.getByLabel('Product / requirement notes', { exact: true }).fill('Keep this enquiry draft.');
  await page.getByRole('button', { name: 'Create a new account', exact: true }).click();
  const modal = page.getByRole('dialog');
  await modal.getByLabel('Company name *', { exact: true }).fill('GM offset');
  await expect(modal.getByText('Account already exists.', { exact: true })).toBeVisible();
  await modal.getByRole('button', { name: 'Use this account', exact: true }).click();
  await expect(modal).toHaveCount(0);
  await expect(page.getByLabel('Account *', { exact: true })).toHaveValue(existing.entityId);
  await expect(page.getByLabel('Product / requirement notes', { exact: true })).toHaveValue('Keep this enquiry draft.');
});

test('similar name needs review before a genuinely different company can be saved', async ({ page, request }) => {
  const suffix = randomUUID().slice(0, 8);
  const original = `Aurora Lithography ${suffix}`;
  const company = `Aurora Lithograpy ${suffix}`;
  await createAccount(request, original);
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill(company);
  await expect(page.getByText('Similar accounts already exist.', { exact: true })).toBeVisible();
  await expect(page.getByText(original, { exact: true })).toBeVisible();
  const submit = page.getByRole('button', { name: 'Create account', exact: true });
  await expect(submit).toBeDisabled();
  await page.getByLabel(reviewLabel, { exact: true }).check();
  await expect(submit).toBeEnabled();
  await page.screenshot({ path: test.info().outputPath('similar-desktop.png'), fullPage: true });
  await submit.click();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
});

test('failed lookup blocks creation until a successful retry', async ({ page }) => {
  const pattern = '**/api/accounts/matches?**';
  await page.route(pattern, route => route.abort());
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill('GM offset');
  const retry = page.getByRole('button', { name: 'Retry account check', exact: true });
  await expect(retry).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create account', exact: true })).toBeDisabled();
  await page.unroute(pattern);
  await retry.click();
  await expect(page.getByText('Account already exists.', { exact: true })).toBeVisible();
  await expect(retry).toHaveCount(0);
});

test('stale lookup cannot replace results for a newer name', async ({ page }) => {
  let release!: () => void;
  let started!: () => void;
  let completed = false;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { started = resolve; });
  await page.route('**/api/accounts/matches?**', async route => {
    if (new URL(route.request().url()).searchParams.get('name') !== 'GM offset') return route.continue();
    started();
    await waiting;
    await route.fulfill({
      json: { name: 'GM offset', total: 1, exactCount: 1, complete: true, conflictCount: 0,
        items: [{ revision: existing, match: 'exact', conflicted: false }] },
    });
    completed = true;
  });
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill('GM offset');
  await requested;
  await page.getByLabel('Company name *', { exact: true }).fill(`Zeftronic ${randomUUID()}`);
  const submit = page.getByRole('button', { name: 'Create account', exact: true });
  await expect(submit).toBeEnabled();
  release();
  await expect.poll(() => completed).toBe(true);
  await expect(page.getByText('Account already exists.', { exact: true })).toHaveCount(0);
  await expect(submit).toBeEnabled();
});

test('account created after hints is rejected at save and shown without losing the form', async ({ page, request }) => {
  const company = `Carbonia ${randomUUID().slice(0, 8)}`;
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill(company);
  await page.getByLabel('Account notes', { exact: true }).fill('Keep this form after a competing create.');
  const submit = page.getByRole('button', { name: 'Create account', exact: true });
  await expect(submit).toBeEnabled();
  const competing = await createAccount(request, company);
  await submit.click();
  await expect(page.getByText('Account already exists.', { exact: true })).toBeVisible();
  await expect(submit).toBeDisabled();
  await expect(page.getByLabel('Account notes', { exact: true })).toHaveValue('Keep this form after a competing create.');
  const found = await (await request.get(`/api/accounts/matches?name=${encodeURIComponent(company)}`)).json();
  expect(found.exactCount).toBe(1);
  expect(found.items[0].revision.entityId).toBe(competing.entityId);
});

test('lost create response retries the same operation even when its own account matches', async ({ page, request }) => {
  const company = `Quintrax ${randomUUID().slice(0, 8)}`;
  const attempts: object[] = [];
  let prepares = 0;
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/writes/prepare') prepares++;
  });
  await page.route('**/api/accounts', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    attempts.push(route.request().postDataJSON());
    const response = await route.fetch();
    if (attempts.length === 1) {
      await route.fulfill({ status: 503, json: { error: { code: 'save_unknown', message: 'Synthetic unconfirmed account save.' } } });
    } else {
      await route.fulfill({ response });
    }
  });
  await page.goto('/accounts/new');
  const input = page.getByLabel('Company name *', { exact: true });
  await input.fill(company);
  const submit = page.getByRole('button', { name: 'Create account', exact: true });
  await submit.click();
  await expect(page.getByText('Synthetic unconfirmed account save.', { exact: true })).toBeVisible();
  await input.fill(`${company}x`);
  await expect(page.getByText('Similar accounts already exist.', { exact: true })).toBeVisible();
  await input.fill(company);
  await expect(page.getByText('Account already exists.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Use this account', exact: true })).toBeDisabled();
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  expect(attempts).toHaveLength(2);
  expect(attempts[0]).toEqual(attempts[1]);
  expect(prepares).toBe(1);
  const id = new URL(page.url()).pathname.split('/').at(-1)!;
  const history = await (await request.get(`/api/entities/account/${id}/history`)).json();
  expect(history.items).toHaveLength(1);
});
