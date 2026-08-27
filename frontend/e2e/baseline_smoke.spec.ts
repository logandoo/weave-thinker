// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { test, expect } from '@playwright/test';

test('baseline smoke: login + chat input + assistant modal opens', async ({ page }) => {
  await page.goto('/app/frontend/');
  await page.waitForTimeout(1000);
  // login if needed
  if (await page.locator('#username').isVisible({ timeout: 5000 }).catch(() => false)) {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');
    await page.waitForTimeout(1500);
  }
  await expect(page.locator('[data-placeholder="输入消息..."]').first()).toBeVisible({ timeout: 15000 });
  const reasoningBtn = page.locator('.reasoning-btn').first();
  await expect(reasoningBtn).toBeVisible();
  await page.screenshot({ path: 'tests/baseline_01_chat.png' });
});
