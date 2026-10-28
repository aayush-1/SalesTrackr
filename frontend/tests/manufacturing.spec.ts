import { expect, test } from '@playwright/test';

// Layout coverage stays independent of emulator availability; producttrackr.spec uses real storage.
test.beforeEach(async ({ page }) => {
  await page.route('**/api/manufacturing/**', route => route.fulfill({ json: {
    workspaceId: 'layout-fixture', generation: 'layout-generation', mode: 'demo', storageBackend: 'firestore',
    items: [], total: 0, supplierCount: 0, jobCount: 0, counts: { active: 0, all: 0, ready: 0, in_progress: 0, completed: 0, cancelled: 0 },
    provider: 'local', retention: 10, location: 'Local simulated ProductTrackr backup folder',
    backups: [], canBackup: true, canRestore: true, lastBackupAt: null, lastError: null,
    canDelete: true, cleanupInProgress: false, pending: null,
  } }));
});

test('ProductTrackr has separate directories and settings without sales API calls', async ({ page }) => {
  const sales: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', route => {
    if (route.request().url().includes('/api/manufacturing/')) return route.fallback();
    sales.push(route.request().url()); return route.abort();
  });
  await page.goto('/manufacturing');
  await expect(page.getByRole('heading', { name: 'Every job. One clear next step.' })).toBeVisible();
  await expect(page.locator('.topbar,.breadcrumb')).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toHaveCount(0);
  await expect(page.locator('.demo-banner')).toHaveCount(0);
  for (const title of ['Products', 'Operations', 'Suppliers', 'Contacts']) {
    await page.getByRole('navigation', { name: 'Manufacturing navigation' }).getByRole('link', { name: title, exact: true }).click();
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  }
  await page.getByRole('link', { name: 'ProductTrackr settings', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'General', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tab', { name: 'Backup and recovery', exact: true })).toHaveCount(0);
  await expect(page.getByRole('tab', { name: 'Record cleanup', exact: true })).toHaveCount(1);
  await expect(page.getByRole('tab', { name: 'Spreadsheet export', exact: true })).toHaveCount(1);
  await page.getByRole('tab', { name: 'Records & relationships', exact: true }).click();
  await expect(page.getByRole('tabpanel')).toContainText('exactly one primary');
  await page.getByRole('tab', { name: 'Data & recovery', exact: true }).click();
  await page.reload();
  await expect(page.getByRole('tab', { name: 'Data & recovery', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel')).toContainText("SalesTrackr's Drive backups do not include this workspace.");
  await page.getByRole('tab', { name: 'Record cleanup', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Completed record cleanup', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Nothing to clean up', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Spreadsheet export', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Directory spreadsheet', exact: true })).toBeVisible();
  await page.goto('/manufacturing/records');
  await expect(page).toHaveURL(/\/manufacturing\/records$/);
  await expect(page.getByRole('heading', { name: 'Manufacturing records', exact: true })).toBeVisible();
  await page.goto('/manufacturing/blueprint');
  await expect(page).toHaveURL(/\/manufacturing\/operations$/);
  await page.goto('/manufacturing/unknown-page');
  await expect(page.getByRole('heading', { name: 'ProductTrackr page not found' })).toBeVisible();
  expect(sales).toEqual([]); expect(errors).toEqual([]);
});

test('production ProductTrackr shows cloud storage without a local-preview banner', async ({ page }) => {
  await page.route('**/api/manufacturing/meta', route => route.fulfill({ json: {
    workspaceId: 'producttrackr', generation: 'production-generation', mode: 'production', storageBackend: 'firestore',
  } }));
  await page.goto('/manufacturing/settings');
  await expect(page.getByRole('heading', { name: 'ProductTrackr settings', exact: true })).toBeVisible();
  await expect(page.locator('.unit-preview-notice')).toHaveCount(0);
  await expect(page.getByRole('tabpanel')).toContainText('Cloud Firestore');
  await expect(page.getByRole('tabpanel')).not.toContainText('Local Firestore emulator');
});

test('workspace switcher changes the entire space and leaves sales settings intact', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'A clear view of your sales.', exact: true })).toBeVisible();
  const trigger = page.getByRole('button', { name: 'Switch workspace', exact: true });
  await trigger.click();
  await page.setViewportSize({ width: 1440, height: 960 });
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await page.keyboard.press('Escape');
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.getByRole('heading', { name: 'A clear view of your sales.', exact: true }).click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await trigger.click();
  await page.getByRole('group', { name: 'Available workspaces' }).getByRole('link', { name: /^ProductTrackr/ }).click();
  await expect(trigger).toContainText('ProductTrackr');
  await expect(page.getByRole('link', { name: 'All sales records', exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: 'ProductTrackr settings', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'ProductTrackr settings', exact: true })).toBeVisible();
  await trigger.click();
  await page.getByRole('group', { name: 'Available workspaces' }).getByRole('link', { name: /^SalesTrackr/ }).click();
  await expect(page.getByRole('heading', { name: 'A clear view of your sales.', exact: true })).toBeVisible();
  await expect(page.locator('.topbar,.breadcrumb')).toHaveCount(0);
  await expect(page.locator('footer').getByRole('button', { name: 'Refresh data', exact: true })).toBeAttached();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Backup and recovery', exact: true })).toHaveAttribute('aria-selected', 'true');
});

test('workspace switching honours the sales unsaved-changes warning', async ({ page }) => {
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill('Unsaved workspace switch');
  await page.getByRole('button', { name: 'Switch workspace', exact: true }).click();
  const manufacturing = page.getByRole('group', { name: 'Available workspaces' }).getByRole('link', { name: /^ProductTrackr/ });
  page.once('dialog', dialog => dialog.dismiss()); await manufacturing.click();
  await expect(page).toHaveURL(/\/accounts\/new$/);
  await expect(page.getByLabel('Company name *', { exact: true })).toHaveValue('Unsaved workspace switch');
  page.once('dialog', dialog => dialog.accept()); await manufacturing.click();
  await expect(page.getByRole('heading', { name: 'Every job. One clear next step.' })).toBeVisible();
});

test('manufacturing navigation and settings fit mobile and narrow desktop screens', async ({ page }) => {
  await page.goto('/manufacturing');
  await expect(page.getByRole('heading', { name: 'Every job. One clear next step.' })).toBeVisible();
  for (const width of [320, 390, 768, 1024]) {
    await page.setViewportSize({ width, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(page.getByRole('button', { name: 'Switch workspace', exact: true })).toHaveCount(1);
    await page.getByRole('button', { name: 'Switch workspace', exact: true }).click();
    const choices = page.getByRole('group', { name: 'Available workspaces' });
    const bounds = (await choices.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0); expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    await page.keyboard.press('Escape');
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
  await page.getByRole('navigation', { name: 'Manufacturing navigation' }).getByRole('link', { name: 'Products', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Products', exact: true })).toBeVisible();
  await expect(page.locator('.sidebar')).not.toHaveClass(/open/);
  await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
  await page.getByRole('link', { name: 'ProductTrackr settings', exact: true }).click();
  await page.getByRole('tab', { name: 'Data & recovery', exact: true }).click();
  await expect(page.getByRole('tabpanel')).toContainText('Local simulation only.');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Switch workspace', exact: true })).toHaveCount(1);
  await page.getByRole('button', { name: 'Switch workspace', exact: true }).click();
  await page.getByRole('group', { name: 'Available workspaces' }).getByRole('link', { name: /^SalesTrackr/ }).click();
  await expect(page.getByRole('heading', { name: 'A clear view of your sales.', exact: true })).toBeVisible();
});
