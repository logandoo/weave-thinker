// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { test, expect, request } from '@playwright/test';

// Helper: cleanup all test conversations using Playwright's request context (handles self-signed certs)
async function cleanupConversations(baseURL: string) {
  const ctx = await request.newContext({ baseURL, ignoreHTTPSErrors: true, timeout: 120000 });
  try {
    const loginRes = await ctx.post('/api/auth/login', {
      data: { username: 'test', password: '123456' },
    });
    const { access_token } = await loginRes.json();
    const headers = { Authorization: `Bearer ${access_token}` };
    const listRes = await ctx.get('/api/conversations', { headers });
    const conversations = await listRes.json();
    if (Array.isArray(conversations) && conversations.length > 0) {
      const ids = conversations.map((c: any) => c.id);
      await ctx.post('/api/conversations/bulk-delete', {
        headers,
        data: { conversation_ids: ids },
      });
    }
  } finally {
    await ctx.dispose();
  }
}

test.describe('Weave Thinker E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test.afterAll(async ({ }, testInfo) => {
    const baseURL = testInfo.project.use.baseURL || 'https://localhost:8158';
    try {
      await cleanupConversations(baseURL);
    } catch (e) {
      console.warn('Cleanup failed:', e);
    }
  });

  test('login page loads correctly', async ({ page }) => {
    await expect(page.locator('#username')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('#password')).toBeVisible();
    await expect(page.locator('.login-btn')).toBeVisible();
    await expect(page.locator('text=Weave Thinker')).toBeVisible();
  });

  test('can login with test credentials', async ({ page }) => {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');
    
    await expect(page.locator('.new-chat-btn')).toBeVisible({ timeout: 20000 });
  });

  test('can send a message and receive streaming response', async ({ page }) => {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');
    
    await expect(page.locator('.new-chat-btn')).toBeVisible({ timeout: 20000 });
    
    await page.click('.new-chat-btn');
    await page.waitForTimeout(1000);
    
    const input = page.locator('.chat-input[contenteditable]');
    await expect(input).toBeVisible({ timeout: 5000 });
    
    const prompt = '只回复OK两个字母';
    await input.fill(prompt);
    const sendBtn = page.locator('button.send-btn');
    await expect(sendBtn).toBeEnabled();
    await sendBtn.click();
    
    await expect.poll(async () => {
      return (await page.locator('.message-list').textContent()) || '';
    }, { timeout: 30000 }).toContain(prompt);

    const userMessage = page.locator('.message-bubble.user').last();
    await expect(userMessage).toContainText(prompt, { timeout: 30000 });
    
    const assistantContent = page.locator('.streaming-message .text, .message-bubble.assistant .text').last();
    await expect(assistantContent).toBeVisible({ timeout: 120000 });
    await expect.poll(async () => {
      return ((await assistantContent.textContent()) || '').trim();
    }, { timeout: 120000 }).not.toBe('');
  });

  test('auto web search triggers for relevant queries', async ({ page }) => {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');

    await expect(page.locator('.new-chat-btn')).toBeVisible({ timeout: 20000 });

    await page.click('.new-chat-btn');
    await page.waitForTimeout(1000);

    const input = page.locator('.chat-input[contenteditable]');
    await input.fill('2024年诺贝尔物理学奖得主是谁');
    await page.locator('button.send-btn').click();

    // Search results card should appear during streaming or in final message
    await expect(page.locator('.search-results-card').first()).toBeVisible({ timeout: 120000 });
  });

  test('can stop a streaming response', async ({ page }) => {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');

    await expect(page.locator('.new-chat-btn')).toBeVisible({ timeout: 20000 });

    await page.click('.new-chat-btn');
    await page.waitForTimeout(1000);

    const input = page.locator('.chat-input[contenteditable]');
    const sendButton = page.locator('button.send-btn');
    await input.fill('请从 1 数到 200，并且每一项都写成完整句子，不要使用代码块。');
    await sendButton.click();

    await expect(sendButton).toHaveClass(/stop-mode/, { timeout: 10000 });

    // Model may spend time in reasoning before emitting text — accept either.
    // F1-1 part-protocol renders live text as .timeline-text inside
    // .streaming-message (the legacy .text branch only renders when no
    // timeline items exist); poll both.
    await expect.poll(async () => {
      const textEl = page.locator('.streaming-message .timeline-text, .streaming-message .text').last();
      const text = (await textEl.isVisible().catch(() => false)) ? ((await textEl.textContent()) || '').trim() : '';
      const reasoningEl = page.locator('.streaming-message .reasoning-text, .streaming-message details').first();
      const reasoning = (await reasoningEl.isVisible().catch(() => false)) ? ((await reasoningEl.textContent()) || '').trim() : '';
      return text.length + reasoning.length;
    }, { timeout: 180000 }).toBeGreaterThan(20);

    await sendButton.click();

    await expect(sendButton).not.toHaveClass(/stop-mode/, { timeout: 20000 });
    await expect.poll(async () => {
      return await page.locator('.message-bubble.assistant').count();
    }, { timeout: 20000 }).toBeGreaterThan(0);
  });

  test('can regenerate the latest assistant message', async ({ page }) => {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');

    await expect(page.locator('.new-chat-btn')).toBeVisible({ timeout: 20000 });

    await page.click('.new-chat-btn');
    await page.waitForTimeout(1000);

    const input = page.locator('.chat-input[contenteditable]');
    await input.fill('请只回复 ok');
    await page.locator('button.send-btn').click();

    const assistantBubble = page.locator('.message-bubble.assistant').last();
    await expect(assistantBubble).toBeVisible({ timeout: 120000 });

    const regenerateButton = assistantBubble.locator('.message-action-btn[title="重新生成"]');
    await expect(regenerateButton).toBeVisible({ timeout: 120000 });
    await regenerateButton.click();

    await expect.poll(async () => {
      return await page.locator('.message-bubble.assistant').count();
    }, { timeout: 120000 }).toBe(1);

    await expect.poll(async () => {
      return ((await page.locator('.message-bubble.assistant .text').last().textContent()) || '').trim();
    }, { timeout: 120000 }).not.toBe('');
  });

  test('assistant selector works', async ({ page }) => {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');
    
    await expect(page.locator('.new-chat-btn')).toBeVisible({ timeout: 20000 });
    
    const dropdown = page.locator('.assistant-selector .dropdown-trigger');
    if (await dropdown.isVisible()) {
      await dropdown.click();
      await expect(page.locator('.dropdown-menu')).toBeVisible();
    }
  });

  test('no admin routes in sidebar', async ({ page }) => {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');
    
    await expect(page.locator('.new-chat-btn')).toBeVisible({ timeout: 20000 });
    
    const adminButtons = page.locator('.sidebar-footer').locator('text=用户管理, .sidebar-footer >> text=会话历史');
    const count = await adminButtons.count();
    expect(count).toBe(0);
  });
});