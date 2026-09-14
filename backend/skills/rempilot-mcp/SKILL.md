---
name: rempilot-mcp
description: Install, connect, diagnose, and use RemPilot MCP for the hosts and resources currently available in RemPilot. Use for saved-host discovery, background remote or local shells, SFTP and transfers, snippets, port forwards, and session logs without exposing SSH credentials or writing visible terminal UI.
---

<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# RemPilot MCP

Use one RemPilot MCP server and one resource model. Never ask whether the user wants Local or Team mode, never put login state in MCP configuration, and never infer resource visibility from the UI's selected workspace. RemPilot returns only the resources currently accessible on the device that executes the call.

## Connect

Choose transport from the process that actually runs the MCP client:

- For a trusted native desktop MCP client that can launch a direct subprocess, use the stable Helper at `~/Library/Application Support/RemPilot/bin/rempilot-mcp`. Run `describe --json`, install the returned stdio descriptor unchanged, and configure exactly one server named `rempilot`.
- For an AI backend running on the same computer—including a locally deployed product whose UI is a web page—use the App-managed Streamable HTTP endpoint currently published by RemPilot Settings or `describe --json`, and complete its standard OAuth flow. Never guess or hardcode the port. The first Allow persists through refresh-token rotation and does not require a RemPilot login.
- A browser page alone or a hosted/cloud AI service cannot reach that loopback endpoint. Do not describe a visible web UI as the connector: determine where its backend runs. A separately deployed public RemPilot HTTP endpoint, when available, has its own HTTPS URL and account/device authorization.

After connection, inspect MCP `tools/list`. Treat that response as the published tool catalog, not proof that the private Runtime or a particular client grant is ready. Native stdio becomes protocol-ready before it contacts the App, so run `doctor --json` for a bounded private-transport preflight and `access-status --json` for native-client eligibility and grant state. Streamable HTTP is an optional, independently reported adapter; its failure must not be interpreted as a stdio failure.

Read [references/discovery.md](references/discovery.md) for the exact installation and diagnosis contract.

## Operate

- Discover resource IDs with `rempilot.host.*`, list/search tools, or the current MCP resources before operating. Do not reuse an ID after access changes without resolving it again.
- Use `rempilot.remote_shell.*` for remote commands, `rempilot.local_shell.*` for local commands, and SFTP tools for remote files and transfers.
- Never open or write a visible terminal for AI execution. Never fall back to raw `ssh`, `scp`, `sftp`, `rsync`, or manual credential handling.
- Reuse background sessions when appropriate and verify mutations with a follow-up read or status call.
- Let RemPilot decide live resource and tool access. Apply `readOnlyHint`, `destructiveHint`, and `rempilot/*` risk metadata through the MCP Host's configured full-access or approval mode. RemPilot does not add a second per-operation prompt.

Read [references/routing.md](references/routing.md) before choosing an execution path and [references/safety.md](references/safety.md) before any mutation.
