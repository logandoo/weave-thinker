#!/usr/bin/env node
// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Fail-closed guard for the vue-router #2704 backport.
 *
 * Why: npm install / npm ci runs `postinstall: patch-package`, but
 * patch-package exits 0 with "No patch files found" when the patches/
 * directory is absent (e.g. a Docker layer that installs before copying it).
 * That would silently ship the Edge focus-steal path. This check runs as
 * `prebuild` (so every `npm run build`, including Docker + CI, is gated) and
 * inside `npm run test:unit`.
 *
 * Exit 0 only when:
 *   - installed vue-router is the pinned 4.6.4 (the backport target), and
 *   - the tracked patch file exists, and
 *   - no runtime dist file still contains a visibilitychange listener, and
 *   - pagehide + popstate are still present.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PKG_DIR = path.join(ROOT, 'node_modules', 'vue-router');
const PKG_JSON = path.join(PKG_DIR, 'package.json');
const DIST = path.join(PKG_DIR, 'dist');
const PATCH = path.join(ROOT, 'patches', 'vue-router+4.6.4.patch');
const EXPECTED_VERSION = '4.6.4';

const failures = [];

if (!fs.existsSync(PKG_JSON)) {
  failures.push(`vue-router 未安装：${PKG_JSON}`);
} else {
  const version = JSON.parse(fs.readFileSync(PKG_JSON, 'utf8')).version;
  if (version !== EXPECTED_VERSION) {
    failures.push(
      `vue-router 版本为 ${version}，期望 ${EXPECTED_VERSION}。` +
        '若已升级到 >=5.3.0（上游已修复），请删除 patches/ + postinstall + 本检查并更新 AGENTS.md。',
    );
  }
}

if (!fs.existsSync(PATCH) || fs.statSync(PATCH).size < 100) {
  failures.push(`缺少可追踪补丁：${PATCH}`);
}

if (fs.existsSync(DIST)) {
  const runtime = fs
    .readdirSync(DIST)
    .filter((f) => /\.(?:mjs|cjs|js)$/.test(f));
  let pagehideHits = 0;
  let popstateHits = 0;
  for (const file of runtime) {
    const text = fs.readFileSync(path.join(DIST, file), 'utf8');
    if (text.includes('visibilitychange')) {
      failures.push(
        `未打补丁的入口：dist/${file} 仍含 visibilitychange（Edge focus-steal 风险）`,
      );
    }
    if (text.includes('pagehide')) pagehideHits += 1;
    if (text.includes('popstate')) popstateHits += 1;
  }
  if (runtime.length === 0) failures.push('vue-router/dist 为空');
  if (pagehideHits === 0) failures.push('dist 中未找到 pagehide（滚动保存被误删）');
  if (popstateHits === 0) failures.push('dist 中未找到 popstate（历史监听被误删）');
}

if (failures.length) {
  console.error('[vue-router-edge-backport] FAIL');
  for (const f of failures) console.error('  - ' + f);
  console.error(
    '修复：确认 frontend/patches/vue-router+4.6.4.patch 存在且 npm install 已运行；' +
      'Docker 构建需在 npm ci 之前 COPY frontend/patches。',
  );
  process.exit(1);
}

console.log(
  `[vue-router-edge-backport] PASS — vue-router ${EXPECTED_VERSION}，所有 dist 入口无 visibilitychange，pagehide+popstate 保留`,
);
