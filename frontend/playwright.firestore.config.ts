import { defineConfig } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = process.platform === 'win32' ? '.venv\\Scripts\\python.exe' : '.venv/bin/python';
const workspace = `browser-e2e-${process.pid}-${Date.now()}`;
export default defineConfig({
  testDir: './tests',
  testMatch: 'firestore-backup.spec.ts',
  outputDir: path.join(root, '.local-data', 'browser-firestore-results', workspace),
  workers: 1,
  timeout: 120_000,
  use: { baseURL: 'http://127.0.0.1:8013', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure' },
  webServer: {
    command: `${python} -m uvicorn backend.app.main:create_app --factory --host 127.0.0.1 --port 8013`,
    cwd: root,
    url: 'http://127.0.0.1:8013/healthz',
    reuseExistingServer: false,
    env: {
      APP_MODE: 'demo',
      STORAGE_BACKEND: 'firestore',
      FIRESTORE_PROJECT_ID: 'demo-salestrackr-browser',
      FIRESTORE_WORKSPACE_ID: workspace,
      FIRESTORE_EMULATOR_HOST: '127.0.0.1:8085',
      DEMO_SEED: 'false',
      DEMO_DATA_DIR: path.join('.local-data', 'browser-firestore', workspace),
      PUBLIC_ORIGIN: 'http://127.0.0.1:8013',
    },
  },
});
