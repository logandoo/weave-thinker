<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Discovery And Installation

RemPilot exposes one MCP server through two standard transports. Transport is a client-environment decision, not a resource scope.

## Native stdio

The stable Helper is:

`~/Library/Application Support/RemPilot/bin/rempilot-mcp`

1. Run `describe --json` from that absolute path.
2. Map its `command`, `args`, and `transport` into the AI client's documented MCP configuration. Preserve unrelated settings.
3. Execute the Helper directly. Do not add a shell wrapper, proxy, custom environment secret, scope argument, or app-bundle path.
4. Reload MCP servers and confirm `tools/list` succeeds. This proves only that the stdio protocol is ready; the Helper intentionally defers App startup, private-runtime connection, authorization, and task-journal restoration until a request needs them.

The Helper authenticates its parent from live OS process and code identity. If that identity is unsuitable for durable native trust, stdio fails with `stdio_client_not_trusted`; use Streamable HTTP. There is no process-scoped fallback, pairing code, Team descriptor, or temporary grant.

The Helper may start the installed RemPilot app hidden through the platform launcher when the private runtime is unavailable. This runtime activation must not open, restore, or focus an App window. Use `doctor --json` rather than process-name checks or visible app activation. A successful doctor result has `status: "transport_ready"` and `scope: "transport_preflight"`; it verifies the private transport and identity handshake but deliberately reports authorization as `not_checked`. Use `access-status --json` to distinguish pending approval, revoked access, and an ineligible stdio client.

## Local Streamable HTTP

When the AI backend itself runs on this computer, use the exact endpoint shown
by **RemPilot Settings → Agent Access → Streamable HTTP**. A native process may
also read `streamable_http.endpoint` from `describe --json`. If
`endpoint_status` is `not_allocated`, start MCP Runtime and query again. Never
guess or hardcode the port.

This includes locally deployed AI products whose user interface happens to open in a browser. Follow OAuth protected-resource discovery, DCR, Authorization Code + PKCE, and refresh-token rotation. The initial local Allow is persistent and does not require a RemPilot account login. Do not copy tokens or authorization codes into prompts or configuration.

The RemPilot App owns the loopback listener and forwards authorized calls through its private UDS runtime. RemPilot atomically allocates and privately persists the loopback endpoint. It normally reuses the same port across restarts; if another process occupies it, RemPilot publishes a new port and resets the old OAuth authority. Re-read the endpoint and reconnect once rather than editing tokens. Login, logout, and team membership only change the live resources returned by RemPilot.

The private UDS runtime and Streamable HTTP adapter have independent health. An unavailable HTTP authority or loopback listener disables only Streamable HTTP; native stdio remains available and Settings reports the HTTP fault separately.

A browser page without a local backend, hosted/cloud AI, remote runner, or another machine cannot reach the loopback address. Never configure `127.0.0.1` for such a client. If a separately deployed public RemPilot HTTP endpoint is available, use its advertised HTTPS URL and its account/device OAuth contract instead.

## Sources Of Truth

1. RemPilot Settings or `describe --json`: currently published local HTTP endpoint.
2. `describe --json`: native stdio descriptor.
3. `doctor --json`: bounded native private-transport preflight; authorization is not checked.
4. `access-status --json`: native-client eligibility and grant state.
5. OAuth protected-resource discovery: local or separately deployed HTTP authorization metadata.
6. MCP `tools/list`: published tool catalog and protocol readiness, not runtime authorization.
7. Current list/search/resource responses: resource IDs visible at that moment.
