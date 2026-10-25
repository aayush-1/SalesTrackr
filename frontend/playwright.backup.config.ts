import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: 'backup-recovery.spec.ts',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8012', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure' },
  webServer: {
    command: 'npm exec vite preview -- --host 127.0.0.1 --port 8012 --strictPort',
    url: 'http://127.0.0.1:8012',
    reuseExistingServer: false,
  },
});
