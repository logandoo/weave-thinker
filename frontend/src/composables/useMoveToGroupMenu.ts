// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 「移动到分组」菜单共享逻辑（Sidebar / 工作台抽屉共用）。
 *
 * 把「助手 × 分组」摊成菜单分区（纯逻辑在 moveMenuSections.ts，已单测），并封装
 * 统一的移动派发：同助手移组 / 移出组 / 跨助手（未分组或目标分组）一次搞定。
 * 跨助手移动到分组会同时发送 group_id + assistant_id（后端要求目标分组属于 resulting
 * assistant，见 conversation.py move_conversation）。
 */
import { computed, ref } from 'vue'
import { useGroupStore } from '@/stores/groups'
import { useAssistantStore } from '@/stores/assistant'
import { useChatStore } from '@/stores/chat'
import { useToast } from '@/composables/useToast'
import {
  buildMoveMenuSections,
  filterMoveMenuSections,
  mergeGroupsById,
  type MoveMenuSection,
} from '@/composables/moveMenuSections'

export function useMoveToGroupMenu() {
  const groupStore = useGroupStore()
  const assistantStore = useAssistantStore()
  const chatStore = useChatStore()
  const { show: showToast } = useToast()

  // 当前助手的分组（列表已加载）+ 全量分组（菜单打开时加载）合并去重：
  // 全量请求返回前也能先显示当前助手的分组，避免只有「未分组」的闪烁。
  const moveMenuSections = computed<MoveMenuSection[]>(() =>
    buildMoveMenuSections(
      assistantStore.assistants,
      assistantStore.currentAssistantId,
      mergeGroupsById(groupStore.groups, groupStore.allGroups),
    ),
  )

  // ── 搜索 + 折叠（助手/分组很多时，7-8 助手 × 100 分组 = 数百项，靠搜索+折叠定位）──
  const moveMenuQuery = ref('')
  // 显式展开/折叠覆盖（key=assistantId ?? '__none__'）；未覆盖时默认：当前助手展开、其余折叠。
  const sectionExpandedOverride = ref<Record<string, boolean>>({})

  const filteredMoveMenuSections = computed<MoveMenuSection[]>(() =>
    filterMoveMenuSections(moveMenuSections.value, moveMenuQuery.value),
  )

  function sectionKey(s: MoveMenuSection): string {
    return s.assistantId ?? '__none__'
  }

  /** 搜索中 → 命中节全部展开；否则看显式覆盖，未覆盖则当前助手展开、其余折叠。 */
  function isSectionExpanded(s: MoveMenuSection): boolean {
    if (moveMenuQuery.value.trim()) return true
    const ov = sectionExpandedOverride.value[sectionKey(s)]
    return ov === undefined ? s.isCurrent : ov
  }

  function toggleSection(s: MoveMenuSection): void {
    // 搜索态下分区强制展开（isSectionExpanded 恒 true），折叠无意义且会写入误导性覆盖 → 忽略。
    if (moveMenuQuery.value.trim()) return
    const key = sectionKey(s)
    sectionExpandedOverride.value = {
      ...sectionExpandedOverride.value,
      [key]: !isSectionExpanded(s),
    }
  }

  /** 打开菜单时重置搜索/折叠，避免上次残留。 */
  function resetMoveMenuState(): void {
    moveMenuQuery.value = ''
    sectionExpandedOverride.value = {}
  }

  /** 打开菜单时刷新全量分组（跨助手），保证新建/删除分组后菜单新鲜；失败静默降级。 */
  function refreshMoveMenuGroups(): void {
    void groupStore.loadAllGroups()
  }

  /**
   * 把对话移动到 (targetAssistantId, groupId)。
   * - 跨助手判定以「对话自身所属助手」为准（不是 currentAssistantId）：助手切换重载
   *   窗口内可能仍短暂渲染上一助手的会话，用 currentAssistantId 会漏发 assistant_id，
   *   后端按会话归属校验分组 → 400。
   * - 同助手：仅移组/移出组（本地更新 group_id）。
   * - 跨助手：同发 assistant_id，并从当前列表移除。
   * 返回是否成功，供调用方决定关菜单/同步拖拽列表。
   */
  async function moveToTarget(
    convId: string,
    groupId: string | null,
    targetAssistantId: string | null,
  ): Promise<boolean> {
    const conv = chatStore.conversations.find((c) => c.id === convId)
    const sourceAssistantId = conv?.assistant_id ?? assistantStore.currentAssistantId
    const crossAssistant = targetAssistantId != null && targetAssistantId !== sourceAssistantId
    try {
      await groupStore.moveConversation(
        convId,
        groupId,
        crossAssistant ? targetAssistantId : undefined,
      )
      if (crossAssistant) {
        chatStore.removeConversationFromList(convId)
        const target = targetAssistantId ? assistantStore.getAssistantById(targetAssistantId) : undefined
        const groupName = groupId
          ? [...groupStore.allGroups, ...groupStore.groups].find((g) => g.id === groupId)?.name
          : undefined
        const targetLabel = target ? `助手「${target.name}」` : '其他助手'
        showToast(groupName ? `已移动到${targetLabel}的分组「${groupName}」` : `已移动到${targetLabel}`, 'success')
      } else {
        if (conv) conv.group_id = groupId
        showToast(groupId ? '已移动到分组' : '已移出分组', 'success')
      }
      return true
    } catch (e) {
      console.error('Failed to move conversation:', e)
      showToast('移动失败', 'error')
      return false
    }
  }

  return {
    moveMenuSections,
    filteredMoveMenuSections,
    moveMenuQuery,
    isSectionExpanded,
    toggleSection,
    resetMoveMenuState,
    refreshMoveMenuGroups,
    moveToTarget,
  }
}
