<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Tool Routing

## Discover First

- Resolve saved hosts with `rempilot.host.list`, `rempilot.host.search`, and `rempilot.host.inspect`.
- The result automatically reflects the executing device's current access. Login state, selected workspace, and Local/Team labels are not MCP inputs.
- Treat host IDs, vault IDs, session IDs, transfer IDs, and rule IDs as opaque. Resolve again after membership, login, or device availability changes.
- Use `rempilot.terminal.attach` only to read an existing user terminal and `rempilot.log.query` for retained evidence.

Initial read-only tools:

- `rempilot.host.list`
- `rempilot.host.search`
- `rempilot.host.inspect`
- `rempilot.terminal.attach`
- `rempilot.local.terminal.list`
- `rempilot.local.terminal.attach`
- `rempilot.log.query`
- `rempilot.sftp.list_remote_directory`
- `rempilot.sftp.read_remote_file`
- `rempilot.sftp.transfer_list`
- `rempilot.sftp.transfer_status`

Mutation tools remain discoverable, but their presence in `tools/list` is not authorization. The MCP Host applies its configured full-access or approval mode from the tool metadata; RemPilot separately rechecks the current resource, Client scope, revocation state, and execution lease.

## Commands

- Remote: open `rempilot.remote_shell.open`, retain the returned session identifiers, then use `write`, bounded `read`, `history`, `signal`, and `close`.
- Local: use the equivalent `rempilot.local_shell.*` lifecycle.
- Prefer one reusable background session per task. There is no visible-terminal or public one-shot-exec fallback.
- If a write pauses with `input_required`, continue only that call's opaque saved-secret workflow. Never transmit password text.

## Files And Transfers

- List/read with SFTP tools. Inspect before editing and apply exact text edits.
- Use transfer tools for upload/download and poll status to a terminal state.
- Use explicit create, move, delete, and permissions tools for remote mutations.

## Other Capabilities

- Search snippets before creating or running one. When several writable vaults exist, supply a `vault_id` returned by current discovery rather than relying on UI selection.
- List port forwards before create, update, delete, start, or stop. A rule and its target host must belong to the same accessible vault.
- Use `interaction.pick_local_path` only when user path selection is inherently required.

## Failures

Treat the guarded MCP error contract as the source of truth. A failed `tools/call`
returns `error.code`, a safe `error.message`, and these exact
`error.details` facts:

- `category`: failure class such as `authentication`, `connectivity`, `trust`,
  `authorization`, `integrity`, or `execution`.
- `stage`: the failed boundary, such as `ssh_resolve`, `ssh_connect`,
  `ssh_authenticate`, `ssh_trust`, `ssh_open_pty`, `sftp`, or `runtime`.
- `retryable`: whether retrying can be reasonable after the blocking condition
  changes. It is not permission to loop automatically.
- `action`: the next safe action. Follow `verify_credentials`,
  `review_host_key`, `check_network`, `open_rempilot`, `change_request`,
  `choose_another_resource`, `resolve_conflict`, `retry`, or
  `inspect_local_diagnostics` as applicable.

Failed `tasks/get` responses expose the same facts in `error.data`, alongside
`error_code` and an optional `operation_state`. Runtime-provided messages and
details are intentionally removed at the MCP boundary, so do not ask for raw
SSH errors or expect credentials, host names, paths, or server output there.

Never replay an operation with `operation_outcome_unknown`,
`password_submission_outcome_unknown`, or `tool_result_rejected`. For local
diagnosis, retain the operation ID and public error code and ask the user to
inspect RemPilot diagnostics. Retry only when `retryable` is true, the indicated
condition has changed, and replay is safe for that operation.

Start with read-only evidence. Invoke a mutation only when it matches the requested outcome and the current RemPilot policy permits it.
