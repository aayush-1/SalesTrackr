import { expect, test, type Page } from '@playwright/test';

async function reviewCompanyMatches(page: Page) {
  await expect(page.getByText('Checking for existing accounts...', { exact: true })).toHaveCount(0);
  const review = page.getByLabel('I checked these matches; this is a different company.', { exact: true });
  if (await review.count()) await review.check();
}

test('record loads are bundled and confirmed saves avoid redundant refetches', async ({ page }) => {
  test.setTimeout(60000);
  const headers = { 'X-SalesTrackr-Request': '1' };
  const create = async (kind: string, data: object) => {
    const ticket = await (await page.request.post('/api/writes/prepare', {
      headers, data: { entityType: kind },
    })).json();
    const response = await page.request.post(`/api/${kind}s`, {
      headers, data: { ...ticket, baseRevision: null, data },
    });
    expect(response.ok()).toBe(true);
    return (await response.json()).revision;
  };
  const company = `Request count example ${Date.now()}`;
  const account = await create('account', { name: company, address: 'Request count city' });
  const contact = await create('contact', { accountId: account.entityId, name: 'Request count contact' });
  const row = await create('record', { accountId: account.entityId, contactId: contact.entityId, productNotes: 'Original requirement' });
  const gets: string[] = [];
  const attempts: object[] = [];
  const prepares: string[] = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path.startsWith('/api/')) gets.push(path);
    if (path === '/api/writes/prepare') prepares.push(path);
    if (path === `/api/records/${row.entityId}/product-notes`) attempts.push(request.postDataJSON());
  });
  const detailPath = `/api/records/${row.entityId}/detail`;
  const historyPath = `/api/entities/record/${row.entityId}/history`;
  await page.goto(`/records/${row.entityId}`);
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  await expect(page.getByText('Request count contact', { exact: true })).toBeVisible();
  await expect(page.locator('.history-item')).toHaveCount(1);
  await expect(page.locator('.loading')).toHaveCount(0);
  expect(gets.filter(path => path === detailPath)).toHaveLength(1);
  expect(gets.some(path => /^\/api\/entities\/(account|contact)\//.test(path))).toBe(false);
  gets.length = 0;
  await page.route(`**${detailPath}`, route => route.abort());
  await page.route('**/api/status', route => route.abort());
  await page.getByRole('button', { name: 'Edit product / requirement notes', exact: true }).click();
  await page.getByLabel('Product / requirement notes', { exact: true }).fill('Updated without refetching the sale.');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.note-block').first()).toContainText('Updated without refetching the sale.');
  await expect(page.locator('.history-item')).toHaveCount(2);
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  await expect(page.getByText('Connection check failed:', { exact: false })).toHaveCount(0);
  expect(gets.filter(path => path === detailPath || path === '/api/status')).toEqual([]);
  expect(gets.filter(path => path === historyPath)).toHaveLength(1);
  await page.unroute(`**${detailPath}`);
  await page.unroute('**/api/status');
  gets.length = 0;
  await page.getByRole('button', { name: 'Refresh data', exact: true }).click();
  await expect.poll(() => gets.filter(path => path === detailPath).length).toBe(1);
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  expect(gets.filter(path => path === '/api/status')).toHaveLength(1);

  let versions = 2;
  for (const patch of [
    { warning: 'Saved, but synchronization is pending.' },
    { complete: false },
    { revisionIsCurrent: false },
    { revisionIsCurrent: undefined },
    { revision: row },
  ]) {
    gets.length = 0;
    await page.route(`**/api/records/${row.entityId}/product-notes`, async route => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), ...patch } });
    });
    await page.getByRole('button', { name: 'Edit product / requirement notes', exact: true }).click();
    await page.getByLabel('Product / requirement notes', { exact: true }).fill(`Revalidated requirement ${versions}`);
    await page.getByRole('button', { name: 'Save changes', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.history-item')).toHaveCount(++versions);
    await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
    await expect.poll(() => gets.filter(path => path === detailPath).length).toBe(1);
    expect(gets.filter(path => path === '/api/status')).toHaveLength('revision' in patch ? 0 : 1);
    await page.unroute(`**/api/records/${row.entityId}/product-notes`);
  }
  gets.length = 0;
  let unconfirmed = true;
  await page.route(`**/api/records/${row.entityId}/product-notes`, async route => {
    const response = await route.fetch();
    if (unconfirmed) {
      unconfirmed = false;
      await route.fulfill({ status: 503, json: { error: { message: 'Synthetic unconfirmed save.', code: 'save_unknown' } } });
    } else {
      await route.fulfill({ response });
    }
  });
  await page.getByRole('button', { name: 'Edit product / requirement notes', exact: true }).click();
  await page.getByLabel('Product / requirement notes', { exact: true }).fill('Retry keeps one saved revision.');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('Synthetic unconfirmed save.');
  expect(gets.filter(path => path === detailPath || path === '/api/status')).toEqual([]);
  const sent = attempts.length;
  await page.getByLabel('Product / requirement notes', { exact: true }).fill('Cannot change an unconfirmed save.');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('The previous save is unconfirmed.');
  expect(attempts).toHaveLength(sent);
  await page.getByLabel('Product / requirement notes', { exact: true }).fill('Retry keeps one saved revision.');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.note-block').first()).toContainText('Retry keeps one saved revision.');
  await expect(page.locator('.history-item')).toHaveCount(++versions);
  expect(gets.filter(path => path === detailPath || path === '/api/status')).toEqual([]);
  expect(prepares).toEqual([]);
  expect(attempts.at(-1)).toEqual(attempts.at(-2));
});

for (const fallback of [false, true]) {
  test(`new accounts and contacts allocate IDs; edits retain safe retry IDs${fallback ? ' without browser UUID support' : ''}`, async ({ page }) => {
    test.setTimeout(60000);
    if (fallback) {
      await page.addInitScript(() => {
        Object.defineProperty(globalThis.crypto, 'randomUUID', { value: undefined });
      });
    }
    const prepares: { entityType: string; entityId: string | null }[] = [];
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/api/writes/prepare') prepares.push(request.postDataJSON());
    });
    const company = `Safe saves ${fallback} ${Date.now()}`;
    await page.goto('/accounts/new');
    await page.getByLabel('Company name *', { exact: true }).fill(company);
    await reviewCompanyMatches(page);
    await page.getByRole('button', { name: 'Create account', exact: true }).click();
    await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
    const accountId = new URL(page.url()).pathname.split('/').at(-1)!;
    await page.getByRole('button', { name: 'Edit account', exact: true }).click();
    await page.getByLabel('Account notes', { exact: true }).fill('Account edit remains confirmed.');
    await page.getByRole('button', { name: 'Save account', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByText('Account edit remains confirmed.', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Add contact', exact: true }).click();
    await page.getByLabel('Contact name *', { exact: true }).fill('Safe retry contact');
    await page.getByRole('button', { name: 'Save contact', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Edit Safe retry contact', exact: true })).toBeVisible();
    const contacts = await (await page.request.get(`/api/contacts?accountId=${accountId}`)).json();
    const contactId = contacts.items[0].entityId;
    const attempts: { writeId: string }[] = [];
    await page.route(`**/api/contacts/${contactId}`, async route => {
      attempts.push(route.request().postDataJSON());
      const response = await route.fetch();
      if (attempts.length === 1) {
        await route.fulfill({ status: 503, json: { error: { code: 'save_unknown', message: 'Synthetic unconfirmed contact save.' } } });
      } else {
        await route.fulfill({ response });
      }
    });
    await page.getByRole('button', { name: 'Edit Safe retry contact', exact: true }).click();
    await page.getByLabel('Designation', { exact: true }).fill('One confirmed contact edit');
    await page.getByRole('dialog').locator('form').evaluate((form: HTMLFormElement) => {
      form.requestSubmit();
      form.requestSubmit();
    });
    await expect(page.getByRole('dialog')).toContainText('Synthetic unconfirmed contact save.');
    expect(attempts).toHaveLength(1);
    await page.getByRole('button', { name: 'Save contact', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByText('One confirmed contact edit', { exact: true })).toBeVisible();
    expect(attempts).toHaveLength(2);
    expect(attempts[1]).toEqual(attempts[0]);
    expect(attempts[0].writeId).toMatch(/^[a-f0-9]{32}$/);
    const accountHistory = await (await page.request.get(`/api/entities/account/${accountId}/history`)).json();
    const contactHistory = await (await page.request.get(`/api/entities/contact/${contactId}/history`)).json();
    expect(accountHistory.total).toBe(2);
    expect(contactHistory.total).toBe(2);
    expect(prepares.filter(ticket => ticket.entityId === null).map(ticket => ticket.entityType)).toEqual(['account', 'contact']);
    expect(prepares).toHaveLength(fallback ? 4 : 2);
  });
}

test('sample workspace and mobile navigation', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'A clear view of your sales.' })).toBeVisible();
  await expect(page.locator('.stats')).toBeVisible();
  await expect(page.locator('.loading')).toHaveCount(0);
  await page.screenshot({ path: 'test-results/overview.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('navigation').getByRole('link', { name: 'Order in hand' }).click();
  await expect(page.getByRole('heading', { name: 'Order in hand' })).toBeVisible();
  await expect(page.getByLabel('Lead acquired date')).toHaveValue('all');
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/mobile.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('create account and record, progress, pay, close, filter and retain history', async ({ page }) => {
  test.setTimeout(60000);
  const unique = Date.now();
  const account = `E2E Company ${unique}`;
  await page.goto('/records/new');
  const me = await (await page.request.get('/api/me')).json();
  const previousMonth = new Date(`${me.today}T00:00:00Z`);
  previousMonth.setUTCMonth(previousMonth.getUTCMonth() - 1, 10);
  const acquiredOn = previousMonth.toISOString().slice(0, 10);
  await page.getByRole('button', { name: 'Create a new account' }).click();
  await page.getByLabel('Company name *', { exact: true }).fill(account);
  await page.getByLabel('Location / address').fill('Pune, Maharashtra');
  await reviewCompanyMatches(page);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByLabel('Account *', { exact: true })).not.toHaveValue('');
  await expect(page.getByLabel('Enquiry title *')).toHaveCount(0);
  await page.getByLabel('Lead acquired date', { exact: true }).fill(acquiredOn);
  await page.getByLabel('Product / requirement notes').fill('Roller and belts. Several products on one enquiry.');
  await page.getByRole('button', { name: 'Create sales record' }).click();
  await expect(page.getByRole('heading', { name: account, exact: true })).toBeVisible();
  await expect(page.locator('.page-header p')).toHaveText('Pune, Maharashtra');
  await expect(page.locator('.stages .reached')).toHaveCount(1);
  const acquired = await page.locator('.stages li').first().innerText();
  await expect(page.locator('.stages li strong')).toHaveText(['New lead', 'Follow-up', 'Order in hand', 'Dispatched', 'Delivered', 'Closed']);
  await expect(page.getByRole('button', { name: 'Received', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Feedback received', exact: true })).toBeDisabled();
  await page.getByLabel('Payment details', { exact: true }).fill('Draft bank reference');
  for (const next of ['Follow-up', 'Order in hand']) {
    await page.getByRole('button', { name: next, exact: true }).click();
    if (next === 'Follow-up') {
      await expect(page.getByLabel('Quotation number (optional)')).not.toHaveAttribute('required', '');
      await page.getByLabel('Quotation number (optional)').fill(`Q-${unique}`);
    }
    await expect(page.getByLabel('Order amount (INR) *')).toHaveCount(0);
    await page.getByRole('button', { name: 'Mark stage reached' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.stages .current strong')).toHaveText(next);
    await expect(page.getByRole('button', { name: 'Feedback received', exact: true })).toBeDisabled();
    await expect(page.getByLabel('Payment details', { exact: true })).toHaveValue('Draft bank reference');
  }
  const recordUrl = page.url();
  await expect(page.getByText(`Q-${unique}`, { exact: true }).first()).toBeVisible();
  await page.getByLabel('Payment details', { exact: true }).fill('Awaiting bank reference confirmation.');
  await page.getByRole('button', { name: 'Save details', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save details', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Payment details', { exact: true })).toHaveValue('Awaiting bank reference confirmation.');
  await expect(page.locator('.payment-panel .badge')).toHaveText('Pending');
  await expect(page.locator('.payment-panel')).not.toContainText(/amount|INR|₹|part-paid/i);
  await expect(page.locator('.stages .reached')).toHaveCount(3);
  await page.goto('/view/pending');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.getByRole('columnheader', { name: /Payment|Pending|Amount/ })).toHaveCount(0);
  await page.goto(recordUrl);
  await expect(page.getByRole('button', { name: 'Dispatched', exact: true })).toBeEnabled();
  for (const next of ['Dispatched', 'Delivered']) {
    await page.getByRole('button', { name: next, exact: true }).click();
    await page.getByRole('button', { name: 'Mark stage reached' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.stages .current strong')).toHaveText(next);
  }
  await expect(page.getByRole('button', { name: 'Closed', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Feedback received', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Received', exact: true }).click();
  await expect(page.locator('.stages .current strong')).toHaveText('Closed');
  await expect(page.locator('.payment-panel .badge')).toHaveText('Received');
  await expect(page.getByRole('button', { name: 'Received', exact: true })).toBeDisabled();
  expect(await page.locator('.stages li').first().innerText()).toBe(acquired.replace('\nCURRENT', ''));
  await expect(page.locator('.stages .reached')).toHaveCount(6);
  await expect(page.getByRole('button', { name: 'Discard record', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Feedback received', exact: true })).toBeEnabled();
  await expect(page.locator('.feedback-panel .badge')).toHaveText('Pending');
  const paymentBox = await page.locator('.payment-panel').boundingBox();
  const feedbackBox = await page.locator('.feedback-panel').boundingBox();
  expect(feedbackBox!.y).toBeGreaterThanOrEqual(paymentBox!.y + paymentBox!.height);
  await page.screenshot({ path: 'test-results/record.png', fullPage: true });
  await page.goto('/view/feedback-pending');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.locator('tbody .badge')).toHaveText('Closed');
  await expect(page.getByLabel('Current sales stage', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: 'test-results/feedback-pending.png', fullPage: true });
  await page.getByRole('link', { name: account, exact: true }).click();
  await page.getByLabel('Payment details', { exact: true }).fill('Payment draft survives feedback confirmation.');
  await page.getByRole('button', { name: 'Feedback received', exact: true }).click();
  await expect(page.locator('.feedback-panel .badge')).toHaveText('Received');
  await expect(page.getByRole('button', { name: 'Feedback received', exact: true })).toBeDisabled();
  await expect(page.locator('.feedback-panel')).toContainText('Received on');
  await expect(page.getByLabel('Payment details', { exact: true })).toHaveValue('Payment draft survives feedback confirmation.');
  await page.getByRole('button', { name: 'Save details', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save details', exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.locator('.feedback-panel .badge')).toHaveText('Received');
  await expect(page.locator('.stages .current strong')).toHaveText('Closed');
  await expect(page.locator('.payment-panel .badge')).toHaveText('Received');
  await page.screenshot({ path: 'test-results/feedback-received.png', fullPage: true });
  await page.goto('/view/feedback-pending');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await expect(page.getByRole('heading', { name: 'Nothing in this view' })).toBeVisible();
  await page.goto(recordUrl);
  await page.getByRole('button', { name: 'Undo feedback received', exact: true }).click();
  await expect(page.locator('.feedback-panel .badge')).toHaveText('Pending');
  await expect(page.getByRole('button', { name: 'Feedback received', exact: true })).toBeEnabled();
  await page.goto('/view/feedback-pending');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await page.goto(recordUrl);
  await page.getByRole('button', { name: 'Feedback received', exact: true }).click();
  await expect(page.locator('.feedback-panel .badge')).toHaveText('Received');
  await page.getByRole('link', { name: account, exact: true }).click();
  await page.getByRole('link', { name: 'New sales record', exact: true }).click();
  await page.getByLabel('Product / requirement notes').fill('A separate enquiry acquired today.');
  await page.getByRole('button', { name: 'Create sales record', exact: true }).click();
  await expect(page.getByRole('heading', { name: account, exact: true })).toBeVisible();

  await page.goto('/records');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await expect(page.locator('tbody tr')).toHaveCount(2);
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('day');
  await expect(page.getByRole('heading', { name: 'Choose a lead acquisition date' })).toBeVisible();
  await page.getByLabel('Acquired on', { exact: true }).fill(acquiredOn);
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.locator('tbody .badge').first()).toHaveText('Closed');
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('month');
  await page.getByLabel('Acquired month', { exact: true }).fill(acquiredOn.slice(0, 7));
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.locator('tbody .badge').first()).toHaveText('Closed');
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('today');
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.locator('tbody .badge').first()).toHaveText('New lead');

  await page.goto('/view/dispatched');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('today');
  await expect(page.getByRole('heading', { name: 'Nothing in this view' })).toBeVisible();
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('month');
  await page.getByLabel('Acquired month', { exact: true }).fill(acquiredOn.slice(0, 7));
  await expect(page.getByRole('heading', { name: 'Nothing in this view' })).toBeVisible();
  await page.goto('/view/delivered');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await expect(page.getByRole('heading', { name: 'Nothing in this view' })).toBeVisible();
  await page.goto('/view/closed');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('month');
  await page.getByLabel('Acquired month', { exact: true }).fill(acquiredOn.slice(0, 7));
  await expect(page.getByRole('link', { name: account, exact: true })).toBeVisible();
  await expect(page.locator('tbody tr').filter({ hasText: account }).locator('.cell-sub')).toHaveText('Pune, Maharashtra');
  await page.goto('/view/orders');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await expect(page.getByRole('heading', { name: 'Nothing in this view' })).toBeVisible();
  await page.goto('/view/pending');
  await page.getByLabel('Search', { exact: true }).fill(account);
  await expect(page.getByRole('heading', { name: 'Nothing in this view' })).toBeVisible();
});

test('move directly to follow-up without a quotation and add or clear it later', async ({ page }) => {
  await page.goto('/records/new');
  const accounts = page.getByLabel('Account *', { exact: true });
  await expect(accounts.locator('option')).not.toHaveCount(1);
  await accounts.selectOption({ index: 1 });
  await page.getByLabel('Product / requirement notes').fill('Optional quotation browser example');
  await page.getByRole('button', { name: 'Create sales record', exact: true }).click();
  await expect(page.locator('.stages .current strong')).toHaveText('New lead');
  await page.getByRole('button', { name: 'Follow-up', exact: true }).click();
  await expect(page.getByLabel('Quotation number (optional)')).toHaveValue('');
  await page.getByRole('button', { name: 'Mark stage reached' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.stages .current strong')).toHaveText('Follow-up');
  const dates = await page.locator('.stages').innerText();
  for (const reference of ['OPTIONAL-Q-123', '']) {
    await page.getByRole('button', { name: 'Edit details', exact: true }).click();
    await page.getByLabel('Quotation number (optional)').fill(reference);
    await page.getByRole('button', { name: 'Save changes', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.details-grid div').filter({ has: page.locator('dt', { hasText: 'Quotation number' }) }).locator('dd'))
      .toHaveText(reference || 'Not added');
    expect(await page.locator('.stages').innerText()).toBe(dates);
  }
});

test('follow-up and notes do not advance a stage', async ({ page }) => {
  await page.goto('/view/leads');
  await page.locator('.record-title').first().click();
  const stagesBefore = await page.locator('.stages').innerText();
  await page.getByRole('button', { name: 'Log follow-up' }).click();
  await page.getByLabel('Follow-up note *').fill('Customer requested a call tomorrow.');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('Customer requested a call tomorrow.', { exact: true }).first()).toBeVisible();
  expect(await page.locator('.stages').innerText()).toBe(stagesBefore);
  await page.getByRole('button', { name: 'Edit details' }).click();
  await expect(page.getByLabel('Title *', { exact: true })).toHaveCount(0);
  const contact = await page.getByLabel('Contact', { exact: true }).inputValue();
  const contactName = await page.getByLabel('Contact', { exact: true }).locator('option:checked').innerText();
  await page.getByLabel('General notes', { exact: true }).fill('Updated notes without changing dates.');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(await page.locator('.stages').innerText()).toBe(stagesBefore);
  if (contact) await expect(page.getByText(contactName, { exact: true })).toBeVisible();
});

test('sales filters use acquisition dates and only relevant current stages', async ({ page }) => {
  const stageOptions = page.getByLabel('Current sales stage', { exact: true }).locator('option');
  for (const [view, stage] of [
    ['leads', 'new_lead'], ['followups', 'follow_up'], ['orders', 'order_received'],
    ['dispatched', 'dispatched'], ['delivered', 'delivered'], ['closed', 'closed'],
  ]) {
    await page.goto(`/view/${view}`);
    await expect(page.getByLabel('Current sales stage', { exact: true })).toHaveCount(0);
    await expect(page.getByLabel('Filter by date')).toHaveCount(0);
    await expect(page.locator(`tbody .stage-${stage}`).first()).toBeVisible();
    await expect(page.locator(`tbody .badge:not(.stage-${stage})`)).toHaveCount(0);
    await expect(page.getByRole('columnheader', { name: 'Lead acquired' })).toHaveAttribute('aria-sort', 'descending');
    const dates = await page.locator('tbody time').evaluateAll(elements => elements.map(el => el.getAttribute('datetime') || ''));
    expect(dates).toEqual([...dates].sort().reverse());
  }
  const scopedViews = [
    ['pending', ['All stages in this view', 'Order in hand', 'Dispatched', 'Delivered']],
    ['paid', ['All stages in this view', 'Order in hand', 'Dispatched', 'Delivered', 'Closed']],
  ] as const;
  for (const [view, options] of scopedViews) {
    await page.goto(`/view/${view}`);
    await expect(stageOptions).toHaveText([...options]);
  }
  await page.goto('/view/followups');
  await expect(page.getByRole('heading', { name: 'Follow-ups', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation').getByRole('link')).toHaveText([
    'Overview', 'All sales records', 'Accounts', 'New leads', 'Follow-ups', 'Order in hand',
    'Dispatched', 'Delivered', 'Payment pending', 'Payment received', 'Closed', 'Feedback pending', 'Discarded',
  ]);
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('today');
  await expect(page.getByLabel('Filter by date')).toHaveCount(0);
  await expect(page.getByLabel('Date refers to')).toHaveCount(0);
  await expect(page.locator('.table-caption')).toContainText('Filtered by lead acquired date');

  await page.goto('/records');
  await page.getByLabel('Current sales stage', { exact: true }).selectOption('new_lead');
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('today');
  await expect(page.getByLabel('Current sales stage', { exact: true })).toHaveValue('new_lead');
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('all');
  await expect(page.getByLabel('Filter by date')).toHaveCount(0);
  await expect(stageOptions).toHaveText(['All stages in this view', 'New lead', 'Follow-up', 'Order in hand', 'Dispatched', 'Delivered', 'Closed']);
});

test('company location changes appear on every linked sale without an enquiry title', async ({ page }) => {
  const company = `Location example ${Date.now()}`;
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *', { exact: true }).fill(company);
  await page.getByLabel('Location / address').fill('Old location');
  await reviewCompanyMatches(page);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  const accountUrl = page.url();
  let recordUrl = '';
  for (const notes of ['Rollers', 'Belts']) {
    await page.goto(accountUrl);
    await page.getByRole('link', { name: 'New sales record', exact: true }).click();
    await expect(page.getByLabel('Account *', { exact: true }).locator('option:checked')).toHaveText(`${company} - Old location`);
    await expect(page.getByLabel('Enquiry title *')).toHaveCount(0);
    await page.getByLabel('Product / requirement notes').fill(notes);
    await page.getByRole('button', { name: 'Create sales record', exact: true }).click();
    await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
    await expect(page.locator('.page-header p')).toHaveText('Old location');
    recordUrl = page.url();
  }
  await page.goto(accountUrl);
  await page.getByRole('button', { name: 'Edit account', exact: true }).click();
  await page.getByLabel('Company name *').fill(`${company} renamed`);
  await page.getByLabel('Location / address').fill('Updated location');
  await page.getByRole('button', { name: 'Save account', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('tbody .record-title')).toHaveText([`${company} renamed`, `${company} renamed`]);
  await expect(page.locator('tbody .cell-sub')).toHaveText(['Updated location', 'Updated location']);
  expect((await page.locator('tbody .record-summary').allTextContents()).sort()).toEqual(['Belts', 'Rollers']);
  await page.getByLabel('Search', { exact: true }).fill('Updated location');
  await expect(page.locator('tbody tr')).toHaveCount(2);
  await page.getByLabel('Search', { exact: true }).fill('Belts');
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await page.goto(recordUrl);
  await expect(page.getByRole('heading', { name: `${company} renamed`, exact: true })).toBeVisible();
  await expect(page.locator('.page-header p')).toHaveText('Updated location');
  await page.goto('/accounts');
  await page.getByRole('textbox', { name: 'Search accounts' }).fill(company);
  const accountTable = page.getByRole('table', { name: 'Accounts', exact: true });
  await expect(accountTable.locator('tbody tr')).toHaveCount(1);
  await expect(accountTable).toContainText('Updated location');
  await expect(page.locator('.account-card')).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await accountTable.getByRole('link', { name: `${company} renamed`, exact: true }).click();
  await expect(page.getByRole('heading', { name: `${company} renamed`, exact: true })).toBeVisible();
});

test('unsaved form warns before leaving', async ({ page }) => {
  await page.goto('/accounts/new');
  await page.getByLabel('Company name *').fill('Unsaved company');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('link', { name: 'All accounts', exact: true }).click();
  await expect(page.getByLabel('Company name *')).toHaveValue('Unsaved company');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('link', { name: 'All accounts', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your accounts.' })).toBeVisible();
});

test('acquisition sorting, focused product editing and discarding retain the saved journey', async ({ page }) => {
  test.setTimeout(90000);
  const company = `Discard example ${Date.now()}`;
  await page.goto('/accounts/new');
  const me = await (await page.request.get('/api/me')).json();
  await page.getByLabel('Company name *', { exact: true }).fill(company);
  await reviewCompanyMatches(page);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  const accountUrl = page.url();
  const recordUrls: string[] = [];
  for (const [daysAgo, notes] of [[20, 'Older requirement'], [2, 'Newest requirement'], [10, 'Middle requirement']] as const) {
    const acquired = new Date(`${me.today}T00:00:00Z`);
    acquired.setUTCDate(acquired.getUTCDate() - daysAgo);
    await page.goto(accountUrl);
    await page.getByRole('link', { name: 'New sales record', exact: true }).click();
    await expect(page.getByLabel('Account *', { exact: true }).locator('option:checked')).toHaveText(company);
    await page.getByLabel('Lead acquired date', { exact: true }).fill(acquired.toISOString().slice(0, 10));
    await page.getByLabel('Product / requirement notes', { exact: true }).fill(notes);
    await page.getByLabel('General notes', { exact: true }).fill('Keep these general notes.');
    await page.getByRole('button', { name: 'Create sales record', exact: true }).click();
    await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
    recordUrls.push(page.url());
  }
  await page.goto('/records');
  await page.getByLabel('Search', { exact: true }).fill(company);
  await expect(page.locator('tbody .record-summary')).toHaveText(['Newest requirement', 'Middle requirement', 'Older requirement']);
  await expect(page.getByRole('columnheader', { name: 'Lead acquired' })).toHaveAttribute('aria-sort', 'descending');
  await expect(page.locator('.table-caption')).toContainText('newest first');

  await page.goto(recordUrls[0]);
  await expect(page.getByRole('button', { name: 'Edit details', exact: true })).toBeVisible();
  const id = recordUrls[0].split('/').pop();
  const original = (await (await page.request.get(`/api/entities/record/${id}`)).json()).revision;
  await page.getByLabel('Payment details', { exact: true }).fill('Unsaved payment draft');
  await page.getByRole('button', { name: 'Edit product / requirement notes', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('textbox')).toHaveCount(1);
  await page.getByLabel('Product / requirement notes', { exact: true }).fill('Updated older requirement');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const productNotes = page.locator('.note-block').filter({
    has: page.getByRole('heading', { name: 'Product / requirement notes', exact: true }),
  });
  await expect(productNotes.getByText('Updated older requirement', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Payment details', { exact: true })).toHaveValue('Unsaved payment draft');
  const edited = (await (await page.request.get(`/api/entities/record/${id}`)).json()).revision;
  expect(edited.data).toEqual({ ...original.data, productNotes: 'Updated older requirement' });
  await page.getByRole('button', { name: 'Edit details', exact: true }).click();
  await expect(page.getByLabel('General notes', { exact: true })).toHaveValue('Keep these general notes.');
  await expect(page.getByLabel('Product notes', { exact: true })).toHaveValue('Updated older requirement');
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
  for (const stage of ['Follow-up', 'Order in hand']) {
    await page.getByRole('button', { name: stage, exact: true }).click();
    await page.getByRole('button', { name: 'Mark stage reached', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.stages .current strong')).toHaveText(stage);
  }
  const before = (await (await page.request.get(`/api/entities/record/${id}`)).json()).revision;
  const countsBefore = (await (await page.request.get('/api/dashboard')).json()).counts;
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: 'Discard record', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByLabel('Payment details', { exact: true })).toHaveValue('Unsaved payment draft');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Discard record', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Discard record', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByLabel('Discard remark *', { exact: true }).fill('Customer withdrew the enquiry.');
  await page.getByRole('dialog').getByRole('button', { name: 'Discard record', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Discard details' })).toContainText('Customer withdrew the enquiry.');
  await expect(page.locator('.timeline-panel .badge')).toHaveText('Discarded');
  await expect(page.locator('.stages .current-label')).toHaveText('LAST STAGE');
  for (const name of ['Edit details', 'Edit product / requirement notes', 'Discard record', 'Received', 'Log follow-up', 'Dispatched']) {
    await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0);
  }
  const discarded = (await (await page.request.get(`/api/entities/record/${id}`)).json()).revision;
  expect(discarded.data).toEqual({ ...before.data, discarded: true, discardNote: 'Customer withdrew the enquiry.', discardedOn: me.today });
  const countsAfter = (await (await page.request.get('/api/dashboard')).json()).counts;
  expect(countsAfter.orders).toBe(countsBefore.orders - 1);
  expect(countsAfter.pending).toBe(countsBefore.pending - 1);
  expect(countsAfter.discarded).toBe(countsBefore.discarded + 1);
  expect(countsAfter.all).toBe(countsBefore.all);
  const history = (await (await page.request.get(`/api/entities/record/${id}/history`)).json()).items;
  expect(history[1]).toEqual(before);
  await page.reload();
  await expect(page.getByRole('region', { name: 'Discard details' })).toBeVisible();
  await expect(page.getByRole('heading', { name: company, exact: true })).toBeVisible();
  await expect(page.locator('.loading')).toHaveCount(0);
  await page.screenshot({ path: 'test-results/discarded-record.png', fullPage: true });
  await page.goto('/view/discarded');
  await page.getByLabel('Search', { exact: true }).fill(company);
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.getByRole('columnheader', { name: 'Discard remark', exact: true })).toBeVisible();
  await expect(page.locator('tbody')).toContainText('Customer withdrew the enquiry.');
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('today');
  await expect(page.getByRole('heading', { name: 'Nothing in this view' })).toBeVisible();
  await page.getByLabel('Lead acquired date', { exact: true }).selectOption('all');
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  for (const view of ['orders', 'pending']) {
    await page.goto(`/view/${view}`);
    await page.getByLabel('Search', { exact: true }).fill(company);
    await expect(page.getByRole('heading', { name: 'Nothing in this view' })).toBeVisible();
  }
  await page.goto('/records');
  await page.getByLabel('Search', { exact: true }).fill(company);
  await expect(page.locator('tbody .record-summary')).toHaveText(['Newest requirement', 'Middle requirement', 'Updated older requirement']);
  await expect(page.locator('tbody .discarded')).toHaveCount(1);
});
