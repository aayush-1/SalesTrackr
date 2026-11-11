import { expect, test, type APIRequestContext } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import type { Account, Revision } from '../src/types';

async function createAccount(request: APIRequestContext, data: Partial<Account>) {
  const headers = { 'X-SalesTrackr-Request': '1' };
  const prepared = await request.post('/api/writes/prepare', { headers, data: { entityType: 'account' } });
  expect(prepared.ok()).toBe(true);
  const response = await request.post('/api/accounts', {
    headers, data: { ...await prepared.json(), baseRevision: null, data },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).revision as Revision<Account>;
}

test('city and state are optional on create and edit and persist after reload', async ({ page }) => {
  test.setTimeout(60000);
  const name = `Location ${randomUUID()}`;
  let creates = 0;
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/accounts') creates++;
  });
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill(name);
  await page.getByLabel('Location / address', { exact: true }).fill('Unit 12, Industrial Road');
  const city = page.getByLabel('City', { exact: true });
  const state = page.getByLabel('State', { exact: true });
  const country = page.getByLabel('Country', { exact: true });
  await expect(country).toHaveValue('India');
  const submit = page.getByRole('button', { name: 'Create account', exact: true });
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible({ timeout: 15000 });
  expect(creates).toBe(1);
  await page.getByRole('button', { name: 'Edit account', exact: true }).click();
  await expect(city).toHaveValue('');
  await expect(state).toHaveValue('');
  await expect(country).toHaveValue('India');
  await city.fill('Mumbai');
  await state.selectOption('Maharashtra');
  await page.getByRole('button', { name: 'Save account', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.reload();
  await expect(page.locator('.page-header p')).toContainText('Mumbai, Maharashtra', { timeout: 15000 });
  await page.getByRole('button', { name: 'Edit account', exact: true }).click();
  await city.fill('');
  await state.selectOption('');
  await page.getByRole('button', { name: 'Save account', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.reload();
  await page.getByRole('button', { name: 'Edit account', exact: true }).click();
  await expect(city).toHaveValue('');
  await expect(state).toHaveValue('');
});

test('default country can be changed or cleared without being reset on edit', async ({ page }) => {
  test.setTimeout(60000);
  const name = `Country ${randomUUID()}`;
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill(name);
  const country = page.getByLabel('Country', { exact: true });
  await expect(country).toHaveValue('India');
  const state = page.getByLabel('State', { exact: true });
  await expect(state).toHaveJSProperty('tagName', 'SELECT');
  await expect(state.locator('option[value="Ladakh"]')).toHaveCount(1);
  await expect(state.locator('option[value="Delhi"]')).toHaveCount(1);
  await country.fill('Canada');
  await expect(state).toHaveJSProperty('tagName', 'INPUT');
  await state.fill('Ontario');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible({ timeout: 15000 });
  await page.reload();
  await page.getByRole('button', { name: 'Edit account', exact: true }).click();
  await expect(country).toHaveValue('Canada');
  await expect(state).toHaveValue('Ontario');
  await country.fill(' india ');
  await expect(state).toHaveJSProperty('tagName', 'SELECT');
  await expect(state).toHaveValue('Ontario');
  await state.selectOption('Delhi');
  await country.fill('Canada');
  await expect(state).toHaveValue('Delhi');
  await state.fill('Ontario');
  await country.fill('');
  await page.getByRole('button', { name: 'Save account', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.reload();
  await page.getByRole('button', { name: 'Edit account', exact: true }).click();
  await expect(country).toHaveValue('');
});

test('saved city suggestions are scoped, deduplicated and reused when saving', async ({ page, request }) => {
  test.setTimeout(90000);
  const suffix = randomUUID().slice(0, 8);
  const cityName = `Demo City ${suffix}`;
  await createAccount(request, { name: `First city ${suffix}`, city: cityName, state: 'Maharashtra' });
  await createAccount(request, { name: `Repeated city ${suffix}`, city: `demo   city ${suffix}`, state: 'Maharashtra' });
  await createAccount(request, {
    name: `Foreign city ${suffix}`, country: 'Canada', state: 'Ontario', city: `Other City ${suffix}`,
  });
  await page.goto('/accounts/new');
  const country = page.getByLabel('Country', { exact: true });
  const state = page.getByLabel('State', { exact: true });
  const city = page.getByLabel('City', { exact: true });
  await state.selectOption('Maharashtra');
  await city.focus();
  const options = page.getByRole('listbox', { name: 'Saved cities', exact: true });
  await expect(options.getByRole('option', { name: cityName, exact: true })).toHaveCount(1);
  await expect(options.getByRole('option', { name: `Other City ${suffix}`, exact: true })).toHaveCount(0);
  await city.fill(`  demo   CITY ${suffix}  `);
  await expect(options.getByRole('option', { name: cityName, exact: true })).toBeVisible();
  await page.getByLabel('Company name *', { exact: true }).fill(`City reuse ${randomUUID()}`);
  await expect(city).toHaveValue(cityName);
  const saved = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/accounts');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  const result = await (await saved).json();
  expect(result.revision.data.city).toBe(cityName);
  await expect(page.getByRole('button', { name: 'Edit account', exact: true })).toBeVisible({ timeout: 15000 });
  await page.getByRole('button', { name: 'Edit account', exact: true }).click();
  await country.fill('Canada');
  await state.fill('Ontario');
  await city.focus();
  await expect(options.getByRole('option', { name: `Other City ${suffix}`, exact: true })).toHaveCount(1);
  await expect(options.getByRole('option', { name: cityName, exact: true })).toHaveCount(0);
  await page.route('**/api/accounts/cities?**', route => route.abort());
  await state.fill('Quebec');
  await expect(page.getByText('Suggestions unavailable. You can still enter a city.', { exact: true })).toBeVisible();
  await city.fill(`New City ${suffix}`);
  await page.getByRole('button', { name: 'Save account', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.page-header p')).toContainText(`New City ${suffix}`);
});

test('city dropdown supports selection, keyboard navigation and custom entry without losing unsaved changes', async ({ page }) => {
  await page.route('**/api/accounts/cities?**', route => route.fulfill({ json: { cities: ['Mumbai', 'Pune'] } }));
  await page.goto('/accounts/new');
  const city = page.getByRole('combobox', { name: 'City', exact: true });
  const options = page.getByRole('listbox', { name: 'Saved cities', exact: true });
  const toggle = page.getByRole('button', { name: 'Toggle city suggestions', exact: true });
  await city.focus();
  await expect(options.getByRole('option')).toHaveCount(2);
  await options.getByRole('option', { name: 'Pune', exact: true }).click();
  await expect(city).toHaveValue('Pune');
  await expect(city).toBeFocused();
  await expect(city).toHaveAttribute('aria-expanded', 'false');
  const leaving = page.waitForEvent('dialog');
  const navigating = page.getByRole('link', { name: 'All accounts', exact: true }).click();
  const warning = await leaving;
  expect(warning.message()).toContain('discard unsaved changes');
  await warning.dismiss();
  await navigating;
  await expect(page).toHaveURL(/\/accounts\/new$/);
  await toggle.click();
  await expect(options.getByRole('option')).toHaveCount(2);
  await city.press('ArrowUp');
  await expect(options.getByRole('option', { name: 'Pune', exact: true })).toHaveAttribute('aria-selected', 'true');
  await city.press('ArrowDown');
  await expect(options.getByRole('option', { name: 'Mumbai', exact: true })).toHaveAttribute('aria-selected', 'true');
  await city.press('Escape');
  await expect(options).toHaveCount(0);
  await expect(city).toHaveValue('Pune');
  await page.getByLabel('Company name *', { exact: true }).fill(`Dropdown ${randomUUID()}`);
  const submit = page.getByRole('button', { name: 'Create account', exact: true });
  await expect(submit).toBeEnabled();
  await city.fill('mum');
  await expect(options.getByRole('option')).toHaveText(['Mumbai']);
  await city.press('ArrowDown');
  await city.press('Enter');
  await expect(city).toHaveValue('Mumbai');
  await expect(submit).toBeEnabled();
  await expect(page).toHaveURL(/\/accounts\/new$/);
  await toggle.click();
  await city.press('Tab');
  await expect(options).toHaveCount(0);
  await city.fill('New City');
  await expect(page.getByText('No saved cities match. You can enter a new city.', { exact: true })).toBeVisible();
  await city.press('Enter');
  await expect(options).toHaveCount(0);
  await expect(city).toHaveValue('New City');
  await expect(submit).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await toggle.click();
  await expect(options.getByRole('option')).toHaveCount(2);
  const bounds = await options.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('city and state filters reset pagination and PDF exports every matching page', async ({ page, request }) => {
  test.setTimeout(90000);
  const prefix = `Directory ${randomUUID().slice(0, 8)}`;
  for (let index = 0; index < 26; index++) {
    await createAccount(request, {
      name: `${prefix} ${String(index).padStart(2, '0')}`,
      address: `Unit ${index}, Industrial Road`, city: 'Pune', state: 'Maharashtra',
    });
  }
  await createAccount(request, { name: `${prefix} Mumbai`, city: 'Mumbai', state: 'Maharashtra' });
  await createAccount(request, { name: `${prefix} other state`, city: 'Pune', state: 'Karnataka' });
  await page.goto('/accounts');
  const table = page.getByRole('table', { name: 'Accounts', exact: true });
  await page.getByLabel('Search accounts', { exact: true }).fill(prefix);
  await expect(page.locator('.pagination')).toContainText('28 accounts');
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(table.locator('tbody tr')).toHaveCount(4);
  await page.getByLabel('City', { exact: true }).fill('pune');
  await expect(page.getByRole('button', { name: 'Previous', exact: true })).toBeDisabled();
  await expect(page.locator('.pagination')).toContainText('27 accounts');
  await page.getByLabel('State', { exact: true }).fill('MAHARASHTRA');
  await expect(page.locator('.pagination')).toContainText('26 accounts');
  await expect(table.locator('tbody tr')).toHaveCount(24);
  await expect(table).not.toContainText(`${prefix} Mumbai`);
  await expect(table).not.toContainText(`${prefix} other state`);
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(table.locator('tbody tr')).toHaveCount(2);
  const requested = page.waitForRequest(request => new URL(request.url()).pathname === '/api/accounts/pdf');
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Create PDF', exact: true }).click();
  const url = new URL((await requested).url());
  expect(Object.fromEntries(url.searchParams)).toEqual({ search: prefix, city: 'pune', state: 'MAHARASHTRA' });
  const download = await downloading;
  expect(download.suggestedFilename()).toBe('accounts.pdf');
  const file = test.info().outputPath('accounts.pdf');
  await download.saveAs(file);
  expect((await readFile(file)).subarray(0, 5).toString()).toBe('%PDF-');
  await page.screenshot({ path: test.info().outputPath('accounts-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(element => element.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await expect(page.getByRole('button', { name: 'Create PDF', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('accounts-mobile.png'), fullPage: true });
  await page.getByLabel('City', { exact: true }).fill('Unknown');
  await expect(page.getByText('No accounts found', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await expect(page.getByLabel('City', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('State', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('Search accounts', { exact: true })).toHaveValue('');
});
