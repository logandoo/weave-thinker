// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  // 2026-11-04 E2E 清债波：269 条历史 ad-hoc spec 归档到 e2e/_archive/（保留 git
  // 历史）。默认只收集 13 条门禁套件（scripts/e2e_gate.sh）；
  // E2E_INCLUDE_ARCHIVE=1 时放开，可单跑归档 spec：
  //   E2E_INCLUDE_ARCHIVE=1 npx playwright test e2e/_archive/xxx.spec.ts
  testIgnore: process.env.E2E_INCLUDE_ARCHIVE ? [] : ['**/_archive/**'],
  globalSetup: './e2e/global-setup.ts',
  timeout: 300000,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:8159',
    headless: true,
    ignoreHTTPSErrors: true,
    actionTimeout: 15000,
    navigationTimeout: 30000,
    viewport: { width: 1440, height: 900 },
  },
  retries: 1,
});