<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div v-if="visible" class="zen-history-drawer-overlay" @click="$emit('close')"></div>
    <div v-if="visible" class="zen-history-drawer" :class="{ 'sidebar-dragging': isDragging }">
      <div class="drawer-header">
        <span class="drawer-title">Agent历史</span>
        <div class="drawer-actions">
          <button class="drawer-action-btn" @click="openCreateGroupDialog" title="新建分组">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              <line x1="12" y1="11" x2="12" y2="17"/>
              <line x1="9" y1="14" x2="15" y2="14"/>
            </svg>
          </button>
          <button class="drawer-action-btn" @click="openSpotlightSearch" title="搜索对话">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="8"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
          </button>
          <button class="drawer-close" @click="$emit('close')">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>

      <div class="drawer-body">
        <!-- 助手切换（工作台 Agent 侧栏）：切换后会话/分组列表随所选助手刷新 -->
        <div class="zen-asst-switcher">
          <button class="zen-asst-trigger" @click.stop="asstMenuOpen = !asstMenuOpen" :title="currentAssistantName">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
              <circle cx="12" cy="7" r="4"/>
            </svg>
            <span class="zen-asst-name">{{ currentAssistantName }}</span>
            <svg class="zen-asst-chevron" :class="{ expanded: asstMenuOpen }" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="6 9 12 15 18 9"/>
            </svg>
          </button>
          <div v-if="asstMenuOpen" class="zen-asst-menu" @click.stop>
            <button
              v-for="a in assistantStore.assistants"
              :key="a.id"
              class="zen-asst-item"
              :class="{ active: a.id === assistantStore.currentAssistantId }"
              @click="selectAssistantFromDrawer(a.id)"
            >
              {{ a.name }}
            </button>
            <div v-if="!assistantStore.assistants.length" class="zen-asst-empty">暂无助手</div>
          </div>
        </div>

        <!-- New Chat Button -->
        <div class="action-buttons">
          <button class="new-chat-btn" @click="handleNewChat">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 20h9"/>
              <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
            </svg>
            <span>新建对话</span>
          </button>
        </div>

        <!-- Selection Bar -->
        <div class="selection-bar" v-if="selectionModeActive">
          <div class="selection-bar-header">
            <label class="select-all-label" v-if="isExportMode">
              <input
                type="checkbox"
                :checked="selectedConversationIds.size === chatStore.conversations.length && chatStore.conversations.length > 0"
                @change="toggleSelectAll"
              />
              全选
            </label>
            <span class="selected-count">
              <template v-if="isDeleteMode">
                <template v-if="selectedConversationIds.size > 0 && selectedGroupIds.size > 0">
                  {{ selectedConversationIds.size }}个对话, {{ selectedGroupIds.size }}个分组
                </template>
                <template v-else-if="selectedConversationIds.size > 0">
                  {{ selectedConversationIds.size }}个对话
                </template>
                <template v-else-if="selectedGroupIds.size > 0">
                  {{ selectedGroupIds.size }}个分组
                </template>
                <template v-else>未选择</template>
              </template>
              <template v-else>{{ selectedConversationIds.size }} 已选</template>
            </span>
          </div>
          <div class="selection-bar-actions">
            <button
              class="selection-confirm-btn"
              :class="{ 'export-confirm-btn': isExportMode, 'delete-confirm-btn': isDeleteMode }"
              @click="handleSelectionConfirm"
              :disabled="!hasAnySelection || selectionPending"
            >
              {{ selectionPending ? (isExportMode ? '导出中...' : '删除中...') : (isExportMode ? '导出' : '删除') }}
            </button>
            <button class="selection-cancel-btn" @click="exitSelectionMode">取消</button>
          </div>
          <div class="selection-progress" v-if="selectionProgress">{{ selectionProgress }}</div>
        </div>

        <!-- Conversation List -->
        <div
          
          class="conversation-list"
          ref="conversationListEl"
          @mousedown="onConversationListMouseDown"
        >
          <!-- Groups Directory -->
          <div class="groups-directory">
            <div class="groups-directory-header" @click="toggleGroupsDirectory">
              <svg class="directory-chevron" :class="{ expanded: !groupsDirectoryCollapsed }" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="9 6 15 12 9 18"/>
              </svg>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              </svg>
              <span class="directory-name">分组对话</span>
              <span class="directory-count">{{ groupStore.groups.length }}</span>
            </div>
            <div v-show="!groupsDirectoryCollapsed" class="groups-directory-content">
              <SortableList
                v-model="groupStore.groups"
                :group="{ name: 'zen-groups', pull: false, put: false }"
                item-key="id"
                handle=".group-header"
                :delay="100"
                :disabled="selectionModeActive"
                @start="onDragStart"
                @end="onGroupDragEnd"
              >
                <div v-for="group in groupStore.groups" :key="group.id"
                     class="conversation-group"
                     :data-group-id="group.id"
                     :data-draggable="true"
                     :data-key="group.id"
                     :class="{ 'drag-hover': isDragging && dragExpandedGroupId === group.id }"
                >
                  <div
                    class="group-header"
                    :class="{ 'group-selected': isDeleteMode && selectedGroupIds.has(group.id), 'group-focus': focusGroupId === group.id }"
                    @click="handleGroupHeaderClick(group.id)"
                  >
                    <input
                      v-if="isDeleteMode"
                      type="checkbox"
                      class="group-checkbox"
                      :checked="selectedGroupIds.has(group.id)"
                      @click.stop
                      @change="toggleGroupSelect(group.id)"
                    />
                    <svg
                      class="group-chevron"
                      :class="{ expanded: !groupStore.isGroupCollapsed(group.id) || (isDragging && dragExpandedGroupId === group.id) }"
                      width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                    >
                      <polyline points="9 6 15 12 9 18"/>
                    </svg>
                    <span class="group-color-dot" :style="{ backgroundColor: group.color }"></span>
                    <span class="group-name">{{ group.name }}</span>
                    <span class="group-count">{{ getGroupConvCount(group.id) }}</span>
                    <div class="group-actions" v-if="!isDeleteMode">
                      <button class="group-action-btn" @click.stop="openEditGroupDialog(group)" title="编辑分组">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                        </svg>
                      </button>
                      <button class="group-action-btn delete" @click.stop="openDeleteGroupDialog(group)" title="删除分组">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                          <polyline points="3 6 5 6 21 6"/>
                          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                        </svg>
                      </button>
                    </div>
                  </div>
                  <div class="group-conversations" :class="{ 'group-convs-collapsed': groupStore.isGroupCollapsed(group.id) && (!isDragging || dragExpandedGroupId !== group.id) }">
                    <SortableList
                      class="group-conv-drag-list"
                      :list="dragLists[group.id] || []"
                      :group="convDragGroup"
                      item-key="id"
                      :sort-key="'group:' + group.id"

                      :delay="100"

                      :sort="false"

                      :force-reinit="forceReinitCounter"
                      :disabled="selectionModeActive || editingTitleId !== null"
                      ghost-class="sortable-ghost"
                      @start="onDragStart"
                      @end="onConversationDragEnd"
                    >
                      <div v-for="conv in (dragLists[group.id] || [])" :key="conv.id"
                           data-draggable="true" :data-key="conv.id">
                        <ConversationRow
                          :conv="conv"
                          :selection-mode-active="selectionModeActive"
                          :selected="selectedConversationIds.has(conv.id)"
                          :editing-title-id="editingTitleId"

                          :editing-title="editingTitle"
                          :swiped-conversation-id="swipedConversationId"
                          :swipe-offset="swipeOffset"

                          :is-swipe-dragging="isSwipeDragging"
                          :current-conversation-id="chatStore.currentConversationId"
                          @click="handleConversationClick"
                          @toggle-select="toggleSelect"
                          @start-edit="startEditTitle"
                          @save-title="saveTitle"
                          @cancel-edit="cancelEditTitle"

                          @update-title="updateTitle"
                          @swipe-start="handleTouchStart"
                          @swipe-end="handleTouchEnd"
                          @swipe-move="handleTouchMove"
                          @swipe-export="handleSwipeExport"
                          @swipe-edit="handleSwipeEdit"

                          @swipe-move-group="handleSwipeMoveGroup"
                          @swipe-save-note="handleSwipeSaveToNote"
                          @swipe-delete="handleSwipeDelete"
                          @toggle-menu="toggleMenu"
                        />
                      </div>
                    </SortableList>
                  </div>
                </div>
              </SortableList>
            </div>
          </div>

          <!-- Ungrouped Conversations - Time Categorized -->
          <div
            v-for="category in ungroupedTimeCategories"
            :key="category.key"
            class="time-category-section"
          >
            <div class="time-category-header">{{ category.label }}</div>
            <SortableList
              class="ungrouped-conv-drag-list"
              :list="dragLists[category.key] || []"
              :group="convDragGroup"
              item-key="id"
              :sort-key="'ungrouped:' + category.key"

              :delay="100"

              :sort="false"

              :force-reinit="forceReinitCounter"
              :disabled="selectionModeActive || editingTitleId !== null"
              ghost-class="sortable-ghost"
              @start="onDragStart"
              @end="onConversationDragEnd"
            >
              <div v-for="conv in (dragLists[category.key] || [])" :key="conv.id"
                   data-draggable="true" :data-key="conv.id">
                <ConversationRow
                  :conv="conv"
                  :selection-mode-active="selectionModeActive"
                  :selected="selectedConversationIds.has(conv.id)"
                  :editing-title-id="editingTitleId"

                  :editing-title="editingTitle"
                  :swiped-conversation-id="swipedConversationId"
                  :swipe-offset="swipeOffset"

                  :is-swipe-dragging="isSwipeDragging"
                  :current-conversation-id="chatStore.currentConversationId"
                  @click="handleConversationClick"
                  @toggle-select="toggleSelect"
                  @start-edit="startEditTitle"
                  @save-title="saveTitle"
                  @cancel-edit="cancelEditTitle"

                  @update-title="updateTitle"
                  @swipe-start="handleTouchStart"
                  @swipe-end="handleTouchEnd"
                  @swipe-move="handleTouchMove"
                  @swipe-export="handleSwipeExport"
                  @swipe-edit="handleSwipeEdit"

                  @swipe-move-group="handleSwipeMoveGroup"
                  @swipe-save-note="handleSwipeSaveToNote"
                  @swipe-delete="handleSwipeDelete"
                  @toggle-menu="toggleMenu"
                />
              </div>
            </SortableList>
          </div>

          <!-- Empty ungrouped drop zone -->
          <div
            v-if="ungroupedTimeCategories.length === 0 && groupStore.groups.length > 0"
            class="ungrouped-drop-zone"
          >
            <div class="ungrouped-drop-zone-label">未分组对话</div>
            <SortableList
              class="ungrouped-conv-drag-list ungrouped-empty-drag-list"
              :list="emptyUngroupedList"
              :group="convDragGroup"
              item-key="id"
              sort-key="ungrouped:empty"

              :delay="100"

              :sort="false"

              :force-reinit="forceReinitCounter"
              :disabled="selectionModeActive || editingTitleId !== null"
              ghost-class="sortable-ghost"
              @start="onDragStart"
              @end="onConversationDragEnd"
            >
              <div v-for="item in emptyUngroupedList" :key="item.id"
                   data-draggable="true" :data-key="item.id">
              </div>
            </SortableList>
          </div>

          <div v-if="!loadingConversations && chatStore.conversations.length === 0 && groupStore.groups.length === 0" class="list-empty">
            暂无对话
          </div>
          <div v-if="loadingConversations" class="list-empty">加载中...</div>
        </div>

        <!-- Marquee overlay -->
        <div
          v-show="isMarqueeSelecting && marqueeAnchor"
          class="marquee-overlay"
          :style="marqueeStyle"
        ></div>
      </div>

      <!-- Context Menu -->
      <Teleport to="body">
        <div v-if="activeMenuId" class="conversation-menu" :style="menuStyle" @click.stop>
          <button class="menu-item" @click="handleMenuEdit">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            <span>修改名称</span>
          </button>
          <button class="menu-item" @click="handleMenuExport">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            <span>导出对话</span>
          </button>
          <button class="menu-item" @click="handleMenuSaveToNote">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
            </svg>
            <span>添加到笔记</span>
          </button>
          <button class="menu-item delete" @click="handleMenuDelete">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
            <span>删除对话</span>
          </button>
          <div class="menu-divider"></div>
          <button
            v-if="menuTargetConv?.group_id"
            class="menu-item"
            @click="removeConversationFromGroup(menuTargetConv.id)"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M18 6L6 18M6 6l12 12"/>
            </svg>
            <span>移出分组</span>
          </button>
          <button class="menu-item" @click="openMoveToGroupMenu($event)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              <line x1="12" y1="11" x2="12" y2="17"/>
              <line x1="9" y1="14" x2="15" y2="14"/>
            </svg>
            <span>移动到分组</span>
          </button>
        </div>
      </Teleport>

      <!-- Move to Group Sub-menu -->
      <Teleport to="body">
        <div v-if="showMoveToGroupMenu" class="move-group-overlay" @mousedown.self="closeAllMenus"></div>
        <div v-if="showMoveToGroupMenu" class="conversation-menu move-group-menu" :style="moveMenuStyle" @click.stop>
          <button class="menu-item" @click="moveConversationToGroup(moveTargetConv!.id, null)">
            <span>未分组</span>
          </button>
          <button
            v-for="group in groupStore.getGroupsForAssistant(assistantStore.currentAssistantId)"
            :key="group.id"
            class="menu-item"
            :class="{ active: moveTargetConv?.group_id === group.id }"
            @click="moveConversationToGroup(moveTargetConv!.id, group.id)"
          >
            <span class="group-color-dot" :style="{ backgroundColor: group.color }"></span>
            <span>{{ group.name }}</span>
          </button>
        </div>
      </Teleport>

      <!-- Group Dialog (Create/Edit) -->
      <Teleport to="body">
        <div v-if="showGroupDialog" class="modal-overlay" @mousedown.self="showGroupDialog = false">
          <div class="modal-content" @click.stop>
            <h3 class="modal-title">{{ editingGroup ? '编辑分组' : '新建分组' }}</h3>
            <div class="modal-body">
              <div class="form-group">
                <label>分组名称</label>
                <input v-model="newGroupName" type="text" placeholder="输入分组名称" @keyup.enter="saveGroup" />
              </div>
              <div class="form-group">
                <label>标签颜色</label>
                <div class="color-picker">
                  <button
                    v-for="color in PRESET_COLORS"
                    :key="color"
                    class="color-option"
                    :class="{ active: newGroupColor === color }"
                    :style="{ backgroundColor: color }"
                    @click="newGroupColor = color"
                  ></button>
                </div>
              </div>
            </div>
            <div class="modal-actions">
              <button class="modal-btn cancel" @click="showGroupDialog = false">取消</button>
              <button class="modal-btn confirm" @click="saveGroup">保存</button>
            </div>
          </div>
        </div>
      </Teleport>

      <!-- Delete Group Confirmation -->
      <Teleport to="body">
        <div v-if="showDeleteGroupDialog" class="modal-overlay" @mousedown.self="showDeleteGroupDialog = false">
          <div class="modal-content" @click.stop>
            <h3 class="modal-title">删除分组</h3>
            <div class="modal-body">
              <p>确定要删除分组 "{{ deletingGroup?.name }}" 吗？</p>
              <label class="checkbox-label">
                <input type="checkbox" v-model="deleteGroupWithConversations" />
                <span>同时删除分组中的全部对话</span>
              </label>
            </div>
            <div class="modal-actions">
              <button class="modal-btn cancel" @click="showDeleteGroupDialog = false">取消</button>
              <button class="modal-btn delete" @click="confirmDeleteGroup">删除</button>
            </div>
          </div>
        </div>
      </Teleport>

      <!-- Bulk Delete Confirmation -->
      <Teleport to="body">
        <div v-if="showBulkDeleteDialog" class="modal-overlay" @click="showBulkDeleteDialog = false">
          <div class="modal-content" @click.stop>
            <h3 class="modal-title">确认删除</h3>
            <div class="modal-body">
              <p>{{ bulkDeleteConfirmMessage }}</p>
              <label v-if="selectedGroupIds.size > 0" class="checkbox-label">
                <input type="checkbox" v-model="bulkDeleteWithConversations" />
                <span>同时删除分组中的全部对话</span>
              </label>
            </div>
            <div class="modal-actions">
              <button class="modal-btn cancel" @click="showBulkDeleteDialog = false">取消</button>
              <button class="modal-btn delete" @click="confirmBulkDelete">删除</button>
            </div>
          </div>
        </div>
      </Teleport>

      <!-- Notebook Picker for Save-to-Note -->
      <NotebookPicker
        v-if="showNotebookPicker"
        @select="handleNotebookSelected"
        @close="showNotebookPicker = false"
      />

      <!-- Spotlight Search Modal -->
      <div v-if="spotlightVisible" class="spotlight-overlay" @click="closeSpotlightSearch">
        <div class="spotlight-modal" @click.stop>
          <div class="spotlight-input-wrapper">
            <svg class="spotlight-search-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="8"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input
              ref="spotlightInputRef"
              v-model="spotlightQuery"
              class="spotlight-input"
              placeholder="搜索对话内容..."
              @input="onSpotlightInput"
              @keydown.escape="closeSpotlightSearch"
            />
          </div>
          <div class="spotlight-results" v-if="spotlightQuery.trim()">
            <div v-if="spotlightLoading" class="spotlight-status">搜索中...</div>
            <div v-else-if="spotlightResults.length === 0" class="spotlight-status">未找到相关对话</div>
            <div
              v-for="result in spotlightResults"
              :key="result.conversation_id"
              class="spotlight-result-item"
              @click="handleSpotlightResultClick(result.conversation_id, spotlightQuery)"
            >
              <div class="spotlight-result-title" @click="handleSpotlightResultClick(result.conversation_id, spotlightQuery)">{{ result.title }}</div>
              <div
                v-for="msg in result.matched_messages"
                :key="msg.id"
                class="spotlight-result-snippet spotlight-result-snippet--clickable"
                @click.stop="handleSpotlightMessageClick(result.conversation_id, msg.id, spotlightQuery)"
              >
                <span class="spotlight-snippet-role">{{ msg.role === 'user' ? '用户' : '助手' }}:</span>
                <span class="spotlight-snippet-text" v-html="highlightKeyword(msg.content_snippet, spotlightQuery)"></span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Batch Operation Popup -->
      <div v-if="batchPopupVisible" class="batch-popup-overlay" @click="closeBatchPopup">
        <div class="batch-popup" @click.stop>
          <div class="batch-popup-header">
            <h2>{{ batchPopupMode === 'export' ? '批量导出会话' : '批量删除会话' }}</h2>
            <button class="batch-popup-close" @click="closeBatchPopup">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="batch-popup-body">
            <div class="batch-popup-toolbar">
              <label class="batch-select-all">
                <input type="checkbox" :checked="batchPopupSelected.size === chatStore.conversations.length && chatStore.conversations.length > 0" @change="toggleBatchPopupSelectAll" />
                全选 ({{ batchPopupSelected.size }}/{{ chatStore.conversations.length }})
              </label>
            </div>
            <div class="batch-popup-list">
              <div v-if="chatStore.conversations.length === 0" class="batch-popup-empty">暂无会话</div>
              <div
                v-for="conv in chatStore.conversations"
                :key="conv.id"
                :data-conversation-id="conv.id"
                class="batch-popup-item"
                :class="{ selected: batchPopupSelected.has(conv.id) }"
                @click="toggleBatchPopupSelect(conv.id)"
              >
                <input type="checkbox" :checked="batchPopupSelected.has(conv.id)" @click.stop @change="toggleBatchPopupSelect(conv.id)" />
                <div class="batch-popup-item-info">
                  <div class="batch-popup-item-title">{{ conv.title || '新对话' }}</div>
                  <div class="batch-popup-item-meta">{{ formatDate(conv.updated_at) }}</div>
                </div>
              </div>
            </div>
          </div>
          <div class="batch-popup-footer">
            <div class="batch-popup-status" v-if="batchPopupProgress">{{ batchPopupProgress }}</div>
            <button class="batch-popup-cancel" @click="closeBatchPopup" :disabled="batchPopupPending">取消</button>
            <button
              class="batch-popup-confirm"
              :class="{ 'batch-popup-delete': batchPopupMode === 'delete' }"
              @click="handleBatchPopupConfirm"
              :disabled="batchPopupSelected.size === 0 || batchPopupPending"
            >
              {{ batchPopupPending ? '处理中...' : (batchPopupMode === 'export' ? '导出' : '删除') }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useChatStore } from '@/stores/chat'
import { useAssistantStore } from '@/stores/assistant'
import { useGroupStore } from '@/stores/groups'
import { useNotesStore } from '@/stores/notes'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { navigateWithMobileHistory } from '@/composables/useMobileNavigation'
import type { Conversation, ConversationGroup, ConversationSearchResult } from '@/types'
import { chatApi } from '@/api/chat'
import ConversationRow from './ConversationRow.vue'
import SortableList from './SortableList.vue'
import NotebookPicker from './NotebookPicker.vue'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; selectConversation: [conversationId: string] }>()

const chatStore = useChatStore()
const assistantStore = useAssistantStore()
const groupStore = useGroupStore()
const notesStore = useNotesStore()
const router = useRouter()
const route = useRoute()
const { show: showToast } = useToast()
const { confirm: showConfirm } = useConfirmDialog()

const loadingConversations = ref(false)
const forceReinitCounter = ref(0)
const editingTitleId = ref<string | null>(null)
const editingTitle = ref('')
const titleInput = ref<HTMLInputElement | null>(null)
const selectionMode = ref<'export' | 'delete' | null>(null)
const selectedConversationIds = ref<Set<string>>(new Set())
const selectedGroupIds = ref<Set<string>>(new Set())
const selectionPending = ref(false)
const selectionProgress = ref('')
const activeMenuId = ref<string | null>(null)
const menuStyle = ref<{ top: string; left: string }>({ top: '0px', left: '0px' })
const menuTargetConv = ref<{ id: string; title: string; group_id?: string | null } | null>(null)

// Group management
const showGroupDialog = ref(false)
const editingGroup = ref<ConversationGroup | null>(null)
const newGroupName = ref('')
const newGroupColor = ref('#3b82f6')
const showDeleteGroupDialog = ref(false)
const deletingGroup = ref<ConversationGroup | null>(null)
const deleteGroupWithConversations = ref(false)
const showMoveToGroupMenu = ref(false)
const moveMenuStyle = ref<{ top: string; left: string }>({ top: '0px', left: '0px' })
const moveTargetConv = ref<{ id: string; title: string; group_id?: string | null } | null>(null)

const showBulkDeleteDialog = ref(false)
const bulkDeleteConfirmMessage = ref('')
const bulkDeleteWithConversations = ref(false)
const bulkDeleteContext = ref<'selection' | 'batchPopup'>('selection')

// Drag state
const dragLists = reactive<Record<string, Conversation[]>>({})
const isDragging = ref(false)
const dragExpandedGroupId = ref<string | null>(null)
const groupsDirectoryWasCollapsed = ref(false)
const emptyUngroupedList = ref<Conversation[]>([])

const GROUPS_DIR_KEY = 'chatllm_zen_groups_dir_collapsed'
const groupsDirectoryCollapsed = ref(localStorage.getItem(GROUPS_DIR_KEY) === 'true')

function toggleGroupsDirectory() {
  groupsDirectoryCollapsed.value = !groupsDirectoryCollapsed.value
  localStorage.setItem(GROUPS_DIR_KEY, String(groupsDirectoryCollapsed.value))
}

// Marquee
const conversationListEl = ref<HTMLDivElement | null>(null)
const isMarqueeSelecting = ref(false)
const marqueeAnchor = ref<{ x: number; y: number } | null>(null)
const marqueeEnd = ref<{ x: number; y: number } | null>(null)
const marqueePreviousSelection = ref<Set<string>>(new Set())
let suppressClickAfterMarqueeUntil = 0

// Edge scroll
let edgeScrollRaf: number | null = null
const lastPointerPosition = ref<{ x: number; y: number } | null>(null)
const EDGE_SCROLL_THRESHOLD = 40
const EDGE_SCROLL_SPEED = 10

// Swipe
const swipedConversationId = ref<string | null>(null)
const swipeOffset = ref(0)
const swipeTrackingId = ref<string | null>(null)
const swipeStartX = ref(0)
const swipeStartY = ref(0)
const swipeStartOffset = ref(0)
const isSwipeTracking = ref(false)
const isSwipeDragging = ref(false)
const SWIPE_ACTION_WIDTH = 240
let suppressConversationClickUntil = 0

// Save-to-note
const showNotebookPicker = ref(false)
const saveToNoteConvId = ref<string | null>(null)
const saveToNoteConvTitle = ref('')

// Spotlight search
const spotlightVisible = ref(false)
const spotlightQuery = ref('')
const spotlightResults = ref<ConversationSearchResult[]>([])
const spotlightLoading = ref(false)
const spotlightInputRef = ref<HTMLInputElement | null>(null)
let spotlightDebounceTimer: ReturnType<typeof setTimeout> | null = null
// Last-request-wins guard (same race as Sidebar.vue: slow in-flight query
// overwriting a faster correct one — "先正确后突然变完全不相关", 2026-08-07).
let spotlightSearchSeq = 0

// Conversation to reveal (expand group + focus scroll) on next drawer open —
// survives the v-if unmount triggered by the search-jump close.
let pendingRevealConversationId: string | null = null

// Settle time for the 0.25s grid-template-rows group expand transition
const GROUP_EXPAND_SETTLE_MS = 320

function formatDate(dateStr: string): string {
  if (!dateStr) return ''
  const normalized = dateStr.endsWith('Z') || dateStr.includes('+') || dateStr.includes('-', 10) ? dateStr : dateStr + 'Z'
  const date = new Date(normalized)
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

function openSpotlightSearch() {
  spotlightVisible.value = true
  spotlightQuery.value = ''
  spotlightResults.value = []
  spotlightLoading.value = false
  spotlightSearchSeq++
  nextTick(() => { spotlightInputRef.value?.focus() })
}

function closeSpotlightSearch() {
  spotlightVisible.value = false
  spotlightQuery.value = ''
  spotlightResults.value = []
  spotlightLoading.value = false
  spotlightSearchSeq++
  if (spotlightDebounceTimer) clearTimeout(spotlightDebounceTimer)
}

function onSpotlightInput() {
  if (spotlightDebounceTimer) clearTimeout(spotlightDebounceTimer)
  const q = spotlightQuery.value.trim()
  if (!q) {
    spotlightSearchSeq++
    spotlightResults.value = []
    spotlightLoading.value = false
    return
  }
  spotlightLoading.value = true
  const seq = ++spotlightSearchSeq
  spotlightDebounceTimer = setTimeout(async () => {
    const currentQ = spotlightQuery.value.trim()
    if (!currentQ || seq !== spotlightSearchSeq) {
      spotlightResults.value = []
      spotlightLoading.value = false
      return
    }
    try {
      const data = await chatApi.searchConversations(currentQ)
      if (seq !== spotlightSearchSeq) return
      spotlightResults.value = data
    } catch {
      if (seq !== spotlightSearchSeq) return
      spotlightResults.value = []
    }
    if (seq === spotlightSearchSeq) spotlightLoading.value = false
  }, 300)
}

async function handleSpotlightResultClick(conversationId: string, query: string) {
  closeSpotlightSearch()
  chatStore.searchHighlightQuery = query
  chatStore.searchHighlightMessageId = ''
  chatStore.searchHighlightNonce++
  closeSwipeActions()
  await chatStore.selectConversation(conversationId)
  // Reveal (group expand + scroll) BEFORE emitting close: the drawer root is
  // v-if="visible" and unmounts on 'selectConversation'/'close', which would
  // kill the scroll before it runs. The scroll itself is replayed on reopen
  // via pendingRevealConversationId.
  pendingRevealConversationId = conversationId
  await revealConversationInSidebar(conversationId)
  emit('selectConversation', conversationId)
  emit('close')
}

async function handleSpotlightMessageClick(conversationId: string, messageId: string, query: string) {
  closeSpotlightSearch()
  chatStore.searchHighlightQuery = query
  chatStore.searchHighlightMessageId = messageId
  chatStore.searchHighlightNonce++
  closeSwipeActions()
  await chatStore.selectConversation(conversationId)
  pendingRevealConversationId = conversationId
  await revealConversationInSidebar(conversationId)
  emit('selectConversation', conversationId)
  emit('close')
}

async function revealConversationInSidebar(conversationId: string) {
  const conv = chatStore.conversations.find(c => c.id === conversationId)
  const groupId = conv?.group_id || null
  const groupExists = !!(groupId && groupStore.groups.some(g => g.id === groupId))
  const wasGroupCollapsed = groupExists && groupStore.isGroupCollapsed(groupId!)
  if (wasGroupCollapsed) groupStore.expandGroup(groupId!)
  const wasDirCollapsed = groupsDirectoryCollapsed.value
  if (wasDirCollapsed) groupsDirectoryCollapsed.value = false
  await nextTick()
  // The group collapse/expand animation is 0.25s grid-template-rows; only wait
  // when a group was actually collapsed so the scroll lands on the settled position.
  if (wasGroupCollapsed) {
    await new Promise(r => setTimeout(r, GROUP_EXPAND_SETTLE_MS))
  }
  const list = conversationListEl.value
  const el = list?.querySelector(`.conversation-item[data-id="${conversationId}"]`) as HTMLElement | null
  if (list && el) {
    const listRect = list.getBoundingClientRect()
    const elRect = el.getBoundingClientRect()
    list.scrollTo({
      top: list.scrollTop + (elRect.top - listRect.top - (listRect.height - elRect.height) / 2),
      behavior: 'smooth',
    })
  } else if (!el) {
    console.warn('revealConversationInSidebar: row not found in drawer', conversationId, { groupId, groupExists })
  }
}

// Batch popup
const batchPopupVisible = ref(false)
const batchPopupMode = ref<'export' | 'delete'>('export')
const batchPopupSelected = ref<Set<string>>(new Set())
const batchPopupPending = ref(false)
const batchPopupProgress = ref('')

function openBatchExport() {
  batchPopupMode.value = 'export'
  batchPopupSelected.value = new Set()
  batchPopupPending.value = false
  batchPopupProgress.value = ''
  batchPopupVisible.value = true
}

function openBatchDelete() {
  batchPopupMode.value = 'delete'
  batchPopupSelected.value = new Set()
  batchPopupPending.value = false
  batchPopupProgress.value = ''
  batchPopupVisible.value = true
}

function closeBatchPopup() {
  batchPopupVisible.value = false
  batchPopupSelected.value = new Set()
  showBulkDeleteDialog.value = false
  bulkDeleteContext.value = 'selection'
}

function toggleBatchPopupSelect(id: string) {
  const next = new Set(batchPopupSelected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  batchPopupSelected.value = next
}

function toggleBatchPopupSelectAll() {
  if (batchPopupSelected.value.size === chatStore.conversations.length) {
    batchPopupSelected.value = new Set()
  } else {
    batchPopupSelected.value = new Set(chatStore.conversations.map(c => c.id))
  }
}

async function handleBatchPopupConfirm() {
  const ids = Array.from(batchPopupSelected.value)
  if (ids.length === 0) return
  if (batchPopupMode.value === 'export') {
    batchPopupPending.value = true
    try {
      batchPopupProgress.value = '导出中...'
      await chatApi.exportConversations(assistantStore.currentAssistantId || '', ids)
      batchPopupProgress.value = '导出完成'
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e?.message || '操作失败'
      batchPopupProgress.value = `错误: ${msg}`
      return
    }
    batchPopupPending.value = false
    setTimeout(() => closeBatchPopup(), 1000)
  } else {
    batchPopupVisible.value = false
    bulkDeleteConfirmMessage.value = `确定要删除选中的${ids.length}个会话吗？`
    bulkDeleteWithConversations.value = false
    bulkDeleteContext.value = 'batchPopup'
    showBulkDeleteDialog.value = true
  }
}

async function executeBatchPopupDelete() {
  showBulkDeleteDialog.value = false
  batchPopupPending.value = true
  try {
    batchPopupProgress.value = '删除中...'
    const ids = Array.from(batchPopupSelected.value)
    const currentId = chatStore.currentConversationId
    await chatStore.bulkDeleteConversations(ids)
    await chatStore.loadConversations(assistantStore.currentAssistantId)
    if (currentId && ids.includes(currentId) && route.path === '/zen') {
      await navigateWithMobileHistory(router, '/zen', { replace: true })
    }
    batchPopupProgress.value = '删除完成'
  } catch (e: any) {
    const msg = e?.response?.data?.detail || e?.message || '操作失败'
    batchPopupProgress.value = `错误: ${msg}`
    return
  }
  batchPopupPending.value = false
  setTimeout(() => closeBatchPopup(), 1000)
}

// Computed
const selectionModeActive = computed(() => selectionMode.value !== null)
const isExportMode = computed(() => selectionMode.value === 'export')
const isDeleteMode = computed(() => selectionMode.value === 'delete')
const hasAnySelection = computed(() => selectedConversationIds.value.size > 0 || selectedGroupIds.value.size > 0)

// Group that owns the current conversation — parent-group focus state
const focusGroupId = computed(() => {
  const current = chatStore.conversations.find(c => c.id === chatStore.currentConversationId)
  return current?.group_id || null
})

const marqueeStyle = computed(() => {
  if (!marqueeAnchor.value) return {}
  const end = marqueeEnd.value || marqueeAnchor.value
  return {
    left: `${Math.min(marqueeAnchor.value.x, end.x)}px`,
    top: `${Math.min(marqueeAnchor.value.y, end.y)}px`,
    width: `${Math.abs(end.x - marqueeAnchor.value.x)}px`,
    height: `${Math.abs(end.y - marqueeAnchor.value.y)}px`,
  }
})

const PRESET_COLORS = [
  '#ef4444', '#f97316', '#f59e0b', '#84cc16',
  '#10b981', '#06b6d4', '#3b82f6', '#6366f1',
  '#8b5cf6', '#d946ef', '#f43f5e', '#6b7280'
]

// Time categories
function getTimeCategoryKey(date: Date): string {
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffDays = diffMs / (24 * 60 * 60 * 1000)
  if (diffDays <= 7) return 'recent'
  if (diffDays <= 30) return 'month'
  return 'earlier'
}

function getTimeCategoryLabel(key: string): string {
  if (key === 'recent') return '7天内'
  if (key === 'month') return '30天内'
  return '更早'
}

function categorizeConversationsByTime(conversations: Conversation[]): Map<string, Conversation[]> {
  const categories = new Map<string, Conversation[]>()
  categories.set('recent', [])
  categories.set('month', [])
  categories.set('earlier', [])
  for (const conv of conversations) {
    const date = new Date(conv.last_user_message_at || conv.updated_at)
    const key = getTimeCategoryKey(date)
    if (!categories.has(key)) categories.set(key, [])
    categories.get(key)!.push(conv)
  }
  return categories
}

const ungroupedTimeCategories = computed(() => {
  const ungrouped = chatStore.conversations.filter(c => !c.group_id)
  const categories = categorizeConversationsByTime(ungrouped)
  const result: { key: string; label: string; conversations: Conversation[] }[] = []
  const categoryOrder: Record<string, number> = { recent: 0, month: 1, earlier: 2 }
  const sortedKeys = Array.from(categories.keys()).sort((a, b) => {
    return (categoryOrder[a] ?? 99) - (categoryOrder[b] ?? 99)
  })
  for (const key of sortedKeys) {
    const convs = categories.get(key)!
    if (convs.length > 0) result.push({ key, label: getTimeCategoryLabel(key), conversations: convs })
  }
  return result
})

const convDragGroup = { name: 'zen-conversations', pull: true, put: true }

function syncDragLists() {
  if (isDragging.value) return
  emptyUngroupedList.value = []
  const validKeys = new Set<string>()
  const ungrouped = chatStore.conversations.filter(c => !c.group_id)
  const timeCategories = categorizeConversationsByTime(ungrouped)
  for (const [key, convs] of timeCategories) {
    dragLists[key] = convs.slice()
    validKeys.add(key)
  }
  for (const group of groupStore.groups) {
    dragLists[group.id] = chatStore.conversations.filter(c => c.group_id === group.id).slice()
    validKeys.add(group.id)
  }
  for (const key of Object.keys(dragLists)) {
    if (!validKeys.has(key)) delete dragLists[key]
  }
}

function getGroupConvCount(groupId: string): number {
  return (dragLists[groupId] || []).length
}

// Load data
watch(() => props.visible, async (v) => {
  if (v) {
    if (!chatStore.conversations.length) {
      loadingConversations.value = true
      try {
        // 助手列表每次抽屉打开都重载（Sidebar 模式下由 Sidebar 负责；工作台无 Sidebar）。
        // 不可只在空列表时加载：陈旧列表会把已删除助手误判为有效（wave-2 C5 实证）。
        await assistantStore.loadAssistants()
        ensureCurrentAssistant()
        await Promise.all([
          chatStore.loadConversations(assistantStore.currentAssistantId),
          groupStore.loadGroups(assistantStore.currentAssistantId || undefined)
        ])
        syncDragLists()
      } finally {
        loadingConversations.value = false
      }
    } else {
      await assistantStore.loadAssistants()
      ensureCurrentAssistant()
      syncDragLists()
    }
    // Re-apply a pending search-jump reveal: the drawer unmounts (v-if) on
    // 'selectConversation', so the focus scroll is replayed on next open.
    if (pendingRevealConversationId) {
      const cid = pendingRevealConversationId
      pendingRevealConversationId = null
      await revealConversationInSidebar(cid)
    }
  } else {
    historySearchQuery.value = ''
    chatStore.searchResults = []
    exitSelectionMode()
    cancelEditTitle()
    closeSwipeActions()
    closeAllMenus()
  }
})

watch(
  [() => chatStore.conversations, () => groupStore.groups],
  () => { nextTick(() => syncDragLists()) },
  { deep: true }
)

// Conversation actions
function highlightKeyword(text: string, keyword: string): string {
  if (!keyword.trim()) return text
  const escaped = keyword.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return text.replace(new RegExp(`(${escaped})`, 'gi'), '<mark>$1</mark>')
}

async function handleNewChat() {
  // Look for any existing conversation that has no messages and the default title.
  // This prevents creating duplicate empty sessions.
  const emptyConv = chatStore.conversations.find(c => {
    const msgs = chatStore.messages[c.id]
    return (!msgs || msgs.length === 0) && c.title === '新对话'
  })
  if (emptyConv) {
    await chatStore.selectConversation(emptyConv.id)
    emit('close')
    return
  }
  await chatStore.createConversation(undefined, assistantStore.currentAssistantId)
  emit('close')
}

async function handleSelectConversation(id: string) {
  closeSwipeActions()
  await chatStore.selectConversation(id)
  emit('selectConversation', id)
  emit('close')
}

async function handleConversationClick(id: string) {
  if (Date.now() < suppressConversationClickUntil || Date.now() < suppressClickAfterMarqueeUntil) return
  if (selectionModeActive.value) { toggleSelect(id); return }
  if (swipedConversationId.value === id) { closeSwipeActions(); return }
  await handleSelectConversation(id)
}

// Title editing
function startEditTitle(conv: { id: string; title: string }) {
  closeAllMenus()
  editingTitleId.value = conv.id
  editingTitle.value = conv.title || ''
}

async function saveTitle(id: string) {
  const title = editingTitle.value.trim()
  if (!title) { cancelEditTitle(); return }
  if (title !== (chatStore.conversations.find(c => c.id === id)?.title || '')) {
    await chatStore.updateConversationTitle(id, title)
  }
  editingTitleId.value = null
  editingTitle.value = ''
}

function updateTitle(value: string) {
  editingTitle.value = value
}

function handleSwipeMoveGroup(convId: string) {
  const conv = chatStore.conversations.find(c => c.id === convId)
  if (!conv) return
  moveTargetConv.value = { id: conv.id, title: conv.title || '新对话', group_id: conv.group_id }
  closeSwipeActions()
  const MENU_WIDTH = 180
  const left = Math.max(8, (window.innerWidth - MENU_WIDTH) / 2)
  moveMenuStyle.value = { top: '40%', left: `${left}px` }
  showMoveToGroupMenu.value = true
}

function cancelEditTitle() {
  editingTitleId.value = null
  editingTitle.value = ''
}

// Context menu
function toggleMenu(id: string, event: MouseEvent) {
  if (activeMenuId.value === id) { closeAllMenus(); return }
  closeSwipeActions()
  const conv = chatStore.conversations.find(c => c.id === id)
  if (conv) menuTargetConv.value = { id: conv.id, title: conv.title || '', group_id: conv.group_id }
  activeMenuId.value = id
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const MENU_WIDTH = 160
  const PADDING = 8
  let top = rect.bottom + 2
  let left = rect.right - MENU_WIDTH
  if (left < PADDING) left = PADDING
  if (left + MENU_WIDTH > window.innerWidth - PADDING) left = window.innerWidth - MENU_WIDTH - PADDING
  menuStyle.value = { top: `${top}px`, left: `${left}px` }
  nextTick(() => {
    const menuEl = document.querySelector('.conversation-menu') as HTMLElement
    if (!menuEl) return
    const menuRect = menuEl.getBoundingClientRect()
    if (menuRect.height > window.innerHeight - rect.bottom - PADDING && rect.top > window.innerHeight - rect.bottom) {
      let newTop = rect.top - menuRect.height - 2
      if (newTop < PADDING) newTop = PADDING
      menuStyle.value = { top: `${newTop}px`, left: `${left}px` }
    }
  })
}

function closeAllMenus() {
  activeMenuId.value = null
  menuTargetConv.value = null
  showMoveToGroupMenu.value = false
  moveTargetConv.value = null
  asstMenuOpen.value = false
  closeSwipeActions()
}

// ===== 助手切换（工作台 Agent 侧栏）=====
// 与 Sidebar.vue 选择助手同语义：selectAssistant 落 localStorage + 跨标签同步，
// watch currentAssistantId 后刷新会话/分组列表（外部来源的切换同样生效）。
const asstMenuOpen = ref(false)

const currentAssistantName = computed(() => {
  const a = assistantStore.assistants.find((x) => x.id === assistantStore.currentAssistantId)
  return a ? a.name : '选择助手'
})

function selectAssistantFromDrawer(id: string) {
  asstMenuOpen.value = false
  if (id === assistantStore.currentAssistantId) return
  assistantStore.selectAssistant(id)
}

/**
 * 工作台模式没有 Sidebar 的 onMounted 默认选中逻辑：currentAssistantId 为空或失效时
 * 补一次（优先「默认助手」）。仅补空缺，不覆盖用户已选 —— 抽屉重开不重置。
 * 自身触发的切换经 asstWatchSuppressed 免掉 id-watch 的二次拉取（A4.9 M9）。
 */
let asstWatchSuppressed = false

function ensureCurrentAssistant() {
  const list = assistantStore.assistants
  if (!list.length) return
  const cur = assistantStore.currentAssistantId
  if (cur && list.some((a) => a.id === cur)) return
  const fallback = list.find((a) => a.name === '默认助手') || list[0]
  asstWatchSuppressed = true
  assistantStore.selectAssistant(fallback.id)
  nextTick(() => { asstWatchSuppressed = false })
}

watch(() => assistantStore.currentAssistantId, async () => {
  if (!props.visible || asstWatchSuppressed) return
  loadingConversations.value = true
  try {
    await Promise.all([
      chatStore.loadConversations(assistantStore.currentAssistantId),
      groupStore.loadGroups(assistantStore.currentAssistantId || undefined),
    ])
    syncDragLists()
  } finally {
    loadingConversations.value = false
  }
})

async function handleMenuEdit() {
  if (!menuTargetConv.value) return
  const conv = menuTargetConv.value
  closeAllMenus()
  startEditTitle(conv)
}

async function handleMenuExport() {
  if (!menuTargetConv.value || !assistantStore.currentAssistantId) return
  const convId = menuTargetConv.value.id
  closeAllMenus()
  try {
    const result = await chatApi.exportConversations(assistantStore.currentAssistantId, [convId])
    showToast(result.success ? (result.path ? `已保存到 ${result.path}` : '导出成功') : (result.error || '导出失败'), result.success ? 'success' : 'error')
  } catch { showToast('导出失败', 'error') }
}

async function handleMenuDelete() {
  if (!menuTargetConv.value) return
  const convId = menuTargetConv.value.id
  closeAllMenus()
  if (!await showConfirm({ message: '确定要删除这条对话吗？', danger: true, confirmText: '删除' })) return
  await chatStore.deleteConversation(convId)
}

// Swipe actions
function closeSwipeActions() {
  swipedConversationId.value = null; swipeOffset.value = 0; swipeTrackingId.value = null
  isSwipeTracking.value = false; isSwipeDragging.value = false; swipeStartOffset.value = 0
}

function handleTouchStart(e: TouchEvent, conversationId: string) {
  if (selectionModeActive.value || editingTitleId.value || e.touches.length !== 1) return
  const touch = e.touches[0]
  swipeTrackingId.value = conversationId; swipeStartX.value = touch.clientX; swipeStartY.value = touch.clientY
  swipeStartOffset.value = swipedConversationId.value === conversationId ? swipeOffset.value : 0
  isSwipeTracking.value = true; isSwipeDragging.value = false
  if (swipedConversationId.value && swipedConversationId.value !== conversationId) {
    swipedConversationId.value = null; swipeOffset.value = 0
  }
}

function handleTouchEnd() {
  if (!isSwipeTracking.value) return
  if (isSwipeDragging.value) {
    suppressConversationClickUntil = Date.now() + 300
    if (swipeOffset.value <= -SWIPE_ACTION_WIDTH / 2 && swipeTrackingId.value) {
      swipedConversationId.value = swipeTrackingId.value; swipeOffset.value = -SWIPE_ACTION_WIDTH
    } else { closeSwipeActions(); return }
  }
  swipeTrackingId.value = null; isSwipeTracking.value = false; isSwipeDragging.value = false
}

function handleTouchMove(e: TouchEvent, conversationId: string) {
  if (selectionModeActive.value || !isSwipeTracking.value || swipeTrackingId.value !== conversationId || e.touches.length !== 1) return
  const touch = e.touches[0]
  const deltaX = touch.clientX - swipeStartX.value; const deltaY = touch.clientY - swipeStartY.value
  if (!isSwipeDragging.value) {
    if (Math.abs(deltaY) > 10 && Math.abs(deltaY) > Math.abs(deltaX)) { swipeTrackingId.value = null; isSwipeTracking.value = false; return }
    if (Math.abs(deltaX) < 10) return
    if (deltaX > 0 && swipeStartOffset.value === 0) { swipeTrackingId.value = null; isSwipeTracking.value = false; return }
    isSwipeDragging.value = true
  }
  e.preventDefault()
  swipedConversationId.value = conversationId
  swipeOffset.value = Math.max(-SWIPE_ACTION_WIDTH, Math.min(0, swipeStartOffset.value + deltaX))
}

function handleSwipeEdit(id: string, title: string) { closeSwipeActions(); startEditTitle({ id, title }) }
async function handleSwipeExport(id: string) {
  closeSwipeActions()
  if (!assistantStore.currentAssistantId) return
  try {
    const result = await chatApi.exportConversations(assistantStore.currentAssistantId, [id])
    showToast(result.success ? (result.path ? `已保存到 ${result.path}` : '导出成功') : (result.error || '导出失败'), result.success ? 'success' : 'error')
  } catch { showToast('导出失败', 'error') }
}
async function handleSwipeDelete(id: string) {
  closeSwipeActions()
  if (!await showConfirm({ message: '确定要删除这条对话吗？', danger: true, confirmText: '删除' })) return
  await chatStore.deleteConversation(id)
}

function handleSwipeSaveToNote(convId: string, convTitle: string) {
  closeSwipeActions(); saveToNoteConvId.value = convId; saveToNoteConvTitle.value = convTitle
  notesStore.loadNotebooks(); showNotebookPicker.value = true
}

function handleMenuSaveToNote() {
  if (!menuTargetConv.value) return
  const conv = menuTargetConv.value; closeAllMenus()
  saveToNoteConvId.value = conv.id; saveToNoteConvTitle.value = conv.title || '新对话'
  notesStore.loadNotebooks(); showNotebookPicker.value = true
}

async function handleNotebookSelected(notebookId: string) {
  showNotebookPicker.value = false
  const convId = saveToNoteConvId.value; if (!convId) return
  try {
    const conversation = await chatApi.getConversation(convId)
    const msgs = conversation.messages || []
    if (msgs.length === 0) { showToast('对话无消息内容', 'error'); return }
    const content = msgs.map((m: any) => {
      const role = m.role === 'user' ? '**用户**' : '**助手**'
      let text = m.content || ''
      if (m.role === 'assistant' && m.tool_results) {
        try {
          const tr = JSON.parse(m.tool_results)
          const attachments = (tr?.attachments ?? []) as Array<{ name?: string; path?: string; type?: string }>
          const imageAttachments = attachments.filter((a: any) => a.type === 'image' && a.path)
          if (imageAttachments.length > 0) {
            text += '\n\n' + imageAttachments.map((a: any) => `![${a.name || 'image'}](${a.path})`).join('\n')
          }
        } catch { /* ignore */ }
      }
      return `${role}\n\n${text}`
    }).join('\n\n---\n\n')
    await notesStore.createNote(notebookId, { title: saveToNoteConvTitle.value || '对话记录', content })
    showToast('已添加到笔记', 'success')
  } catch { showToast('添加到笔记失败', 'error') }
  saveToNoteConvId.value = null; saveToNoteConvTitle.value = ''
}

// Selection mode
function enterExportMode() { closeAllMenus(); cancelEditTitle(); selectionMode.value = 'export'; selectedConversationIds.value = new Set(); selectedGroupIds.value = new Set(); selectionProgress.value = '' }
function enterDeleteMode() { closeAllMenus(); cancelEditTitle(); selectionMode.value = 'delete'; selectedConversationIds.value = new Set(); selectedGroupIds.value = new Set(); selectionProgress.value = ''; showBulkDeleteDialog.value = false; bulkDeleteConfirmMessage.value = ''; bulkDeleteWithConversations.value = false; bulkDeleteContext.value = 'selection' }
function exitSelectionMode() { closeSwipeActions(); selectionMode.value = null; selectedConversationIds.value = new Set(); selectedGroupIds.value = new Set(); selectionPending.value = false; selectionProgress.value = '' }
function toggleSelectAll() { selectedConversationIds.value = selectedConversationIds.value.size === chatStore.conversations.length ? new Set() : new Set(chatStore.conversations.map(c => c.id)) }
function toggleSelect(id: string) { const s = new Set(selectedConversationIds.value); s.has(id) ? s.delete(id) : s.add(id); selectedConversationIds.value = s }
function toggleGroupSelect(groupId: string) { const s = new Set(selectedGroupIds.value); s.has(groupId) ? s.delete(groupId) : s.add(groupId); selectedGroupIds.value = s }

async function handleSelectionConfirm() {
  if (isExportMode.value) { await handleExport(); return }
  if (isDeleteMode.value) { await handleBulkDelete() }
}

async function handleExport() {
  if (selectedConversationIds.value.size === 0 || !assistantStore.currentAssistantId) return
  selectionPending.value = true; selectionProgress.value = '正在导出...'
  try {
    const result = await chatApi.exportConversations(assistantStore.currentAssistantId, Array.from(selectedConversationIds.value))
    selectionProgress.value = result.success ? (result.path ? `已保存到 ${result.path}` : '导出成功！') : (result.error || '导出失败')
    setTimeout(() => exitSelectionMode(), 1200)
  } catch { selectionProgress.value = '导出失败' } finally { selectionPending.value = false }
}

async function handleBulkDelete() {
  if (!hasAnySelection.value) return
  const convCount = selectedConversationIds.value.size; const groupCount = selectedGroupIds.value.size
  let msg = '确定要删除选中的'
  if (convCount > 0 && groupCount > 0) msg += `${convCount}个对话和${groupCount}个分组吗？`
  else if (convCount > 0) msg += `${convCount}个对话吗？`
  else msg += `${groupCount}个分组吗？`
  bulkDeleteConfirmMessage.value = msg; bulkDeleteWithConversations.value = false; showBulkDeleteDialog.value = true
}

async function confirmBulkDelete() {
  if (bulkDeleteContext.value === 'batchPopup') {
    await executeBatchPopupDelete()
    return
  }
  showBulkDeleteDialog.value = false; selectionPending.value = true; selectionProgress.value = '正在删除...'
  try {
    if (bulkDeleteWithConversations.value && selectedGroupIds.value.size > 0) {
      const gids = Array.from(selectedGroupIds.value)
      const convsInGroups = chatStore.conversations.filter(c => c.group_id && selectedGroupIds.value.has(c.group_id))
      const allConvIds = new Set([...selectedConversationIds.value, ...convsInGroups.map(c => c.id)])
      if (allConvIds.size > 0) {
        const ids = Array.from(allConvIds)
        const currentId = chatStore.currentConversationId
        await chatStore.bulkDeleteConversations(ids)
        if (currentId && ids.includes(currentId) && route.path === '/zen') {
          await navigateWithMobileHistory(router, '/zen', { replace: true })
        }
      }
      await groupStore.bulkDeleteGroups(gids, false)
      for (const gid of gids) chatStore.conversations = chatStore.conversations.filter(c => c.group_id !== gid)
    } else {
      if (selectedConversationIds.value.size > 0) {
        const ids = Array.from(selectedConversationIds.value)
        const currentId = chatStore.currentConversationId
        await chatStore.bulkDeleteConversations(ids)
        if (currentId && ids.includes(currentId) && route.path === '/zen') {
          await navigateWithMobileHistory(router, '/zen', { replace: true })
        }
      }
      if (selectedGroupIds.value.size > 0) {
        await groupStore.bulkDeleteGroups(Array.from(selectedGroupIds.value), false)
        for (const gid of selectedGroupIds.value) for (const c of chatStore.conversations) if (c.group_id === gid) c.group_id = null
      }
    }
    selectionProgress.value = '删除成功！'; setTimeout(() => exitSelectionMode(), 1200)
  } catch { selectionProgress.value = '删除失败' } finally { selectionPending.value = false }
}

// Group management
function openCreateGroupDialog() { editingGroup.value = null; newGroupName.value = ''; newGroupColor.value = '#3b82f6'; showGroupDialog.value = true; closeAllMenus() }
function openEditGroupDialog(group: ConversationGroup) { editingGroup.value = group; newGroupName.value = group.name; newGroupColor.value = group.color; showGroupDialog.value = true; closeAllMenus() }

async function saveGroup() {
  const name = newGroupName.value.trim()
  if (!name) { showToast('分组名称不能为空', 'error'); return }
  try {
    if (editingGroup.value) { await groupStore.updateGroup(editingGroup.value.id, { name, color: newGroupColor.value }); showToast('分组已更新', 'success') }
    else { await groupStore.createGroup(name, newGroupColor.value, assistantStore.currentAssistantId); showToast('分组已创建', 'success') }
    showGroupDialog.value = false
  } catch { showToast('保存失败', 'error') }
}

function openDeleteGroupDialog(group: ConversationGroup) { deletingGroup.value = group; deleteGroupWithConversations.value = false; showDeleteGroupDialog.value = true; closeAllMenus() }

async function confirmDeleteGroup() {
  if (!deletingGroup.value) return
  const groupId = deletingGroup.value.id; const shouldDelete = deleteGroupWithConversations.value
  try {
    await groupStore.deleteGroup(groupId, shouldDelete)
    if (shouldDelete) chatStore.conversations = chatStore.conversations.filter(c => c.group_id !== groupId)
    else for (const c of chatStore.conversations) if (c.group_id === groupId) c.group_id = null
    showToast('分组已删除', 'success'); showDeleteGroupDialog.value = false; deletingGroup.value = null
  } catch { showToast('删除失败', 'error') }
}

// Move to group
function openMoveToGroupMenu(event: MouseEvent) {
  if (!menuTargetConv.value) return
  moveTargetConv.value = menuTargetConv.value
  const target = event.currentTarget as HTMLElement; const rect = target.getBoundingClientRect()
  const SUB_W = 180; const PAD = 8; const GAP = 4
  let top = rect.top; let left = rect.right + GAP
  if (left + SUB_W > window.innerWidth - PAD) { left = rect.left - SUB_W - GAP; if (left < PAD) left = PAD }
  moveMenuStyle.value = { top: `${top}px`, left: `${left}px` }; showMoveToGroupMenu.value = true
  nextTick(() => {
    const el = document.querySelector('.move-group-menu') as HTMLElement; if (!el) return
    if (el.getBoundingClientRect().bottom > window.innerHeight - PAD) {
      let newTop = window.innerHeight - el.getBoundingClientRect().height - PAD; if (newTop < PAD) newTop = PAD
      moveMenuStyle.value = { top: `${newTop}px`, left: `${left}px` }
    }
  })
}

async function moveConversationToGroup(convId: string, groupId: string | null) {
  try {
    await groupStore.moveConversation(convId, groupId)
    const conv = chatStore.conversations.find(c => c.id === convId); if (conv) conv.group_id = groupId
    showMoveToGroupMenu.value = false; closeAllMenus(); showToast(groupId ? '已移动到分组' : '已移出分组', 'success')
    nextTick(() => syncDragLists())
  } catch { showToast('移动失败', 'error') }
}

async function removeConversationFromGroup(convId: string) { await moveConversationToGroup(convId, null) }

// Drag & Drop
function handleGroupHeaderClick(groupId: string) {
  if (isDeleteMode.value) {
    toggleGroupSelect(groupId)
    return
  }
  groupStore.toggleGroupCollapse(groupId)
}

const collapsedBeforeDrag = ref<Set<string>>(new Set())

function onDragStart(event: any) {
  isDragging.value = true
  const fromEl = event?.from as HTMLElement | undefined
  const isGroupDrag = fromEl?.classList?.contains('groups-directory-content') || fromEl?.closest('.groups-directory-content') !== null
  if (isGroupDrag) {
    collapsedBeforeDrag.value = new Set(groupStore.collapsedGroups)
    for (const g of groupStore.groups) if (groupStore.isGroupCollapsed(g.id)) groupStore.expandGroup(g.id)
  } else {
    collapsedBeforeDrag.value = new Set(groupStore.collapsedGroups)
    const draggedItemKey = event?.item?.getAttribute('data-key')
    const sourceGroup = groupStore.groups.find(g => {
      const convs = dragLists[g.id] || []
      return convs.some(c => c.id === draggedItemKey)
    })
    if (sourceGroup && groupStore.isGroupCollapsed(sourceGroup.id)) {
      groupStore.expandGroup(sourceGroup.id)
    }
    groupsDirectoryWasCollapsed.value = groupsDirectoryCollapsed.value
    if (groupsDirectoryCollapsed.value) groupsDirectoryCollapsed.value = false
    startEdgeScroll()
    document.addEventListener('mousemove', handleDragMouseMove)
    document.addEventListener('touchmove', handleDragMouseMove, { passive: true })
  }
}

function onConversationDragEnd(event: any) {
  const toEl = event?.to as HTMLElement | undefined
  const targetGroupEl = toEl?.closest('.conversation-group') as HTMLElement | null
  const targetGroupId = targetGroupEl?.getAttribute('data-group-id') || null

  nextTick(() => {
    reconcileDragState(); processDragChanges()
    nextTick(() => {
      deduplicateDragElements(); isDragging.value = false
      if (groupsDirectoryWasCollapsed.value) groupsDirectoryCollapsed.value = true
      groupsDirectoryWasCollapsed.value = false

      for (const group of groupStore.groups) {
        const wasCollapsedBefore = collapsedBeforeDrag.value.has(group.id)
        if (group.id === targetGroupId) {
          groupStore.expandGroup(group.id)
        } else if (wasCollapsedBefore && !groupStore.isGroupCollapsed(group.id)) {
          groupStore.collapseGroup(group.id)
        }
      }
      collapsedBeforeDrag.value = new Set()
      stopEdgeScroll()
      document.removeEventListener('mousemove', handleDragMouseMove); document.removeEventListener('touchmove', handleDragMouseMove)
      if (zenDragHoverTimer) { clearTimeout(zenDragHoverTimer); zenDragHoverTimer = null }
      zenDragHoverTargetGroupId = null
      dragExpandedGroupId.value = null
    })
  })
}

function deduplicateDragElements() {
  const containerElements = new Map<Element, Map<string, Element>>()
  document.querySelectorAll('[data-draggable="true"][data-key]:not(.sortable-ghost):not(.sortable-fallback)').forEach(el => {
    const key = el.getAttribute('data-key'); if (!key) return
    const parent = el.parentElement; if (!parent) return
    if (!containerElements.has(parent)) containerElements.set(parent, new Map())
    const containerMap = containerElements.get(parent)!
    if (containerMap.has(key)) el.remove(); else containerMap.set(key, el)
  })
}

function reconcileDragState() {
  const groupIdByConvId = new Map<string, string | null>()
  for (const group of groupStore.groups) {
    const container = document.querySelector(`[data-group-id="${group.id}"] .group-conv-drag-list`)
    if (container) container.querySelectorAll('[data-draggable="true"][data-key]:not(.sortable-ghost):not(.sortable-fallback)').forEach(el => {
      const cid = el.getAttribute('data-key'); if (cid) groupIdByConvId.set(cid, group.id)
    })
  }
  document.querySelectorAll('.ungrouped-conv-drag-list').forEach(container => {
    container.querySelectorAll('[data-draggable="true"][data-key]:not(.sortable-ghost):not(.sortable-fallback)').forEach(el => {
      const cid = el.getAttribute('data-key'); if (cid && !groupIdByConvId.has(cid)) groupIdByConvId.set(cid, null)
    })
  })
  const emptyZone = document.querySelector('.ungrouped-empty-drag-list')
  if (emptyZone) emptyZone.querySelectorAll('[data-draggable="true"][data-key]:not(.sortable-ghost):not(.sortable-fallback)').forEach(el => {
    const cid = el.getAttribute('data-key'); if (cid) groupIdByConvId.set(cid, null)
  })
  for (const conv of chatStore.conversations) { const gid = groupIdByConvId.get(conv.id); if (gid !== undefined) conv.group_id = gid }
  const groupIds = new Set(groupStore.groups.map(g => g.id)); const ungroupedConvs: Conversation[] = []
  for (const group of groupStore.groups) dragLists[group.id] = []
  for (const conv of chatStore.conversations) {
    const gid = groupIdByConvId.get(conv.id) ?? conv.group_id
    if (gid && groupIds.has(gid)) { conv.group_id = gid; dragLists[gid].push(conv) }
    else { conv.group_id = null; ungroupedConvs.push(conv) }
  }
  const tc = categorizeConversationsByTime(ungroupedConvs)
  for (const [key, convs] of tc) dragLists[key] = convs
  emptyUngroupedList.value = []
}

function onGroupDragEnd(event: any) {
  isDragging.value = false
  for (const gid of collapsedBeforeDrag.value) groupStore.collapseGroup(gid)
  collapsedBeforeDrag.value = new Set(); dragExpandedGroupId.value = null
  if (!event.moved) return
  groupStore.reorderGroups(groupStore.groups.map((g, i) => ({ id: g.id, sort_order: i })))
}

function processDragChanges() {
  const items: { id: string; sort_order: number; group_id: string | null }[] = []
  const groupIds = new Set(groupStore.groups.map(g => g.id))
  for (const conv of emptyUngroupedList.value) { conv.group_id = null; items.push({ id: conv.id, sort_order: items.length, group_id: null }) }
  for (const [key, convs] of Object.entries(dragLists)) {
    if (!Array.isArray(convs) || convs.length === 0) continue
    const isGroup = groupIds.has(key)
    for (let i = 0; i < convs.length; i++) {
      const conv = convs[i]
      if (isGroup) { conv.group_id = key; conv.sort_order = i; items.push({ id: conv.id, sort_order: i, group_id: key }) }
      else { conv.group_id = null; items.push({ id: conv.id, sort_order: items.length, group_id: null }) }
    }
  }
  if (items.length > 0) {
    const itemMap = new Map(items.map(it => [it.id, it.group_id]))
    for (const conv of chatStore.conversations) { const ng = itemMap.get(conv.id); if (ng !== undefined) conv.group_id = ng }
    groupStore.reorderConversations(items).then(() => nextTick(() => syncDragLists())).catch(e => console.error('Failed to reorder:', e))
  }
}

function handleDragMouseMove(e: MouseEvent | TouchEvent) {
  if (!isDragging.value) return
  const cx = 'touches' in e ? e.touches[0].clientX : e.clientX
  const cy = 'touches' in e ? e.touches[0].clientY : e.clientY
  
  // First check group headers (always visible, even when collapsed)
  let foundGroupId: string | null = null
  const groupHeaders = document.querySelectorAll('.group-header')
  for (const header of groupHeaders) {
    const rect = header.getBoundingClientRect()
    if (cx >= rect.left && cx <= rect.right && cy >= rect.top && cy <= rect.bottom) {
      const groupEl = header.closest('.conversation-group')
      if (groupEl) {
        foundGroupId = groupEl.getAttribute('data-group-id')
      }
      break
    }
  }
  
  // If not over a header, check if over the currently expanded group's content area
  if (!foundGroupId && dragExpandedGroupId.value) {
    const expandedGroup = document.querySelector(`[data-group-id="${dragExpandedGroupId.value}"]`)
    if (expandedGroup) {
      const conversationsEl = expandedGroup.querySelector('.group-conversations')
      if (conversationsEl) {
        const rect = conversationsEl.getBoundingClientRect()
        if (rect.height > 0 && cx >= rect.left && cx <= rect.right && cy >= rect.top && cy <= rect.bottom) {
          foundGroupId = dragExpandedGroupId.value
        }
      }
    }
  }
  
  if (foundGroupId) {
    if (zenDragHoverTargetGroupId !== foundGroupId) {
      if (zenDragHoverTimer) { clearTimeout(zenDragHoverTimer); zenDragHoverTimer = null }
      zenDragHoverTargetGroupId = foundGroupId
      if (dragExpandedGroupId.value === foundGroupId) return
      zenDragHoverTimer = setTimeout(() => {
        if (zenDragHoverTargetGroupId === foundGroupId) {
          dragExpandedGroupId.value = foundGroupId
          // 悬停展开折叠组：强制 SortableList 重初始化，使新展开的内层列表可接收拖放
          forceReinitCounter.value++
        }
        zenDragHoverTimer = null
      }, 200)
    }
  } else {
    if (zenDragHoverTimer) { clearTimeout(zenDragHoverTimer); zenDragHoverTimer = null }
    zenDragHoverTargetGroupId = null
    dragExpandedGroupId.value = null
  }
}

let zenDragHoverTimer: ReturnType<typeof setTimeout> | null = null
let zenDragHoverTargetGroupId: string | null = null

// Edge scroll
function trackPointerPosition(e: MouseEvent | TouchEvent) {
  if ('touches' in e && e.touches.length > 0) lastPointerPosition.value = { x: e.touches[0].clientX, y: e.touches[0].clientY }
  else if (!('touches' in e)) lastPointerPosition.value = { x: e.clientX, y: e.clientY }
}

function startEdgeScroll() {
  if (edgeScrollRaf) return; lastPointerPosition.value = null
  document.addEventListener('mousemove', trackPointerPosition); document.addEventListener('touchmove', trackPointerPosition, { passive: true })
  const step = () => {
    if (!conversationListEl.value || !lastPointerPosition.value || (!isDragging.value && !isMarqueeSelecting.value)) { stopEdgeScroll(); return }
    const rect = conversationListEl.value.getBoundingClientRect(); const { y } = lastPointerPosition.value
    const topDist = y - rect.top; const bottomDist = rect.bottom - y
    if (topDist >= 0 && topDist < EDGE_SCROLL_THRESHOLD) conversationListEl.value.scrollTop -= Math.max(2, EDGE_SCROLL_SPEED * (1 - topDist / EDGE_SCROLL_THRESHOLD))
    else if (bottomDist >= 0 && bottomDist < EDGE_SCROLL_THRESHOLD) conversationListEl.value.scrollTop += Math.max(2, EDGE_SCROLL_SPEED * (1 - bottomDist / EDGE_SCROLL_THRESHOLD))
    edgeScrollRaf = requestAnimationFrame(step)
  }
  edgeScrollRaf = requestAnimationFrame(step)
}

function stopEdgeScroll() {
  if (edgeScrollRaf) { cancelAnimationFrame(edgeScrollRaf); edgeScrollRaf = null }
  document.removeEventListener('mousemove', trackPointerPosition); document.removeEventListener('touchmove', trackPointerPosition)
  lastPointerPosition.value = null
}

// Marquee selection
function onConversationListMouseDown(e: MouseEvent) {
  if (!selectionModeActive.value || e.button !== 0) return
  const target = e.target as HTMLElement
  if (target.closest('input[type="checkbox"]') || target.closest('button') || target.closest('.group-header') || target.closest('.conversation-menu') || target.closest('.time-category-header')) return
  if (!conversationListEl.value) return
  const listRect = conversationListEl.value.getBoundingClientRect()
  if (e.clientX < listRect.left || e.clientX > listRect.right || e.clientY < listRect.top || e.clientY > listRect.bottom) return
  marqueeAnchor.value = { x: e.clientX, y: e.clientY }; marqueeEnd.value = null
  marqueePreviousSelection.value = new Set(selectedConversationIds.value)
  document.addEventListener('mousemove', onMarqueeMouseMove); document.addEventListener('mouseup', onMarqueeMouseUp)
  e.preventDefault()
}

function onMarqueeMouseMove(e: MouseEvent) {
  if (!marqueeAnchor.value || !conversationListEl.value) return
  const dx = e.clientX - marqueeAnchor.value.x; const dy = e.clientY - marqueeAnchor.value.y
  if (!isMarqueeSelecting.value && Math.sqrt(dx * dx + dy * dy) < 5) return
  if (!isMarqueeSelecting.value) { isMarqueeSelecting.value = true; startEdgeScroll() }
  marqueeEnd.value = { x: e.clientX, y: e.clientY }
  const left = Math.min(marqueeAnchor.value.x, e.clientX); const top = Math.min(marqueeAnchor.value.y, e.clientY)
  const right = Math.max(marqueeAnchor.value.x, e.clientX); const bottom = Math.max(marqueeAnchor.value.y, e.clientY)
  const rows = conversationListEl.value.querySelectorAll('.conversation-row')
  const newlySelected = new Set<string>()
  const useMod = e.shiftKey || e.ctrlKey || e.metaKey
  if (useMod) for (const id of marqueePreviousSelection.value) newlySelected.add(id)
  for (const row of rows) {
    const rRect = row.getBoundingClientRect(); const id = row.querySelector('.conversation-item')?.getAttribute('data-id'); if (!id) continue
    if (rRect.left < right && rRect.right > left && rRect.top < bottom && rRect.bottom > top) {
      if (useMod && marqueePreviousSelection.value.has(id)) newlySelected.delete(id); else newlySelected.add(id)
    }
  }
  selectedConversationIds.value = newlySelected
}

function onMarqueeMouseUp() {
  if (isMarqueeSelecting.value) { stopEdgeScroll(); suppressClickAfterMarqueeUntil = Date.now() + 50 }
  isMarqueeSelecting.value = false; marqueeAnchor.value = null; marqueeEnd.value = null
  document.removeEventListener('mousemove', onMarqueeMouseMove); document.removeEventListener('mouseup', onMarqueeMouseUp)
}

onMounted(() => { document.addEventListener('click', closeAllMenus) })
onUnmounted(() => { document.removeEventListener('click', closeAllMenus); stopEdgeScroll() })
</script>

<style scoped>
.zen-history-drawer-overlay {
  position: fixed; inset: 0; z-index: 250; background: rgba(0, 0, 0, 0.3);
}
.zen-history-drawer {
  position: fixed; top: 0; left: 0; bottom: 0; width: 320px; z-index: 260;
  background-color: var(--surface-panel-strong); border-right: 1px solid var(--panel-border);
  box-shadow: 4px 0 24px rgba(90, 130, 60, 0.1); display: flex; flex-direction: column;
  animation: zenSlideInLeft 0.25s ease;
}
@keyframes zenSlideInLeft { from { transform: translateX(-100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

.drawer-header {
  display: flex; align-items: center; justify-content: space-between; padding: 12px 16px;
  border-bottom: 1px solid var(--panel-border); flex-shrink: 0;
}
.drawer-title { font-size: 15px; font-weight: 500; color: var(--color-text); }
.drawer-actions { display: flex; align-items: center; gap: 4px; }
.drawer-action-btn { padding: 6px; color: var(--color-text-light); border-radius: var(--radius-sm); transition: all var(--transition-fast); }
.drawer-action-btn:hover { background-color: var(--color-hover); color: var(--color-text); }
.drawer-close { padding: 6px; color: var(--color-text-light); border-radius: var(--radius-sm); transition: all var(--transition-fast); }
.drawer-close:hover { background-color: var(--color-hover); color: var(--color-text); }

.drawer-body { flex: 1; min-height: 0; overflow: hidden; display: flex; flex-direction: column; position: relative; }

:deep(.sortable-ghost) { opacity: 0.35; background-color: var(--color-hover); border-radius: var(--radius-sm); transition: opacity 0.15s ease; }
:deep(.sortable-chosen) { z-index: 10; }
:deep(.sortable-drag) { opacity: 0.9; box-shadow: 0 4px 16px rgba(90, 130, 60, 0.12); border-radius: var(--radius-sm); }
:deep(.sortable-fallback) { opacity: 0.8; box-shadow: 0 4px 16px rgba(90, 130, 60, 0.12); }
.batch-select-all { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--color-text); cursor: pointer; }

.drawer-action-btn.danger:hover { color: var(--color-error); }

/* ===== 助手切换（工作台 Agent 侧栏）===== */
.zen-asst-switcher { position: relative; padding: 8px 12px; border-bottom: 1px solid var(--panel-border); flex-shrink: 0; }
.zen-asst-trigger {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 7px 10px;
  border: 1px solid var(--panel-border); border-radius: var(--radius-md);
  background-color: var(--surface-panel); color: var(--color-text);
  font-size: 13px; cursor: pointer; transition: all var(--transition-fast);
}
.zen-asst-trigger:hover { background-color: var(--color-hover); border-color: var(--panel-border-strong); }
.zen-asst-name { flex: 1; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zen-asst-chevron { transition: transform var(--transition-fast); color: var(--color-text-light); flex-shrink: 0; }
.zen-asst-chevron.expanded { transform: rotate(180deg); }
.zen-asst-menu {
  position: absolute; top: calc(100% - 4px); left: 12px; right: 12px; z-index: 60;
  background-color: var(--surface-panel-strong); border: 1px solid var(--panel-border-strong);
  border-radius: var(--radius-md); box-shadow: var(--shadow-md);
  max-height: 240px; overflow-y: auto; padding: 4px;
}
.zen-asst-item {
  display: block; width: 100%; text-align: left; padding: 8px 12px;
  border: none; background: transparent; color: var(--color-text);
  font-size: 13px; border-radius: var(--radius-sm); cursor: pointer;
  transition: background-color var(--transition-fast);
}
.zen-asst-item:hover { background-color: var(--color-hover); }
.zen-asst-item.active { background-color: var(--color-hover); font-weight: 600; }
.zen-asst-empty { padding: 10px 12px; font-size: 12px; color: var(--color-text-light); }
</style>

<style scoped src="../styles/sidebar-panels.css"></style>
