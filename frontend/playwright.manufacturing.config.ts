import { defineConfig } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = process.platform === 'win32' ? '.venv\\Scripts\\python.exe' : '.venv/bin/python';
const workspace = `producttrackr-browser-${process.pid}-${Date.now()}`;
export default defineConfig({
  testDir: './tests', testMatch: /(?:producttrackr|manufacturing|manufacturing-records|manufacturing-recovery|manufacturing-cleanup|manufacturing-spreadsheet)\.spec\.ts/, workers: 1, timeout: 120_000,
  outputDir: path.join(root, '.local-data', 'browser-manufacturing-results', workspace),
  use: { baseURL: 'http://127.0.0.1:8014', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure' },
  webServer: {
    command: `${python} -m uvicorn backend.app.main:create_app --factory --host 127.0.0.1 --port 8014`,
    cwd: root, url: 'http://127.0.0.1:8014/healthz', reuseExistingServer: false,
    env: {
      APP_MODE: 'demo', STORAGE_BACKEND: 'drive', DEMO_SEED: 'false',
      DEMO_DATA_DIR: path.join('.local-data', 'browser-manufacturing', workspace),
      PRODUCTTRACKR_ENABLED: 'true', PRODUCTTRACKR_FIRESTORE_PROJECT_ID: 'demo-producttrackr-browser',
      PRODUCTTRACKR_WORKSPACE_ID: workspace, FIRESTORE_EMULATOR_HOST: '127.0.0.1:8085',
      PUBLIC_ORIGIN: 'http://127.0.0.1:8014',
    },
  },
});
