<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# AGENTS.md — Weave Thinker

Open-source self-hosted personal AI agent platform. This file is the instruction
document for coding agents working in this repo.

## Architecture

- **Backend**: FastAPI on Uvicorn, async SQLAlchemy 2.0 + PostgreSQL, TOML config
- **Frontend**: Vue 3 + TypeScript + Vite + Pinia, built to `backend/static/`
- **Voice**: full-duplex WebSocket voice (`voice_service.py`), per-user dedicated voice assistant
- **Android**: optional WebView shell in `webview-app/`, loads your deployed frontend
- **No monorepo tooling** — pip for backend, npm for frontend
- **Docs**: README.md and `docs/ARCHITECTURE.md` are the current feature docs;
  `docs/SKINS.md` documents the skin token contract

## Key commands

```bash
# Python venv (project root)
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

# Frontend deps
cd frontend && npm install

# Production build (frontend → backend/static/)
./scripts/project_build.sh

# Production run (background, nohup + PID file; auto-detects SSL certs)
./scripts/start.sh
./scripts/stop.sh
./scripts/status.sh
./scripts/restart.sh

# Dev mode (optional):
#   backend:  cd backend && uvicorn main:app --host 0.0.0.0 --port 8158 --reload
#   frontend: cd frontend && npm run dev   (Vite 5173, proxies /api → https://localhost:8158)
# NOTE: with the Vite dev proxy the backend MUST serve TLS on 8158
#       (self-signed key.pem/cert.pem in backend/; see README "Development Mode").

# Android APK (requires Android SDK + JDK 11+; pass your server URL)
./scripts/apk_generate.sh https://your-server.example:8158
```

**No lint/typecheck/format scripts are wired.** `vue-tsc` is in devDependencies
but not bound to any npm script; there is no Python lint config. Do not invent
`npm run lint` / `npm run typecheck` commands.

## Backend specifics

- **Config**: `backend/config.toml` (infra: server/security/database/scheduler/
  workspace/browser + `[agent.*]` sub-sections) + `backend/config_model.toml`
  (all model config: LLM/ASR/voice/TTS/embedding/rerank/judge/subagent/memory/
  providers — merged OVER the main file at load; missing file falls back to main).
  **Both contain secrets — never commit real values.** Templates:
  `backend/config.toml.example`, `backend/config_model.toml.example`.
  All options documented in `backend/app/core/config.py`.
  Only `[server]`, `[database]` and `[security]` are required to start.
- **Dependencies**: `backend/requirements.txt` — no `pyproject.toml`
- **API docs**: `docs/API.md` — detailed reference (machine-generated field
  tables from the running backend's OpenAPI schema + curated behavior sections:
  SSE protocol, deathmatch state machine, memory pipeline, voice events)
- **Migrations**: no Alembic. Raw SQL in `backend/app/db/migrations.py` →
  `STARTUP_MIGRATIONS` list, run at startup before `create_all()`. Add columns by
  appending `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- **Code layout**: routes in `app/api/` (20+ modules), business logic in
  `app/services/`, Pydantic schemas in `app/schemas/`, auth deps in
  `app/core/deps.py`, config in `app/core/config.py`
- **Agent tools**: `app/tools/` (registered via `registry.register()`; includes
  `mcp_client.py`, `mixture_of_agents.py`, background tasks). System skills in
  `backend/skills/` (SKILL.md per subdirectory, loaded by `app/tools/skill_tools.py`)
- **Agent loop**: `agent_service.py` orchestrates, `agent_loop.py` runs tool
  iterations (`[agent.tool_loop] max_iterations`, default 50; parallel tool calls
  on by default)
- **Anti-hallucination stack**: `agentic_judge.py` (semantic router),
  `canary_marker.py`, send-audit with reject budgets, `citation_ledger.py`
  (`[N]` citation ledger with frontend badges), `pre_tool_gate.py`,
  `error_classifier.py`; markdown sanitization in `markdown_sanitizer.py`
- **Web search**: `search_service.py` — provider chain with fallbacks
  (`[web_search]` in config); results persisted to `web_search_results`
- **Skins**: `app/api/skins.py` — built-in catalog (`GET /api/skins`, public) +
  per-user UI prefs (`/api/users/me/preferences`) + per-user CSS upload
  (`/api/skins/upload`). Contract doc: `docs/SKINS.md`
- **Voice**: `app/api/voice.py` REST + `/api/voice/ws` full-duplex WebSocket
  (barge-in). Config: `[asr]`, `[voice]`
- **Background tasks**: `app/services/agent_worker.py` polls `agent_tasks` for
  `pending` tasks, runs AgentLoop independent of HTTP requests
- **Provider routing**: `app/services/provider_router.py` — multi-provider via
  `[providers]` in `config_model.toml`; assistant-level `custom_api_url/key/
  model_name` override (Qwen3.8(Local) type = assistant-configured vLLM-style
  deployment)
- **Scheduler**: `app/services/agent_scheduler.py` polls `scheduled_tasks`
- **Deathmatch (死磕) mode**: `app/services/deathmatch_service.py` — persistent
  multi-turn goal loop (grilling + goal loop). Config `[deathmatch]`
- **Export worker**: `app/services/export_worker.py` (PDF/note export tasks)
- **Runtime dirs** (auto-created, gitignored): `backend/audio_files/`,
  `backend/output_files/`, `backend/agent_memories/`, `backend/skins_custom/`,
  `user_workspaces/`

## Memory subsystem

Three layers coexist — do not conflate them:

1. **DB-backed user agent memory**: per-user summaries injected into the system
   prompt (`memory_service.py`; `UserAgentState`, `AgentMemory`, `AgentDream`).
   `generate_user_agent_memory()` runs on startup + scheduler tick (once per
   server-local calendar day, dedup key `date.today()` — not UTC — unless forced;
   **when the v2 memory runtime is enabled (`[memory] enabled=true`), the
   scheduler skips this legacy daily generation** and the v2 pipeline
   (`memory_scheduler.py`) takes over). Config: `[agent]` memory_* /
   `[agent.memory]`.
2. **File-backed `memory` tool**: episodic notes as Markdown at
   `{backend_root}/agent_memories/{user_id}/{AGENT,USER}.md` (`app/tools/memory.py`).
   Entries delimited by `\n§\n`; content scanned for prompt injection before write.
   The system target reads `backend/agent_memories/func.md` (read-only product /
   capability doc the agent quotes for self-introduction & feature questions) —
   shipped in this repo; fork/port it to your own product copy.
3. **v2 memory subsystem**: `app/services/memory_*_service.py` (~20 modules:
   concept/episodic/retrieval/consolidation/dreaming/cost-governance...),
   configured under `[memory.*]`. Layers 1–2 coexist and feed into it.

## Frontend specifics

- **All Composition API** + `<script setup lang="ts">`, path alias `@` → `src/`
- **Route base**: `/app/frontend/` (hardcoded in router + Vite `base`)
- **Auth**: JWT in `localStorage` key `chatllm_token`; axios interceptor
  attaches Bearer (`src/api/client.ts`)
- **SSE streaming**: `src/api/chat.ts` — `fetch` + `ReadableStream`, keyed by
  conversationId for parallel streams
- **Voice**: `src/api/voice.ts` — WebSocket `/api/voice/ws`, barge-in UI
- **Rendering**: mermaid / echarts / KaTeX / highlight.js
- **Skins**: CSS design tokens — 3 built-in skins (verdant-flat 青野平面 /
  ink-paper 墨韵纸间 / mono-brutal 黑白构成) × light/dark via
  `<html data-skin>` / `data-theme`. Registry `src/config/skins.ts` must stay in
  sync with the backend catalog. New components must use tokens, not hex colors.
- **Mobile**: CSS breakpoint `767px`; `window.WeaverNoteApp` JS bridge for the
  Android shell

## Tests

- **Frontend E2E**: Playwright, `frontend/e2e/` — foundational spec `chat.spec.ts`
  (`npx playwright test e2e/chat.spec.ts`). Config `playwright.prod8158.config.ts`
  targets the production build served at `https://127.0.0.1:8158` (the accepted
  verification target). All specs need the backend running with the
  `test`/`123456` dev account created and Postgres up.
- **npm/npx must run from `frontend/`** — the repo root is not a workspace (root
  `package.json` is a stale partial copy). A stray root `node_modules/` copy
  shadows npx resolution and intermittently yields a false
  `did not expect test.describe() to be called here` error; if that appears,
  remove the root `node_modules/` directory and rerun from `frontend/`.
- **No backend unit-test suite ships** in this release; verify backend changes
  with a runnable script (curl the deployed API / `scripts/restart.sh` + smoke
  requests) plus the frontend E2E, per CONTRIBUTING.md "测试要求".
- Evidence (screenshots/videos/DOM probes) belongs in a local `tests/` scratch
  directory — it is gitignored; do not commit run output.

## Database

PostgreSQL only. Models in `backend/app/db/database.py`. Key tables: `users`,
`assistants`, `conversation_groups`, `conversations`, `messages`, `notebooks`,
`notes`, `user_sessions`, `chat_sessions`, `user_agent_states`, `agent_memories`,
`agent_dreams`, `agent_tasks`, `scheduled_tasks`, `export_tasks`, `user_skills`,
`web_search_results`, plus the v2 memory tables (`memory_concepts`,
`memory_episodes`, `subconscious_log`, ...). Optional: `pgvector` extension for
embedding retrieval (autocreated when available).

## Non-code root directories

- `user_workspaces/` — runtime agent sandboxes (per-user), generated
- `docs/` — current feature docs (ARCHITECTURE, SKINS protocol)
- `scripts/` — build/start/stop/restart/apk lifecycle (PID-file safe: stop only
  kills the recorded PID, never pattern-kills)
- `docs/LICENSE-COMPLIANCE.md` — dependency license audit report
- `requirements/` — per-OS setup guides

## Gotchas

- **SSL**: `start.sh` auto-detects `backend/key.pem` + `backend/cert.pem`;
  self-signed for dev. Without them the server still runs on plain HTTP, but
  the Vite dev proxy and the Android shell expect TLS.
- **SPA fallback**: `backend/main.py` serves `index.html` for `/app/frontend/*`;
  API routes all start with `/api/`.
- **Static build**: `frontend/dist/` and `backend/static/` are gitignored —
  run `./scripts/project_build.sh` to populate.
- **Android keystore**: `webview-app/keystore.jks` is NOT shipped (gitignored) —
  create your own keystore before building the APK; the CHANGE_ME literals in
  `build.gradle` are placeholders only.
- **UI language**: Chinese strings throughout (default assistant 默认助手,
  group 新分组, etc.).
- **Python/Node versions**: dev matrix is Python 3.10+ (3.12/3.13 recommended)
  and Node 18+ (20/22 recommended).
