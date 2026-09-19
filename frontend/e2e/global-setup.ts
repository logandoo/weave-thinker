// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { execFile } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function globalSetup() {
  const frontendDir = path.resolve(__dirname, '..');
  const projectRoot = path.resolve(frontendDir, '..');
  const python = path.join(projectRoot, '.venv', 'bin', 'python');
  const seeds = [
    path.join(projectRoot, 'tests', 'memory_ui_seed.py'),
    path.join(projectRoot, 'tests', 'seed_workspace_preview_fixtures.py'),
  ];
  for (const seed of seeds) {
    if (!fs.existsSync(seed) || !fs.existsSync(python)) {
      console.log(`[global-setup] seed script ${path.basename(seed)} / venv python absent, skip`);
      continue;
    }
    try {
      const { stdout } = await execFileAsync(python, [seed], { cwd: projectRoot });
      console.log(`[global-setup] seed ${path.basename(seed)}:`, stdout.trim());
    } catch (err) {
      console.warn(`[global-setup] seed ${path.basename(seed)} failed:`, err);
    }
  }
}

export default globalSetup;
