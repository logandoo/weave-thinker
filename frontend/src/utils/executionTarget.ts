// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

export interface ResolveExecutionTargetInput {
  conversationId: string | null | undefined
  currentConversationId: string | null | undefined
  storeTarget: string | null
  metaTarget: string | null | undefined
  /** Snapshot taken before adding an optimistic first user message. */
  hasMessages: boolean
}

export type ExecutionTargetVersions = Record<string, number>

/** Allocate a per-conversation request/mutation sequence, never a response sequence. */
export function advanceExecutionTargetVersion(
  versions: ExecutionTargetVersions,
  conversationId: string,
): number {
  const next = (versions[conversationId] || 0) + 1
  versions[conversationId] = next
  return next
}

export function isExecutionTargetVersionCurrent(
  versions: ExecutionTargetVersions,
  conversationId: string,
  version: number,
): boolean {
  return (versions[conversationId] || 0) === version
}

/** Row/meta is authoritative; the store is only an empty-current-row fallback. */
export function resolveExecutionTarget({
  conversationId,
  currentConversationId,
  storeTarget,
  metaTarget,
  hasMessages,
}: ResolveExecutionTargetInput): string | null {
  if (!conversationId) return storeTarget
  if (metaTarget !== undefined) return metaTarget
  if (hasMessages) return null
  return conversationId === currentConversationId ? storeTarget : null
}
