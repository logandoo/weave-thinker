// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from '@playwright/test';

// 生产 8158 测试配置：所有测试只对 project_build.sh 产物（backend/static，
// https://127.0.0.1:8158）运行。用户约束（2026-08-24）：禁用 8159 端口测试，
// 8158 被旧进程占用时用 scripts/stop.sh + start.sh 重启。
export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  timeout: 300000,
  use: {
    // 默认 127.0.0.1:8158（canonical）；env 覆盖仅用于沙箱代理异常时直指后端进程
    baseURL: process.env.W11_BASE_URL || 'https://127.0.0.1:8158',
    headless: true,
    ignoreHTTPSErrors: true,
    actionTimeout: 15000,
    navigationTimeout: 30000,
    viewport: { width: 1440, height: 900 },
  },
  retries: 1,
});
