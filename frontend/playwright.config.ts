import { defineConfig } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = process.platform === 'win32' ? '.venv\\Scripts\\python.exe' : '.venv/bin/python';
export default defineConfig({
  testDir: './tests',
  testIgnore: /(?:firestore-backup|record-cleanup|producttrackr|manufacturing-records|manufacturing-recovery|manufacturing-cleanup|manufacturing-spreadsheet|machine-sales|inventory)\.spec\.ts/,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8011', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure' },
  webServer: {
    command: `${python} -m uvicorn backend.app.main:create_app --factory --host 127.0.0.1 --port 8011`,
    cwd: root,
    url: 'http://127.0.0.1:8011/healthz',
    reuseExistingServer: false,
    env: { APP_MODE: 'demo', STORAGE_BACKEND: 'drive', DEMO_DATA_DIR: '.local-data/e2e', PUBLIC_ORIGIN: 'http://127.0.0.1:8011' },
  },
});
