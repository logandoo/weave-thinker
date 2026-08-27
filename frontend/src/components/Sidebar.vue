<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="sidebar" :class="{ 'sidebar-dragging': isDragging }" role="navigation" aria-label="侧边栏导航">
    <div class="sidebar-header">
      <div class="logo">
        <LogoIcon :size="28" class="logo-icon" />
        <span class="logo-text">Weave Thinker</span>
      </div>
      <button class="close-drawer-btn hide-on-desktop" @click="$emit('close-drawer')" aria-label="关闭侧边栏">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18"/>
          <line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>

    <div class="sidebar-nav hide-on-mobile">
      <button
        class="nav-tab"
        :class="{ active: !isOnNotesPage && !isOnZenPage && !isOnVoicePage }"
        @click="navToChat"
        :aria-current="!isOnNotesPage && !isOnZenPage && !isOnVoicePage ? 'page' : undefined"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
        </svg>
        Agent
      </button>
      <button
        class="nav-tab"
        :class="{ active: isOnNotesPage }"
        @click="navToNotes"
        :aria-current="isOnNotesPage ? 'page' : undefined"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
        笔记
      </button>
      <button
        class="nav-tab"
        :class="{ active: isOnZenPage }"
        @click="navToZen"
        :aria-current="isOnZenPage ? 'page' : undefined"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M2 8h20"/>
          <path d="M2 11h20"/>
          <path d="M5 11v9"/>
          <path d="M19 11v9"/>
        </svg>
        工作台
      </button>
    </div>

    <!-- Desktop: notes / conversations panel swap with smooth transition -->
    <Transition name="sidebar-swap" mode="out-in">
    <div class="notes-panel-inline" v-if="showNotesPanel" key="notes-panel">
      <div class="notes-panel-header">
        <span class="notes-panel-title">笔记本</span>
        <button class="new-note-btn" @click="startNewNote" title="新建笔记">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
            <line x1="12" y1="18" x2="12" y2="12"/>
            <line x1="9" y1="15" x2="15" y2="15"/>
          </svg>
        </button>
      </div>
      <div class="notes-panel-loading" v-if="notesPanelLoading">加载中…</div>
      <div class="notes-panel-list" v-else>
           <!-- "首页" entry: same hierarchy as a notebook, jumps to the
             notebook-picker page so the user can switch notebooks quickly. -->
        <div
          class="np-notebook np-home"
          :class="{ active: isOnNotesRoot }"
          @click="goToNotebooksHome"
          role="button"
          tabindex="0"
          @keydown.enter.prevent="goToNotebooksHome"
          @keydown.space.prevent="goToNotebooksHome"
        >
          <div class="np-notebook-row">
            <svg class="np-chevron np-chevron-placeholder" width="13" height="13" viewBox="0 0 24 24" aria-hidden="true"></svg>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M3 12l2-2 7-7 7 7 2 2"/>
              <path d="M5 10v10a1 1 0 0 0 1 1h3v-6h6v6h3a1 1 0 0 0 1-1V10"/>
            </svg>
            <span class="np-nb-name">首页</span>
          </div>
        </div>
        <div
          class="np-notebook"
          v-for="nb in notesStore.notebooks"
          :key="nb.id"
        >
          <div
            class="np-notebook-row"
            :class="{ active: isActivePanelNotebook(nb.id) }"
            @click="toggleNotesPanelNotebook(nb.id)"
            @dblclick="openNotebookInPanel(nb.id)"
          >
            <svg class="np-chevron" :class="{ expanded: !!notesPanelExpanded[nb.id] }" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="9 6 15 12 9 18"/>
            </svg>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
            <span class="np-nb-name">{{ nb.name }}</span>
            <span class="np-count">{{ nb.note_count }}</span>
            <button
              class="np-menu-btn"
              :class="{ active: npMenuId === 'nb:' + nb.id }"
              @click.stop="openNpNotebookMenu(nb, $event)"
              title="更多操作"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
            </button>
          </div>
          <div class="np-notes-list" v-show="!!notesPanelExpanded[nb.id]">
            <div
              v-for="note in notesStore.notes[nb.id] || []"
              :key="note.id"
              class="np-note-row"
              :class="{ active: isActivePanelNote(nb.id, note.id) }"
              @click="openNoteInPanel(nb.id, note.id)"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <span class="np-note-title">{{ note.title || '无标题' }}</span>
              <button
                class="np-menu-btn"
                :class="{ active: npMenuId === 'note:' + note.id }"
                @click.stop="openNpNoteMenu(note, $event)"
                title="更多操作"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
              </button>
            </div>
            <div v-if="!!notesPanelNotebookLoading[nb.id]" class="np-note-loading">加载中…</div>
            <div v-else-if="!(notesStore.notes[nb.id] || []).length" class="np-empty">暂无笔记</div>
          </div>
        </div>
        <div v-if="!notesStore.notebooks.length" class="np-empty">暂无笔记本</div>
      </div>
    </div>
    <!-- conversation view: shown when notes panel is not active -->
    <div class="sidebar-body" v-else>
    <div class="action-buttons">
      <button class="new-chat-btn" @click="handleNewChat" aria-label="新建对话">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 20h9"/>
          <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
        </svg>
        新建对话
      </button>
      <button class="new-group-btn" @click="openCreateGroupDialog" title="新建分组" aria-label="新建分组">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          <line x1="12" y1="11" x2="12" y2="17"/>
          <line x1="9" y1="14" x2="15" y2="14"/>
        </svg>
      </button>
    </div>

    <div class="sidebar-toolbar">
      <div class="toolbar-assistant" @click.stop="toggleToolsMenu">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
          <circle cx="12" cy="7" r="4"/>
        </svg>
        <span class="toolbar-assistant-name">{{ selectedAssistantName }}</span>
        <svg class="toolbar-arrow" :class="{ open: toolsMenuOpen }" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="9 6 15 12 9 18"/>
        </svg>
      </div>
      <button class="toolbar-search-btn" @click="openSpotlightSearch" title="搜索对话">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"/>
          <line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
      </button>
    </div>

    <!-- Tools Dropdown (assistant selector) -->
    <div class="tools-dropdown" v-show="toolsMenuOpen" @click.stop>
      <div class="tools-section">
        <div class="tools-section-title">选择助手</div>
        <button
          v-for="assistant in assistantStore.assistants"
          :key="assistant.id"
          class="tools-item"
          :class="{ active: assistant.id === assistantStore.currentAssistantId }"
          @click="selectAssistant(assistant.id)"
        >
          <span class="tools-item-name">{{ assistant.name }}</span>
          <div class="tools-item-actions" @click.stop>
            <button class="tools-action-btn" @click="openEditModal(assistant)" title="编辑">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
              </svg>
            </button>
            <button class="tools-action-btn delete" @click="handleDeleteAssistant(assistant.id)" title="删除">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
          </div>
        </button>
        <div class="tools-divider"></div>
        <button class="tools-item create-item" @click="openCreateModal">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
            <circle cx="8.5" cy="7" r="4"/>
            <line x1="20" y1="8" x2="20" y2="14"/>
            <line x1="23" y1="11" x2="17" y2="11"/>
          </svg>
          <span>新建助手</span>
        </button>
      </div>
    </div>

    <!-- Spotlight Search Modal -->
    <Teleport to="body">
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
    </Teleport>

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
              {{ selectedConversationIds.size }}个对话, {{ selectedGroupIds.size }}个分组 已选
            </template>
            <template v-else-if="selectedConversationIds.size > 0">
              {{ selectedConversationIds.size }}个对话 已选
            </template>
            <template v-else-if="selectedGroupIds.size > 0">
              {{ selectedGroupIds.size }}个分组 已选
            </template>
            <template v-else>
              未选择
            </template>
          </template>
          <template v-else>
            {{ selectedConversationIds.size }} 已选
          </template>
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
        <button class="selection-cancel-btn export-cancel-btn" @click="exitSelectionMode">取消</button>
      </div>
      <div class="selection-progress" v-if="selectionProgress">{{ selectionProgress }}</div>
    </div>

    <!-- Group + Conversation List -->
    <div class="conversation-list" ref="conversationListEl" @mousedown="onConversationListMouseDown" v-if="(chatStore.conversations.length > 0 || groupStore.groups.length > 0 || assistantStore.currentAssistantId)">
      <!-- "分组对话" Directory (pinned to top) -->
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
            :group="{ name: 'groups', pull: false, put: false }"
            item-key="id"
            handle=".group-header"
            :delay="100"
            :disabled="isMobileView || selectionModeActive"
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
                    class="group-checkbox selection-checkbox"
                    :checked="selectedGroupIds.has(group.id)"
                    @click.stop
                    @change="toggleGroupSelect(group.id)"
                  />
                  <svg
                    class="group-chevron"
                    :class="{ expanded: !groupStore.isGroupCollapsed(group.id) || (isDragging && dragExpandedGroupId === group.id) }"
                    width="13"
                    height="13"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                  >
                    <polyline points="9 6 15 12 9 18"/>
                  </svg>
                  <span class="group-color-dot" :style="{ backgroundColor: group.color }"></span>
                  <span class="group-name">{{ group.name }}</span>
                  <span class="group-count">{{ getGroupConvCount(group.id) }}</span>
                  <div class="group-actions hide-on-mobile" v-if="!isDeleteMode">
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
                    :delay="100"
                    :sort="false"
                    :sort-key="'group:' + group.id"
                    :disabled="isMobileView || selectionModeActive || editingTitleId !== null"
                    ghost-class="sortable-ghost"
                    :force-reinit="forceReinitCounter"
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
                        @swipe-save-note="handleSwipeSaveToNote"
                        @swipe-delete="handleSwipeDelete"
                        @swipe-move-group="handleSwipeMoveGroup"
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
          :delay="100"
          :sort="false"
          :sort-key="'ungrouped:' + category.key"
          :disabled="isMobileView || selectionModeActive || editingTitleId !== null"
          ghost-class="sortable-ghost"
          :force-reinit="forceReinitCounter"
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
              @swipe-save-note="handleSwipeSaveToNote"
              @swipe-delete="handleSwipeDelete"
              @swipe-move-group="handleSwipeMoveGroup"
              @toggle-menu="toggleMenu"
            />
          </div>
        </SortableList>
      </div>
      <!-- Always-visible empty ungrouped drop zone (for dragging out of groups) -->
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
          :delay="100"
          :sort="false"
          :sort-key="'ungrouped:empty'"
          :disabled="isMobileView || selectionModeActive || editingTitleId !== null"
          ghost-class="sortable-ghost"
          :force-reinit="forceReinitCounter"
          @start="onDragStart"
          @end="onConversationDragEnd"
        >
          <div v-for="item in emptyUngroupedList" :key="item.id"
               data-draggable="true" :data-key="item.id">
          </div>
        </SortableList>
      </div>
    </div>

    <div class="empty-state" v-else-if="!loadingConversations">
      <p v-if="assistantStore.currentAssistantId">暂无对话</p>
      <p v-else>请先选择一个助手</p>
    </div>

    <!-- Marquee selection overlay -->
    <div
      v-show="isMarqueeSelecting && marqueeAnchor"
      class="marquee-overlay"
      :style="marqueeStyle"
    ></div>
    </div><!-- /sidebar-body -->
    </Transition>

    <!-- Notes panel context menu (notebook: 重命名/删除；note: 重命名/移动到/删除，与工作台笔记菜单一致) -->
    <Teleport to="body">
      <div
        v-if="npMenuTarget"
        class="np-context-menu"
        :style="npMenuStyle"
        @click.stop
      >
        <template v-if="npMenuTarget.kind === 'notebook'">
          <button class="menu-item" @click="handleNpRenameNotebook">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            <span>重命名</span>
          </button>
          <button class="menu-item delete" @click="handleNpDeleteNotebook">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
            <span>删除</span>
          </button>
        </template>
        <template v-else>
          <button class="menu-item" @click="handleNpRenameNote">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            <span>重命名</span>
          </button>
          <button class="menu-item" @click="handleNpMoveNote">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              <line x1="12" y1="11" x2="12" y2="17"/>
              <line x1="9" y1="14" x2="15" y2="14"/>
            </svg>
            <span>移动到</span>
          </button>
          <div class="menu-divider"></div>
          <button class="menu-item delete" @click="handleNpDeleteNote">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
            <span>删除</span>
          </button>
        </template>
      </div>
    </Teleport>

    <!-- Rename notebook/note dialog (notes panel menu) -->
    <Teleport to="body">
      <div v-if="showNpRenameDialog" class="modal-overlay" @mousedown.self="showNpRenameDialog = false">
        <div class="modal-content" @click.stop>
          <h3 class="modal-title">{{ npRenameTarget?.kind === 'notebook' ? '重命名笔记本' : '重命名笔记' }}</h3>
          <div class="modal-body">
            <input
              ref="npRenameInputRef"
              v-model="npRenameValue"
              type="text"
              :placeholder="npRenameTarget?.kind === 'notebook' ? '输入笔记本名称' : '输入新标题'"
              @keyup.enter="confirmNpRename"
            />
          </div>
          <div class="modal-actions">
            <button class="modal-btn cancel" @click="showNpRenameDialog = false">取消</button>
            <button class="modal-btn confirm" @click="confirmNpRename">保存</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Move note dialog (notes panel menu) -->
    <Teleport to="body">
      <div v-if="showNpMoveDialog" class="modal-overlay" @mousedown.self="showNpMoveDialog = false">
        <div class="modal-content" @click.stop>
          <h3 class="modal-title">移动到笔记本</h3>
          <div class="modal-body">
            <div class="np-move-options">
              <button
                v-for="nb in notesStore.notebooks"
                :key="nb.id"
                class="np-move-option"
                :class="{ active: npMoveTargetNotebookId === nb.id }"
                @click="npMoveTargetNotebookId = nb.id"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                  <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                </svg>
                <span>{{ nb.name }}</span>
              </button>
            </div>
          </div>
          <div class="modal-actions">
            <button class="modal-btn cancel" @click="showNpMoveDialog = false">取消</button>
            <button class="modal-btn confirm" @click="confirmNpMove" :disabled="!npMoveTargetNotebookId">移动</button>
          </div>
        </div>
      </div>
    </Teleport>

    <div class="sidebar-footer">
      <div class="user-info-wrapper" v-if="auth.user.value">
        <div ref="userInfoRef" class="user-info" @click.stop="showUserMenu = !showUserMenu">
          <span class="user-avatar">{{ auth.user.value.username.charAt(0).toUpperCase() }}</span>
          <span class="user-name">{{ auth.user.value.username }}</span>
        </div>
        <Teleport to="body">
          <div v-if="showUserMenu" class="user-menu-overlay" @click="showUserMenu = false">
            <div class="user-menu" :style="userMenuPositionStyle" @click.stop>
              <button class="user-menu-item" @click="handleUserMenuTheme($event)">
                <svg v-if="!isDark" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
                </svg>
                <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="5"/>
                  <line x1="12" y1="1" x2="12" y2="3"/>
                  <line x1="12" y1="21" x2="12" y2="23"/>
                  <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
                  <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
                  <line x1="1" y1="12" x2="3" y2="12"/>
                  <line x1="21" y1="12" x2="23" y2="12"/>
                  <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
                  <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
                </svg>
                <span>{{ isDark ? '浅色模式' : '深色模式' }}</span>
              </button>
              <button class="user-menu-item" @click="handleUserMenuVoice">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/>
                  <path d="M5 11a7 7 0 0 0 14 0"/>
                  <line x1="12" y1="18" x2="12" y2="22"/>
                  <path d="M2 8c1 0 1 2 2 2s1-2 2-2" opacity="0.5"/>
                  <path d="M18 8c1 0 1 2 2 2s1-2 2-2" opacity="0.5"/>
                </svg>
                <span>语音助理</span>
              </button>
              <button class="user-menu-item" @click="showUserMenu = false; showSystemSettingsDialog = true">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="3"/>
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
                </svg>
                <span>系统设置</span>
              </button>
              <button class="user-menu-item logout" @click="showUserMenu = false; handleLogout()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
                  <polyline points="16 17 21 12 16 7"/>
                  <line x1="21" y1="12" x2="9" y2="12"/>
                </svg>
                <span>退出登录</span>
              </button>
            </div>
          </div>
        </Teleport>
      </div>
    </div>

    <AssistantModal
      :visible="modalVisible"
      :assistant="editingAssistant"
      @close="closeModal"
      @save="handleSaveAssistant"
      @batch-export="handleAssistantBatchExport"
      @batch-delete="handleAssistantBatchDelete"
    />

    <NotebookPicker
      v-if="showNotebookPicker"
      @select="handleNotebookSelected"
      @close="showNotebookPicker = false"
    />

    <NotebookPicker
      v-if="showNewNotePicker"
      @select="handleNewNoteNotebookSelected"
      @close="showNewNotePicker = false"
    />

    <SystemSettingsDialog
      :visible="showSystemSettingsDialog"
      :permissions="auth.user.value?.agent_permissions || { terminal_execution: true, note_create: true, note_edit: true, note_delete: true, notebook_create: true, notebook_edit: true, notebook_delete: true }"
      :saving="savingPermissions"
      @close="showSystemSettingsDialog = false"
      @permissions-save="handleSavePermissions"
      @hotwords-save="handleHotwordsSave"
      @skills-updated="handleSkillsUpdated"
    />

    <Teleport to="body">
      <div
        v-if="activeMenuId"
        class="conversation-menu"
        :style="menuStyle"
        @click.stop
      >
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
      <div
        v-if="showMoveToGroupMenu"
        class="conversation-menu move-group-menu"
        :style="moveMenuStyle"
        @click.stop
      >
        <button
          class="menu-item"
          @click="moveConversationToGroup(moveTargetConv!.id, null)"
        >
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
        <div class="menu-divider" v-if="otherAssistantsForMove.length"></div>
        <div class="menu-section-label" v-if="otherAssistantsForMove.length">移动到其他助手</div>
        <button
          v-for="assistant in otherAssistantsForMove"
          :key="assistant.id"
          class="menu-item"
          @click="moveConversationToAssistant(moveTargetConv!.id, assistant.id)"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
            <circle cx="12" cy="7" r="4"/>
          </svg>
          <span>{{ assistant.name }}</span>
        </button>
      </div>
    </Teleport>

    <!-- Group Dialog (Create/Edit) -->
    <Teleport to="body">
      <div v-if="showGroupDialog" class="modal-overlay" @mousedown.self="showGroupDialog = false">
        <div class="modal-content group-dialog" @click.stop>
          <h3 class="modal-title">{{ editingGroup ? '编辑分组' : '新建分组' }}</h3>
          <div class="modal-body">
            <div class="form-group">
              <label>分组名称</label>
              <input
                v-model="newGroupName"
                type="text"
                placeholder="输入分组名称"
                @keyup.enter="saveGroup"
              />
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
            <div class="form-group" v-if="assistantStore.assistants.length > 1">
              <label>所属助手</label>
              <select v-model="newGroupAssistantId" class="form-select">
                <option v-for="a in assistantStore.assistants" :key="a.id" :value="a.id">{{ a.name }}</option>
              </select>
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
        <div class="modal-content delete-dialog" @click.stop>
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

    <!-- Bulk Delete Confirmation Dialog -->
    <Teleport to="body">
      <div v-if="showBulkDeleteDialog" class="modal-overlay" @click="showBulkDeleteDialog = false">
        <div class="modal-content delete-dialog" @click.stop>
          <h3 class="modal-title">确认删除</h3>
          <div class="modal-body">
            <p>{{ bulkDeleteConfirmMessage }}</p>
            <label v-if="hasSelectedGroups" class="checkbox-label">
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

    <!-- Batch Operation Popup -->
    <Teleport to="body">
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
                <input type="checkbox" :checked="batchPopupSelected.size === batchPopupList.length && batchPopupList.length > 0" @change="toggleBatchPopupSelectAll" />
                全选 ({{ batchPopupSelected.size }}/{{ batchPopupList.length }})
              </label>
            </div>
            <div class="batch-popup-list">
              <div v-if="batchPopupList.length === 0" class="batch-popup-empty">暂无会话</div>
              <div
                v-for="conv in batchPopupList"
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
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useChatStore } from '@/stores/chat'
import { useAssistantStore } from '@/stores/assistant'
import { useSkinStore } from '@/stores/skin'
import { useGroupStore } from '@/stores/groups'
import type { Assistant, AssistantFormData, ConversationGroup, Conversation, ConversationSearchResult } from '@/types'
import { chatApi } from '@/api/chat'
import { assistantApi } from '@/api/assistant'
import { notesApi } from '@/api/notes'
import { useNotesStore } from '@/stores/notes'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useAuth } from '@/composables/useAuth'
import { isMobileViewport, navigateWithMobileHistory } from '@/composables/useMobileNavigation'
import { useMediaQuery } from '@/composables/useMediaQuery'
import AssistantModal from './AssistantModal.vue'
import NotebookPicker from './NotebookPicker.vue'
import SystemSettingsDialog from './SystemSettingsDialog.vue'
import ConversationRow from './ConversationRow.vue'
import SortableList from './SortableList.vue'
import LogoIcon from './LogoIcon.vue'

const router = useRouter()
const route = useRoute()
const chatStore = useChatStore()
const assistantStore = useAssistantStore()
const groupStore = useGroupStore()
const notesStore = useNotesStore()
const auth = useAuth()
const { show: showToast } = useToast()
const { confirm: showConfirm } = useConfirmDialog()

const showNotebookPicker = ref(false)
const showNewNotePicker = ref(false)
const saveToNoteConvId = ref<string | null>(null)
const saveToNoteConvTitle = ref('')

// Dark mode / 皮肤 —— 状态与逻辑统一在 useSkinStore（皮肤×明暗双轴）
const skinStore = useSkinStore()
const isDark = computed(() => skinStore.isDark)

function toggleTheme(event: MouseEvent) {
  skinStore.toggleMode(event)
}

const dropdownOpen = ref(false)
const toolsMenuOpen = ref(false)
const showUserMenu = ref(false)

const userMenuStyle = computed(() => {
  return {} // positioned via CSS
})

const userInfoRef = ref<HTMLElement | null>(null)

const userMenuPositionStyle = computed(() => {
  // 依赖 showUserMenu：每次开合都重取 rect（切肤/布局变化后防漂移）
  void showUserMenu.value
  if (!userInfoRef.value) {
    return { position: 'fixed', bottom: '56px', left: '12px' } as Record<string, string>
  }
  const rect = userInfoRef.value.getBoundingClientRect()
  const menuWidth = 180
  const avatar = userInfoRef.value.querySelector('.user-avatar')?.getBoundingClientRect()
  // 菜单底部贴头像上方一点点（4px），各皮肤一致
  const gap = 4
  const estimatedMenuHeight = 156
  let left = rect.left
  let top = (avatar ? avatar.top : rect.top) - gap

  if (isMobileViewport()) {
    left = Math.min(Math.max(12, left), window.innerWidth - menuWidth - 12)
  }

  if (top - estimatedMenuHeight < 8) {
    top = estimatedMenuHeight + 8
  }

  return {
    position: 'fixed',
    top: `${top}px`,
    left: `${left}px`,
    transform: 'translateY(-100%)',
    bottom: 'auto',
  } as Record<string, string>
})

function handleUserMenuTheme(event: MouseEvent) {
  showUserMenu.value = false
  toggleTheme(event)
}

function handleUserMenuVoice() {
  showUserMenu.value = false
  void navToVoice()
}

const modalVisible = ref(false)
const editingAssistant = ref<Assistant | null>(null)
const loadingConversations = ref(false)
const showSystemSettingsDialog = ref(false)
const titleInput = ref<HTMLInputElement | null>(null)
const editingTitleId = ref<string | null>(null)
const editingTitle = ref('')
const selectionMode = ref<'export' | 'delete' | null>(null)
const savingPermissions = ref(false)
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
const newGroupAssistantId = ref<string | null>(null)

/** 移动到分组菜单里的「移动到其他助手」列表（排除当前助手）。 */
const otherAssistantsForMove = computed(() =>
  assistantStore.assistants.filter(a => a.id !== assistantStore.currentAssistantId)
)
const newGroupColor = ref('#3b82f6')
const showDeleteGroupDialog = ref(false)
const deletingGroup = ref<ConversationGroup | null>(null)
const deleteGroupWithConversations = ref(false)
const showMoveToGroupMenu = ref(false)
const moveMenuStyle = ref<{ top: string; left: string }>({ top: '0px', left: '0px' })
const moveTargetConv = ref<{ id: string; title: string; group_id?: string | null } | null>(null)

const { isMobile: isMobileView } = useMediaQuery()

const dragLists = reactive<Record<string, Conversation[]>>({})
const isDragging = ref(false)

const GROUPS_DIRECTORY_COLLAPSED_KEY = 'chatllm_groups_directory_collapsed'
const groupsDirectoryCollapsed = ref(localStorage.getItem(GROUPS_DIRECTORY_COLLAPSED_KEY) === 'true')

function toggleGroupsDirectory() {
  groupsDirectoryCollapsed.value = !groupsDirectoryCollapsed.value
  localStorage.setItem(GROUPS_DIRECTORY_COLLAPSED_KEY, String(groupsDirectoryCollapsed.value))
}

const showBulkDeleteDialog = ref(false)
const bulkDeleteConfirmMessage = ref('')
const bulkDeleteWithConversations = ref(false)
const bulkDeleteContext = ref<'selection' | 'batchPopup'>('selection')

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
    if (!categories.has(key)) {
      categories.set(key, [])
    }
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
    if (convs.length > 0) {
      result.push({
        key,
        label: getTimeCategoryLabel(key),
        conversations: convs
      })
    }
  }

  return result
})

const convDragGroup = {
  name: 'conversations',
  pull: true,
  put: true
}

const emptyUngroupedList = ref<Conversation[]>([])

// Drag hover expansion
const dragExpandedGroupId = ref<string | null>(null)
const groupsDirectoryWasCollapsed = ref(false)

// Force SortableJS re-initialization counter (incremented on drag start)
const forceReinitCounter = ref(0)

// Marquee selection
const conversationListEl = ref<HTMLDivElement | null>(null)
const isMarqueeSelecting = ref(false)
const marqueeAnchor = ref<{ x: number; y: number } | null>(null)
const marqueeEnd = ref<{ x: number; y: number } | null>(null)
const marqueePreviousSelection = ref<Set<string>>(new Set())

// Edge scroll
let edgeScrollRaf: number | null = null
const lastPointerPosition = ref<{ x: number; y: number } | null>(null)
const EDGE_SCROLL_THRESHOLD = 40
const EDGE_SCROLL_SPEED = 10

// Drag hover rAF throttling
let dragMoveRaf: number | null = null
let pendingDragMoveEvent: MouseEvent | TouchEvent | null = null

// Marquee rAF throttling
let marqueeMoveRaf: number | null = null
let pendingMarqueeMoveEvent: MouseEvent | null = null

// Suppress click after marquee
let suppressClickAfterMarqueeUntil = 0

function syncDragLists() {
  if (isDragging.value) {
    return
  }

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
    if (!validKeys.has(key)) {
      delete dragLists[key]
    }
  }

}

function getGroupConvCount(groupId: string): number {
  return (dragLists[groupId] || []).length
}

const swipedConversationId = ref<string | null>(null)
const swipeOffset = ref(0)
const swipeTrackingId = ref<string | null>(null)
const swipeStartX = ref(0)
const swipeStartY = ref(0)
const swipeStartOffset = ref(0)
const isSwipeTracking = ref(false)
const isSwipeDragging = ref(false)

const SWIPE_ACTION_WIDTH = 250
let suppressConversationClickUntil = 0

function formatDate(dateStr: string): string {
  if (!dateStr) return ''
  const normalized = dateStr.endsWith('Z') || dateStr.includes('+') || dateStr.includes('-', 10) ? dateStr : dateStr + 'Z'
  const date = new Date(normalized)
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

// Spotlight search
const spotlightVisible = ref(false)
const spotlightQuery = ref('')
const spotlightResults = ref<ConversationSearchResult[]>([])
const spotlightLoading = ref(false)
const spotlightInputRef = ref<HTMLInputElement | null>(null)
let spotlightDebounceTimer: ReturnType<typeof setTimeout> | null = null
// Last-request-wins guard: each debounce fire takes a monotonic seq; a
// response is applied ONLY if it belongs to the newest request. Without
// this, a slow in-flight query ("s" ~39s, "su" ~19s) that landed after a
// fast correct query ("su7" ~1.6s) would OVERWRITE the correct results —
// the "先正确后突然变成完全不相关" symptom (2026-08-07).
let spotlightSearchSeq = 0

// Settle time for the 0.25s grid-template-rows group expand transition
const GROUP_EXPAND_SETTLE_MS = 320

function openSpotlightSearch() {
  spotlightVisible.value = true
  spotlightQuery.value = ''
  spotlightResults.value = []
  spotlightLoading.value = false
  spotlightSearchSeq++ // invalidate any in-flight request from a previous modal
  nextTick(() => {
    spotlightInputRef.value?.focus()
  })
}

function closeSpotlightSearch() {
  spotlightVisible.value = false
  spotlightQuery.value = ''
  spotlightResults.value = []
  spotlightLoading.value = false
  spotlightSearchSeq++ // late responses must never re-populate a closed modal
  if (spotlightDebounceTimer) clearTimeout(spotlightDebounceTimer)
}

function onSpotlightInput() {
  if (spotlightDebounceTimer) clearTimeout(spotlightDebounceTimer)
  const q = spotlightQuery.value.trim()
  if (!q) {
    spotlightSearchSeq++ // discard in-flight responses after clearing the input
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
      if (seq !== spotlightSearchSeq) return // stale response — never apply
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
  suppressConversationScrollWatch = true
  try {
    await handleSelectConversation(conversationId)
    await revealConversationInSidebar(conversationId)
  } finally {
    suppressConversationScrollWatch = false
  }
}

async function handleSpotlightMessageClick(conversationId: string, messageId: string, query: string) {
  closeSpotlightSearch()
  chatStore.searchHighlightQuery = query
  chatStore.searchHighlightMessageId = messageId
  chatStore.searchHighlightNonce++
  suppressConversationScrollWatch = true
  try {
    await handleSelectConversation(conversationId)
    await revealConversationInSidebar(conversationId)
  } finally {
    suppressConversationScrollWatch = false
  }
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
    console.warn('revealConversationInSidebar: row not found in sidebar', conversationId, { groupId, groupExists })
  }
}

function escapeHtml(unsafe: string): string {
  return unsafe
    .replace(/\u0026/g, '\u0026amp;')
    .replace(/\u003c/g, '\u0026lt;')
    .replace(/\u003e/g, '\u0026gt;')
    .replace(/"/g, '\u0026quot;')
    .replace(/'/g, '\u0026#039;')
}

function highlightKeyword(text: string, keyword: string): string {
  if (!keyword.trim()) return escapeHtml(text)
  const safeText = escapeHtml(text)
  const escaped = keyword.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$\u0026')
  const regex = new RegExp(`(${escaped})`, 'gi')
  return safeText.replace(regex, '\u003cmark\u003e$1\u003c/mark\u003e')
}

const selectionModeActive = computed(() => selectionMode.value !== null)
const isExportMode = computed(() => selectionMode.value === 'export')
const isDeleteMode = computed(() => selectionMode.value === 'delete')
const hasSelectedGroups = computed(() => selectedGroupIds.value.size > 0)
const hasAnySelection = computed(() => selectedConversationIds.value.size > 0 || selectedGroupIds.value.size > 0)

const marqueeStyle = computed(() => {
  if (!marqueeAnchor.value) return {}
  const end = marqueeEnd.value || marqueeAnchor.value
  const left = Math.min(marqueeAnchor.value.x, end.x)
  const top = Math.min(marqueeAnchor.value.y, end.y)
  const width = Math.abs(end.x - marqueeAnchor.value.x)
  const height = Math.abs(end.y - marqueeAnchor.value.y)
  return {
    left: `${left}px`,
    top: `${top}px`,
    width: `${width}px`,
    height: `${height}px`,
  }
})

const selectedAssistantName = computed(() => {
  if (!assistantStore.currentAssistantId) return '选择助手'
  const assistant = assistantStore.getAssistantById(assistantStore.currentAssistantId)
  return assistant?.name || '选择助手'
})

const isOnNotesPage = computed(() => {
  return route.path.startsWith('/notes')
})

const isOnZenPage = computed(() => {
  return route.path === '/zen'
})

const isOnVoicePage = computed(() => {
  return route.path === '/voice'
})

// True when the user is on the notebook-picker page (i.e. /notes exactly,
// no notebook id selected). Used to highlight the "首页" sidebar entry.
const isOnNotesRoot = computed(() => {
  return route.path === '/notes' || route.path === '/notes/'
})

// Group that owns the current conversation — used to give the parent
// group header a focus state so users can tell which group their
// active session belongs to.
const focusGroupId = computed(() => {
  const current = chatStore.conversations.find(c => c.id === chatStore.currentConversationId)
  return current?.group_id || null
})

const activeNotebookId = computed(() => {
  return typeof route.params.notebookId === 'string' ? route.params.notebookId : ''
})

const activeNoteId = computed(() => {
  return typeof route.params.noteId === 'string' ? route.params.noteId : ''
})

function goToNotebooksHome() {
  if (isOnNotesRoot.value) return
  notesStore.saveLastNotesPath('/notes')
  void navigateWithMobileHistory(router, '/notes')
  if (isMobileViewport()) emit('close-drawer')
}

// Desktop inline notes panel — driven by route
const showNotesPanel = ref(isOnNotesPage.value)
const notesPanelLoading = ref(false)
const NOTES_PANEL_EXPANDED_KEY = 'chatllm_notes_panel_expanded_v2'
function _loadExpandedFromStorage(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(NOTES_PANEL_EXPANDED_KEY) || '{}')
  } catch {
    return {}
  }
}
const notesPanelExpanded = ref<Record<string, boolean>>(_loadExpandedFromStorage())
const notesPanelNotebookLoading = ref<Record<string, boolean>>({})

function _saveExpandedToStorage() {
  localStorage.setItem(NOTES_PANEL_EXPANDED_KEY, JSON.stringify(notesPanelExpanded.value))
}

function setNotesPanelExpanded(notebookId: string, expanded: boolean) {
  if (expanded) {
    notesPanelExpanded.value = { ...notesPanelExpanded.value, [notebookId]: true }
  } else {
    const next = { ...notesPanelExpanded.value }
    delete next[notebookId]
    notesPanelExpanded.value = next
  }
  _saveExpandedToStorage()
}

// Sync panel with route changes (e.g. back/forward, direct URL)
watch(isOnNotesPage, (onNotes) => {
  showNotesPanel.value = onNotes
  if (onNotes) _loadNotesPanelData()
})

// When notebooks become available (loaded by NotebooksList or NotesList),
// ensure notes are loaded for expanded notebooks
watch(() => notesStore.notebooks.length, async (len, oldLen) => {
  if (len > 0 && (!oldLen || oldLen === 0) && showNotesPanel.value) {
    await _loadNotesPanelData()
  }
})

watch([isOnNotesPage, activeNotebookId], async ([onNotes, notebookId]) => {
  if (!onNotes || !notebookId) return
  if (!notesStore.notebooks.length) await _loadNotesPanelData()
  if (!notesPanelExpanded.value[notebookId]) {
    setNotesPanelExpanded(notebookId, true)
  }
  await loadNotesPanelNotebook(notebookId)
})

function navToChat() {
  if (!isOnNotesPage.value) return
  void navigateWithMobileHistory(router, '/')
}

async function navToNotes() {
  if (isOnNotesPage.value) return
  await navigateWithMobileHistory(router, notesStore.lastNotesPath)
  // panel will be shown via the route watcher
}

function navToZen() {
  if (isOnZenPage.value) return
  void navigateWithMobileHistory(router, '/zen')
}

function navToVoice() {
  if (isOnVoicePage.value) return
  void navigateWithMobileHistory(router, '/voice')
}

async function _loadNotesPanelData() {
  if (!notesStore.notebooks.length) {
    await notesStore.loadNotebooks()
    if (!notesStore.notebooks.length) return
  }

  notesPanelLoading.value = true
  try {
    if (Object.keys(notesPanelExpanded.value).length === 0) {
      const firstId = notesStore.notebooks[0].id
      setNotesPanelExpanded(firstId, true)
    }
    const expandedIds = Object.keys(notesPanelExpanded.value).filter(k => notesPanelExpanded.value[k])
    const loadPromises = expandedIds
      .filter(id => !notesStore.notes[id] || notesStore.notes[id].length === 0)
      .map(id => loadNotesPanelNotebook(id))
    if (loadPromises.length > 0) {
      await Promise.all(loadPromises)
    }
  } finally {
    notesPanelLoading.value = false
  }
}

async function openNotesPanel() {
  showNotesPanel.value = true
  await _loadNotesPanelData()
}

// Load data on mount if already on notes page
if (isOnNotesPage.value) _loadNotesPanelData()

async function loadNotesPanelNotebook(notebookId: string) {
  notesPanelNotebookLoading.value = { ...notesPanelNotebookLoading.value, [notebookId]: true }
  try {
    await notesStore.loadNotes(notebookId)
  } finally {
    const next = { ...notesPanelNotebookLoading.value }
    delete next[notebookId]
    notesPanelNotebookLoading.value = next
  }
}

async function toggleNotesPanelNotebook(notebookId: string) {
  if (notesPanelExpanded.value[notebookId]) {
    setNotesPanelExpanded(notebookId, false)
  } else {
    setNotesPanelExpanded(notebookId, true)
    await loadNotesPanelNotebook(notebookId)
  }
}

function openNotebookInPanel(notebookId: string) {
  const targetPath = `/notes/${notebookId}`
  if (route.path === targetPath) return
  void navigateWithMobileHistory(router, targetPath)
  if (isMobileViewport()) emit('close-drawer')
}

function openNoteInPanel(notebookId: string, noteId: string) {
  const targetPath = `/notes/${notebookId}/${noteId}`
  if (route.path === targetPath) return
  void navigateWithMobileHistory(router, targetPath)
  if (isMobileViewport()) emit('close-drawer')
}

function isActivePanelNotebook(notebookId: string) {
  return activeNotebookId.value === notebookId
}

function isActivePanelNote(notebookId: string, noteId: string) {
  return activeNotebookId.value === notebookId && activeNoteId.value === noteId
}

// ─── Notes panel context menu (notebook: 重命名/删除 · note: 重命名/移动到/删除) ───
const npMenuId = ref<string | null>(null)
const npMenuTarget = ref<{ kind: 'notebook' | 'note'; id: string; title: string; notebookId?: string } | null>(null)
const npMenuStyle = ref<{ top: string; left: string }>({ top: '0px', left: '0px' })

function openNpNotebookMenu(nb: { id: string; name: string }, event: MouseEvent) {
  if (npMenuId.value === 'nb:' + nb.id) {
    closeNpMenu()
    return
  }
  positionNpMenu(event)
  npMenuTarget.value = { kind: 'notebook', id: nb.id, title: nb.name }
  npMenuId.value = 'nb:' + nb.id
}

function openNpNoteMenu(note: { id: string; title: string | null; notebook_id: string }, event: MouseEvent) {
  if (npMenuId.value === 'note:' + note.id) {
    closeNpMenu()
    return
  }
  positionNpMenu(event)
  npMenuTarget.value = { kind: 'note', id: note.id, title: note.title || '', notebookId: note.notebook_id }
  npMenuId.value = 'note:' + note.id
}

function positionNpMenu(event: MouseEvent) {
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const MENU_WIDTH = 140
  const PADDING = 8
  const GAP = 2
  let top = rect.bottom + GAP
  let left = rect.right - MENU_WIDTH
  if (left < PADDING) left = PADDING
  if (left + MENU_WIDTH > window.innerWidth - PADDING) {
    left = window.innerWidth - MENU_WIDTH - PADDING
  }
  npMenuStyle.value = { top: `${top}px`, left: `${left}px` }
  nextTick(() => {
    const menuEl = document.querySelector('.np-context-menu') as HTMLElement
    if (!menuEl) return
    const spaceBelow = window.innerHeight - rect.bottom - PADDING
    const spaceAbove = rect.top - PADDING
    if (menuEl.getBoundingClientRect().height > spaceBelow && spaceAbove > spaceBelow) {
      const newTop = Math.max(PADDING, rect.top - menuEl.getBoundingClientRect().height - GAP)
      npMenuStyle.value = { top: `${newTop}px`, left: `${left}px` }
    }
  })
}

function closeNpMenu() {
  npMenuId.value = null
  npMenuTarget.value = null
}

// Rename
const showNpRenameDialog = ref(false)
const npRenameValue = ref('')
const npRenameTarget = ref<{ kind: 'notebook' | 'note'; id: string; title: string; notebookId?: string } | null>(null)
const npRenameInputRef = ref<HTMLInputElement | null>(null)

function handleNpRenameNotebook() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t) return
  npRenameTarget.value = t
  npRenameValue.value = t.title
  showNpRenameDialog.value = true
  nextTick(() => npRenameInputRef.value?.focus())
}

function handleNpRenameNote() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t) return
  npRenameTarget.value = t
  npRenameValue.value = t.title
  showNpRenameDialog.value = true
  nextTick(() => npRenameInputRef.value?.focus())
}

async function confirmNpRename() {
  const title = npRenameValue.value.trim()
  const target = npRenameTarget.value
  if (!title || !target) {
    showNpRenameDialog.value = false
    npRenameTarget.value = null
    return
  }
  try {
    if (target.kind === 'notebook') {
      await notesStore.updateNotebook(target.id, title)
    } else {
      await notesStore.updateNote(target.id, { title })
      await loadNotesPanelNotebook(target.notebookId!)
    }
    showToast('已重命名', 'success')
  } catch (e) {
    console.error('Failed to rename:', e)
    showToast('重命名失败', 'error')
  }
  showNpRenameDialog.value = false
  npRenameTarget.value = null
}

// Move note
const showNpMoveDialog = ref(false)
const npMoveTarget = ref<{ id: string; title: string; notebookId: string } | null>(null)
const npMoveTargetNotebookId = ref('')

function handleNpMoveNote() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t || t.kind !== 'note') return
  npMoveTarget.value = { id: t.id, title: t.title, notebookId: t.notebookId! }
  npMoveTargetNotebookId.value = ''
  showNpMoveDialog.value = true
}

async function confirmNpMove() {
  const note = npMoveTarget.value
  if (!note || !npMoveTargetNotebookId.value) {
    showNpMoveDialog.value = false
    npMoveTarget.value = null
    return
  }
  const from = note.notebookId
  try {
    await notesStore.moveNote(note.id, npMoveTargetNotebookId.value)
    await loadNotesPanelNotebook(from)
    if (npMoveTargetNotebookId.value !== from) {
      await loadNotesPanelNotebook(npMoveTargetNotebookId.value)
    }
    showToast('已移动', 'success')
  } catch (e) {
    console.error('Failed to move note:', e)
    showToast('移动失败', 'error')
  }
  npMoveTarget.value = null
  showNpMoveDialog.value = false
}

// Delete notebook — 与笔记本主页(NotebooksList.handleDeleteNotebook)逻辑一致
async function handleNpDeleteNotebook() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t || t.kind !== 'notebook') return
  const nb = notesStore.notebooks.find(n => n.id === t.id)
  if (!nb) return
  if (nb.is_default) {
    showToast('默认笔记本不能删除', 'error')
    return
  }
  if (!await showConfirm({ message: '确定要删除这个笔记本吗？\n所有笔记也会被删除。', danger: true, confirmText: '删除' })) {
    return
  }
  try {
    await notesStore.deleteNotebook(t.id)
    const panel = { ...notesPanelExpanded.value }
    delete panel[t.id]
    notesPanelExpanded.value = panel
    if (activeNotebookId.value === t.id) {
      notesStore.saveLastNotesPath('/notes')
      void navigateWithMobileHistory(router, '/notes')
    }
    showToast('笔记本已删除', 'success')
  } catch (e) {
    console.error('Failed to delete notebook:', e)
    showToast('删除笔记本失败', 'error')
  }
}

// Delete note
async function handleNpDeleteNote() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t || t.kind !== 'note') return
  if (!await showConfirm({ message: '确定要删除这条笔记吗？', danger: true, confirmText: '删除' })) return
  const noteId = t.id
  const from = t.notebookId!
  try {
    await notesStore.deleteNote(noteId)
    await loadNotesPanelNotebook(from)
    if (activeNoteId.value === noteId) {
      void navigateWithMobileHistory(router, `/notes/${from}`)
    }
    showToast('已删除', 'success')
  } catch (e) {
    console.error('Failed to delete note:', e)
    showToast('删除失败', 'error')
  }
}

function onNpMenuOutsideClick(e: MouseEvent) {
  if (!npMenuId.value) return
  const target = e.target as HTMLElement
  if (target.closest('.np-context-menu') || target.closest('.np-menu-btn')) return
  closeNpMenu()
}

onMounted(() => {
  document.addEventListener('click', onNpMenuOutsideClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onNpMenuOutsideClick)
})

async function loadConversationsForAssistant() {
  loadingConversations.value = true
  try {
    await Promise.all([
      chatStore.loadConversations(assistantStore.currentAssistantId),
      groupStore.loadGroups(assistantStore.currentAssistantId || undefined)
    ])
    syncDragLists()
  } catch (e) {
    console.error('Error in loadConversationsForAssistant:', e)
  } finally {
    loadingConversations.value = false
  }
}

watch(() => assistantStore.currentAssistantId, () => {
  exitSelectionMode()
  cancelEditTitle()
  closeSwipeActions()
  chatStore.currentConversationId = null
  loadConversationsForAssistant()
})

watch(
  [() => chatStore.conversations, () => groupStore.groups],
  () => {
    nextTick(() => syncDragLists())
  },
  { deep: true }
)

// Auto-scroll to active conversation in sidebar
// Suppressed during spotlight search jumps: revealConversationInSidebar
// performs the single authoritative scroll (expand group → center the row).
let suppressConversationScrollWatch = false
watch(() => chatStore.currentConversationId, (id) => {
  if (!id || suppressConversationScrollWatch) return
  nextTick(() => {
    const el = document.querySelector(`.conversation-item[data-id="${id}"]`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  })
})

onMounted(async () => {
  if (auth.isAuthenticated) {
    // 先加载本人上传皮肤（运行时注册表），再做远端偏好同步/归属判定
    void skinStore.loadUploaded().then(() => skinStore.syncFromServer())
  }
  await assistantStore.loadAssistants()
  if (assistantStore.assistants.length > 0) {
    const defaultAssistant = assistantStore.assistants.find(a => a.name === '默认助手')
    if (defaultAssistant) {
      assistantStore.selectAssistant(defaultAssistant.id)
    } else {
      const persistedId = assistantStore.currentAssistantId
      if (persistedId && assistantStore.assistants.some(a => a.id === persistedId)) {
        // Persisted ID is still valid, keep it
      } else {
        assistantStore.selectAssistant(assistantStore.assistants[0].id)
      }
    }
  }
  await loadConversationsForAssistant()
  syncDragLists()
  document.addEventListener('click', closeAllMenus)
  })

onUnmounted(() => {
  document.removeEventListener('click', closeAllMenus)
  })

function selectAssistant(id: string) {
  assistantStore.selectAssistant(id)
  toolsMenuOpen.value = false
  dropdownOpen.value = false
}

function toggleToolsMenu() {
  toolsMenuOpen.value = !toolsMenuOpen.value
}

function openCreateModal() {
  editingAssistant.value = null
  modalVisible.value = true
  dropdownOpen.value = false
  toolsMenuOpen.value = false
}

function openEditModal(assistant: Assistant) {
  editingAssistant.value = assistant
  modalVisible.value = true
  dropdownOpen.value = false
  toolsMenuOpen.value = false
}

function closeModal() {
  modalVisible.value = false
  editingAssistant.value = null
}

function handleAssistantBatchExport() {
  const targetId = editingAssistant.value?.id ?? null
  closeModal()
  void openBatchExport(targetId)
}

function handleAssistantBatchDelete() {
  const targetId = editingAssistant.value?.id ?? null
  closeModal()
  void openBatchDelete(targetId)
}

async function handleSaveAssistant(data: AssistantFormData) {
  try {
    if (editingAssistant.value) {
      await assistantStore.updateAssistant(editingAssistant.value.id, data)
    } else {
      const newAssistant = await assistantStore.createAssistant(data)
      if (!assistantStore.currentAssistantId) {
        assistantStore.selectAssistant(newAssistant.id)
      }
    }
    closeModal()
  } catch (e) {
    console.error('Failed to save assistant:', e)
  }
}

async function handleDeleteAssistant(id: string) {
  if (!await showConfirm({ message: '确定要删除这个助手吗？所有相关对话也会被删除。', danger: true, confirmText: '删除' })) return
  try {
    await assistantStore.deleteAssistant(id)
    if (assistantStore.assistants.length > 0) {
      assistantStore.selectAssistant(assistantStore.assistants[0].id)
    }
  } catch (e) {
    console.error('Failed to delete assistant:', e)
  }
}

async function handleNewChat() {
  // Look for any existing conversation that has no messages and the default title.
  // This prevents creating duplicate empty sessions when the user switches away
  // from an empty conversation and clicks "新建对话" again.
  const emptyConv = chatStore.conversations.find(c => {
    const msgs = chatStore.messages[c.id]
    return (!msgs || msgs.length === 0) && c.title === '新对话'
  })
  if (emptyConv) {
    await chatStore.selectConversation(emptyConv.id)
    await navigateWithMobileHistory(router, '/')
    emit('close-drawer')
    return
  }
  await chatStore.createConversation(undefined, assistantStore.currentAssistantId)
  await navigateWithMobileHistory(router, '/')
  emit('close-drawer')
}

async function handleSelectConversation(id: string) {
  closeSwipeActions()
  await chatStore.selectConversation(id)
  if (isOnNotesPage.value) {
    await navigateWithMobileHistory(router, '/')
  }
  emit('close-drawer')
}

async function handleDeleteConversation(id: string) {
  await chatStore.deleteConversation(id)
}

function startEditTitle(conv: { id: string; title: string }) {
  closeAllMenus()
  editingTitleId.value = conv.id
  editingTitle.value = conv.title || ''
  nextTick(() => {
    titleInput.value?.focus()
    titleInput.value?.select()
  })
}

function updateTitle(value: string) {
  editingTitle.value = value
}

async function saveTitle(id: string) {
  const title = editingTitle.value.trim()
  if (!title) {
    cancelEditTitle()
    return
  }

  if (title !== (chatStore.conversations.find(conv => conv.id === id)?.title || '')) {
    await chatStore.updateConversationTitle(id, title)
  }
  editingTitleId.value = null
  editingTitle.value = ''
}

function cancelEditTitle() {
  editingTitleId.value = null
  editingTitle.value = ''
}

function closeSwipeActions() {
  swipedConversationId.value = null
  swipeOffset.value = 0
  swipeTrackingId.value = null
  isSwipeTracking.value = false
  isSwipeDragging.value = false
  swipeStartOffset.value = 0
}

function getConversationItemStyle(id: string): Record<string, string> {
  if (selectionModeActive.value || swipedConversationId.value !== id) {
    return { transform: 'translateX(0px)' }
  }

  return {
    transform: `translateX(${swipeOffset.value}px)`
  }
}

async function handleConversationClick(id: string) {
  if (Date.now() < suppressConversationClickUntil || Date.now() < suppressClickAfterMarqueeUntil) {
    return
  }

  if (selectionModeActive.value) {
    toggleSelect(id)
    return
  }

  if (swipedConversationId.value === id) {
    closeSwipeActions()
    return
  }

  await handleSelectConversation(id)
}

function toggleMenu(id: string, event: MouseEvent) {
  if (activeMenuId.value === id) {
    closeAllMenus()
    return
  }
  closeSwipeActions()
  const conv = chatStore.conversations.find(c => c.id === id)
  if (conv) {
    menuTargetConv.value = { id: conv.id, title: conv.title || '', group_id: conv.group_id }
  }
  activeMenuId.value = id
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const MENU_WIDTH = 160
  const PADDING = 8
  const GAP = 2

  // Default: right below the trigger button, right-aligned
  let top = rect.bottom + GAP
  let left = rect.right - MENU_WIDTH

  // Keep within horizontal bounds
  if (left < PADDING) left = PADDING
  if (left + MENU_WIDTH > window.innerWidth - PADDING) {
    left = window.innerWidth - MENU_WIDTH - PADDING
  }

  menuStyle.value = { top: `${top}px`, left: `${left}px` }

  // After render, measure actual menu height and flip upward if it overflows bottom
  nextTick(() => {
    const menuEl = document.querySelector('.conversation-menu') as HTMLElement
    if (!menuEl) return
    const menuRect = menuEl.getBoundingClientRect()
    const spaceBelow = window.innerHeight - rect.bottom - PADDING
    const spaceAbove = rect.top - PADDING

    if (menuRect.height > spaceBelow && spaceAbove > spaceBelow) {
      // Not enough space below — flip upward
      let newTop = rect.top - menuRect.height - GAP
      if (newTop < PADDING) newTop = PADDING
      menuStyle.value = { top: `${newTop}px`, left: `${left}px` }
    }
  })
}

function closeAllMenus() {
  activeMenuId.value = null
  menuTargetConv.value = null
  dropdownOpen.value = false
  toolsMenuOpen.value = false
  showUserMenu.value = false
  showMoveToGroupMenu.value = false
  moveTargetConv.value = null
  closeSwipeActions()
}

function handleTouchStart(e: TouchEvent, conversationId: string) {
  if (selectionModeActive.value || editingTitleId.value || e.touches.length !== 1) {
    return
  }

  const touch = e.touches[0]
  swipeTrackingId.value = conversationId
  swipeStartX.value = touch.clientX
  swipeStartY.value = touch.clientY
  swipeStartOffset.value = swipedConversationId.value === conversationId ? swipeOffset.value : 0
  isSwipeTracking.value = true
  isSwipeDragging.value = false

  if (swipedConversationId.value && swipedConversationId.value !== conversationId) {
    swipedConversationId.value = null
    swipeOffset.value = 0
  }
}

function handleTouchEnd() {
  if (!isSwipeTracking.value) {
    return
  }

  if (isSwipeDragging.value) {
    suppressConversationClickUntil = Date.now() + 300
    if (swipeOffset.value <= -SWIPE_ACTION_WIDTH / 2 && swipeTrackingId.value) {
      swipedConversationId.value = swipeTrackingId.value
      swipeOffset.value = -SWIPE_ACTION_WIDTH
    } else {
      closeSwipeActions()
      return
    }
  }

  swipeTrackingId.value = null
  isSwipeTracking.value = false
  isSwipeDragging.value = false
}

function handleTouchMove(e: TouchEvent, conversationId: string) {
  if (
    selectionModeActive.value ||
    !isSwipeTracking.value ||
    swipeTrackingId.value !== conversationId ||
    e.touches.length !== 1
  ) {
    return
  }

  const touch = e.touches[0]
  const deltaX = touch.clientX - swipeStartX.value
  const deltaY = touch.clientY - swipeStartY.value

  if (!isSwipeDragging.value) {
    if (Math.abs(deltaY) > 10 && Math.abs(deltaY) > Math.abs(deltaX)) {
      swipeTrackingId.value = null
      isSwipeTracking.value = false
      return
    }

    if (Math.abs(deltaX) < 10) {
      return
    }

    if (deltaX > 0 && swipeStartOffset.value === 0) {
      swipeTrackingId.value = null
      isSwipeTracking.value = false
      return
    }

    isSwipeDragging.value = true
  }

  e.preventDefault()
  swipedConversationId.value = conversationId
  swipeOffset.value = Math.max(-SWIPE_ACTION_WIDTH, Math.min(0, swipeStartOffset.value + deltaX))
}

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
    if (result.success) {
      showToast(result.path ? `已保存到 ${result.path}` : '导出成功', 'success')
    } else {
      showToast(result.error || '导出失败', 'error')
    }
  } catch (e) {
    console.error('Failed to export conversation:', e)
    showToast('导出失败', 'error')
  }
}

async function handleMenuDelete() {
  if (!menuTargetConv.value) return
  const convId = menuTargetConv.value.id
  closeAllMenus()
  if (!await showConfirm({ message: '确定要删除这条对话吗？', danger: true, confirmText: '删除' })) return
  await chatStore.deleteConversation(convId)
}

function handleSwipeEdit(id: string, title: string) {
  closeSwipeActions()
  startEditTitle({ id, title })
}

async function handleSwipeExport(id: string) {
  closeSwipeActions()
  if (!assistantStore.currentAssistantId) return
  try {
    const result = await chatApi.exportConversations(assistantStore.currentAssistantId, [id])
    if (result.success) {
      showToast(result.path ? `已保存到 ${result.path}` : '导出成功', 'success')
    } else {
      showToast(result.error || '导出失败', 'error')
    }
  } catch (e) {
    console.error('Failed to export conversation:', e)
    showToast('导出失败', 'error')
  }
}

async function handleSwipeDelete(id: string) {
  closeSwipeActions()
  if (!await showConfirm({ message: '确定要删除这条对话吗？', danger: true, confirmText: '删除' })) return
  await chatStore.deleteConversation(id)
}

// ─── Group Management ───────────────────────────────────────────

const PRESET_COLORS = [
  '#ef4444', '#f97316', '#f59e0b', '#84cc16',
  '#10b981', '#06b6d4', '#3b82f6', '#6366f1',
  '#8b5cf6', '#d946ef', '#f43f5e', '#6b7280'
]

function openCreateGroupDialog() {
  editingGroup.value = null
  newGroupName.value = ''
  newGroupColor.value = '#3b82f6'
  newGroupAssistantId.value = assistantStore.currentAssistantId
  showGroupDialog.value = true
  closeAllMenus()
}

function openEditGroupDialog(group: ConversationGroup) {
  editingGroup.value = group
  newGroupName.value = group.name
  newGroupColor.value = group.color
  newGroupAssistantId.value = group.assistant_id || assistantStore.currentAssistantId
  showGroupDialog.value = true
  closeAllMenus()
}

/** 分组被移动到其他助手后，从当前助手的列表/拖拽层移除它及其会话。 */
function removeGroupFromCurrentAssistant(groupId: string) {
  const convIds = chatStore.conversations.filter(c => c.group_id === groupId).map(c => c.id)
  for (const cid of convIds) chatStore.removeConversationFromList(cid)
  groupStore.groups = groupStore.groups.filter(g => g.id !== groupId)
  delete dragLists[groupId]
}

async function saveGroup() {
  const name = newGroupName.value.trim()
  if (!name) {
    showToast('分组名称不能为空', 'error')
    return
  }

  try {
    if (editingGroup.value) {
      // 先修改名称/颜色，再移动助手——移动失败时分组不会从当前视图消失。
      await groupStore.updateGroup(editingGroup.value.id, {
        name,
        color: newGroupColor.value
      })
      if (newGroupAssistantId.value && newGroupAssistantId.value !== editingGroup.value.assistant_id) {
        try {
          await groupStore.moveGroupToAssistant(editingGroup.value.id, newGroupAssistantId.value)
        } catch (e) {
          console.error('Failed to move group:', e)
          // 移动失败：重载分组列表，恢复与服务器一致的状态。
          await groupStore.loadGroups(assistantStore.currentAssistantId || undefined)
          throw e
        }
        if (newGroupAssistantId.value !== assistantStore.currentAssistantId) {
          removeGroupFromCurrentAssistant(editingGroup.value.id)
        }
      }
      showToast('分组已更新', 'success')
    } else {
      const created = await groupStore.createGroup(
        name,
        newGroupColor.value,
        newGroupAssistantId.value || assistantStore.currentAssistantId
      )
      if (created.assistant_id && created.assistant_id !== assistantStore.currentAssistantId) {
        groupStore.groups = groupStore.groups.filter(g => g.id !== created.id)
      }
      showToast('分组已创建', 'success')
    }
    showGroupDialog.value = false
    nextTick(() => syncDragLists())
  } catch (e) {
    console.error('Failed to save group:', e)
    showToast('保存失败', 'error')
  }
}

function openDeleteGroupDialog(group: ConversationGroup) {
  deletingGroup.value = group
  deleteGroupWithConversations.value = false
  showDeleteGroupDialog.value = true
  closeAllMenus()
}

async function confirmDeleteGroup() {
  if (!deletingGroup.value) return
  const groupId = deletingGroup.value.id
  const shouldDeleteConvs = deleteGroupWithConversations.value
  try {
    await groupStore.deleteGroup(
      groupId,
      shouldDeleteConvs
    )
    if (shouldDeleteConvs) {
      chatStore.conversations = chatStore.conversations.filter(c => c.group_id !== groupId)
    } else {
      for (const conv of chatStore.conversations) {
        if (conv.group_id === groupId) {
          conv.group_id = null
        }
      }
    }
    showToast('分组已删除', 'success')
    showDeleteGroupDialog.value = false
    deletingGroup.value = null
  } catch (e) {
    console.error('Failed to delete group:', e)
    showToast('删除失败', 'error')
  }
}

// ─── Move to Group ─────────────────────────────────────────────

function openMoveToGroupMenu(event: MouseEvent) {
  if (!menuTargetConv.value) return
  moveTargetConv.value = menuTargetConv.value
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const SUB_MENU_WIDTH = 180
  const PADDING = 8
  const GAP = 4

  let top = rect.top
  let left = rect.right + GAP

  // If sub-menu would overflow right, show to the left of the main menu
  if (left + SUB_MENU_WIDTH > window.innerWidth - PADDING) {
    left = rect.left - SUB_MENU_WIDTH - GAP
    if (left < PADDING) left = PADDING
  }

  moveMenuStyle.value = { top: `${top}px`, left: `${left}px` }
  showMoveToGroupMenu.value = true

  // After render, measure actual height and adjust if overflows bottom
  nextTick(() => {
    const menuEl = document.querySelector('.move-group-menu') as HTMLElement
    if (!menuEl) return
    const menuRect = menuEl.getBoundingClientRect()
    if (menuRect.bottom > window.innerHeight - PADDING) {
      let newTop = window.innerHeight - menuRect.height - PADDING
      if (newTop < PADDING) newTop = PADDING
      moveMenuStyle.value = { top: `${newTop}px`, left: `${left}px` }
    }
  })
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

function closeMoveToGroupMenu() {
  showMoveToGroupMenu.value = false
  moveTargetConv.value = null
}

async function moveConversationToGroup(convId: string, groupId: string | null) {
  try {
    await groupStore.moveConversation(convId, groupId)
    const conv = chatStore.conversations.find(c => c.id === convId)
    if (conv) {
      conv.group_id = groupId
    }
    closeMoveToGroupMenu()
    closeAllMenus()
    showToast(groupId ? '已移动到分组' : '已移出分组', 'success')
    nextTick(() => syncDragLists())
  } catch (e) {
    console.error('Failed to move conversation:', e)
    showToast('移动失败', 'error')
  }
}

async function removeConversationFromGroup(convId: string) {
  await moveConversationToGroup(convId, null)
}

/** 把单个对话移动到另一个助手（目标助手内未分组）。 */
async function moveConversationToAssistant(convId: string, assistantId: string) {
  try {
    await groupStore.moveConversation(convId, null, assistantId)
    chatStore.removeConversationFromList(convId)
    const target = assistantStore.getAssistantById(assistantId)
    closeMoveToGroupMenu()
    closeAllMenus()
    showToast(target ? `已移动到助手「${target.name}」` : '已移动到其他助手', 'success')
    nextTick(() => syncDragLists())
  } catch (e) {
    console.error('Failed to move conversation:', e)
    showToast('移动失败', 'error')
  }
}

// ─── Drag & Drop ───────────────────────────────────────────────

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
  const isGroupDrag = fromEl?.classList?.contains('groups-directory-content') === true

  collapsedBeforeDrag.value = new Set(groupStore.collapsedGroups)

  if (isGroupDrag) {
    return
  }

  // Collapsed groups are expanded visually via CSS (.sidebar-dragging .group-convs-collapsed)
  // which sets grid-template-rows: 1fr and min-height so SortableJS can register them as
  // drop targets. We do NOT call groupStore.expandGroup() here to avoid triggering
  // forceReinit on the source list which would cancel the drag.

  groupsDirectoryWasCollapsed.value = groupsDirectoryCollapsed.value
  if (groupsDirectoryCollapsed.value) {
    groupsDirectoryCollapsed.value = false
  }

  startEdgeScroll()
  document.addEventListener('mousemove', handleDragMouseMove)
  document.addEventListener('touchmove', handleDragMouseMove, { passive: true })
}

function onConversationDragEnd(event: any) {
  // ── Determine target group from DOM ──────────────────────────
  const toEl = event?.to as HTMLElement | undefined
  let targetGroupEl = toEl?.closest('.conversation-group') as HTMLElement | null
  let targetGroupId = targetGroupEl?.getAttribute('data-group-id') || null

  // If SortableJS did not detect a cross-list move (to === from) but the
  // cursor is hovering over a drag-expanded group, treat it as a drop into
  // that group.
  if (!targetGroupId && dragExpandedGroupId.value) {
    const fromEl = event?.from as HTMLElement | undefined
    const fromGroupEl = fromEl?.closest('.conversation-group') as HTMLElement | null
    const fromGroupId = fromGroupEl?.getAttribute('data-group-id') || null
    if (dragExpandedGroupId.value !== fromGroupId) {
      targetGroupId = dragExpandedGroupId.value
      targetGroupEl = document.querySelector(`[data-group-id="${targetGroupId}"]`)
    }
  }

  const finalTargetId = targetGroupId

  // ── IMMEDIATELY reconcile DOM state ──────────────────────────
  // Must run BEFORE Vue nextTick to prevent Vue re-render from
  // overwriting SortableJS's DOM manipulation.  First remove any
  // SortableJS ghost/clone artifacts, then read the clean DOM.
  deduplicateDragElements()

  // Handle manual move when we have a target group but the dragged
  // item is NOT already in the target's DOM.  This covers two cases:
  // 1. SortableJS rejected a cross-list drop (e.g. drop on group header
  //    where the group-level SortableList has put:false).
  // 2. SortableJS did not detect a cross-list move (event.to === event.from)
  //    but the cursor was over a different group's header.
  if (finalTargetId) {
    const fromEl = event?.from as HTMLElement | undefined
    const sourceKey = fromEl?.getAttribute('data-sort-key') || ''
    const itemKey = (event?.item as HTMLElement)?.getAttribute('data-key')

    // Check if item is already in target group's DOM
    const alreadyInTarget = itemKey
      ? document.querySelector(`[data-group-id="${finalTargetId}"] [data-key="${itemKey}"]`)
      : null

    if (itemKey && !alreadyInTarget) {
      if (sourceKey.startsWith('group:')) {
        const sourceGroupId = sourceKey.replace('group:', '')
        const sourceList = dragLists[sourceGroupId]
        const targetList = dragLists[finalTargetId]
        if (sourceList && targetList) {
          const idx = sourceList.findIndex((c: Conversation) => c.id === itemKey)
          if (idx !== -1) {
            const [conv] = sourceList.splice(idx, 1)
            conv.group_id = finalTargetId
            targetList.push(conv)
          }
        }
      } else {
        // Moving from ungrouped to a group
        const targetList = dragLists[finalTargetId]
        if (targetList) {
          for (const [key, list] of Object.entries(dragLists)) {
            if (key === finalTargetId) continue
            if (!Array.isArray(list)) continue
            const idx = list.findIndex((c: Conversation) => c.id === itemKey)
            if (idx !== -1) {
              const [conv] = list.splice(idx, 1)
              conv.group_id = finalTargetId
              targetList.push(conv)
              break
            }
          }
        }
      }
    }
  }

  // Reconcile: read actual DOM positions, update dragLists,
  // persist to backend via batch reorder API.
  // Set isDragging false BEFORE processDragChanges so that Vue
  // re-renders triggered by chatStore updates use the reconciled
  // dragLists instead of stale state.
  reconcileDragState()
  isDragging.value = false
  processDragChanges()

    // ── Cleanup after Vue nextTick ───────────────────────────────
    nextTick(() => {
      // Run deduplication again in case Vue re-render created artifacts
      deduplicateDragElements()

      if (groupsDirectoryWasCollapsed.value) {
        groupsDirectoryCollapsed.value = true
      }
      groupsDirectoryWasCollapsed.value = false

      // Expand the target group so the user can see the newly moved conversation
      if (finalTargetId) {
        groupStore.expandGroup(finalTargetId)
      }

      stopEdgeScroll()
      document.removeEventListener('mousemove', handleDragMouseMove)
      document.removeEventListener('touchmove', handleDragMouseMove)
      cancelDragMoveRaf()

      if (dragHoverTimer) {
        clearTimeout(dragHoverTimer)
        dragHoverTimer = null
      }
      dragHoverTargetGroupId = null
      dragExpandedGroupId.value = null

      nextTick(() => syncDragLists())
    })
}

// After Vue re-renders from dragLists, there may be duplicate DOM elements:
// one from SortableJS's DOM manipulation and one from Vue's virtual DOM.
// This function removes the SortableJS artifacts, keeping only Vue-rendered elements.
function deduplicateDragElements() {
  const containerElements = new Map<Element, Map<string, Element>>()
  const allDraggableEls = document.querySelectorAll('[data-draggable="true"][data-key]:not(.sortable-ghost):not(.sortable-fallback)')
  allDraggableEls.forEach(el => {
    const key = el.getAttribute('data-key')
    if (!key) return
    const parent = el.parentElement
    if (!parent) return
    if (!containerElements.has(parent)) {
      containerElements.set(parent, new Map())
    }
    const containerMap = containerElements.get(parent)!
    if (containerMap.has(key)) {
      el.remove()
    } else {
      containerMap.set(key, el)
    }
  })
}

// Reconcile dragLists with actual DOM positions after SortableJS drag.
// SortableJS may move DOM elements without firing onAdd/onRemove events
// (e.g. when forceFallback interacts with Vue re-renders). This function
// detects the actual DOM state and updates dragLists accordingly.
function reconcileDragState() {
  const groupIdByConvId = new Map<string, string | null>()

  for (const group of groupStore.groups) {
    const container = document.querySelector(`[data-group-id="${group.id}"] .group-conv-drag-list`)
    if (container) {
      const convEls = container.querySelectorAll('[data-draggable="true"][data-key]:not(.sortable-ghost):not(.sortable-fallback)')
      convEls.forEach(el => {
        const convId = el.getAttribute('data-key')
        if (convId) groupIdByConvId.set(convId, group.id)
      })
    }
  }

  const ungroupedContainers = document.querySelectorAll('.ungrouped-conv-drag-list')
  ungroupedContainers.forEach(container => {
    const convEls = container.querySelectorAll('[data-draggable="true"][data-key]:not(.sortable-ghost):not(.sortable-fallback)')
    convEls.forEach(el => {
      const convId = el.getAttribute('data-key')
      if (convId && !groupIdByConvId.has(convId)) {
        groupIdByConvId.set(convId, null)
      }
    })
  })

  const emptyZone = document.querySelector('.ungrouped-empty-drag-list')
  if (emptyZone) {
    const convEls = emptyZone.querySelectorAll('[data-draggable="true"][data-key]:not(.sortable-ghost):not(.sortable-fallback)')
    convEls.forEach(el => {
      const convId = el.getAttribute('data-key')
      if (convId) groupIdByConvId.set(convId, null)
    })
  }

  for (const conv of chatStore.conversations) {
    const targetGroupId = groupIdByConvId.get(conv.id)
    if (targetGroupId !== undefined) {
      conv.group_id = targetGroupId
    }
  }

  const groupIds = new Set(groupStore.groups.map(g => g.id))
  const ungroupedConvs: Conversation[] = []
  for (const group of groupStore.groups) {
    dragLists[group.id] = []
  }
  for (const conv of chatStore.conversations) {
    const gid = groupIdByConvId.get(conv.id) ?? conv.group_id
    if (gid && groupIds.has(gid)) {
      conv.group_id = gid
      dragLists[gid].push(conv)
    } else {
      conv.group_id = null
      ungroupedConvs.push(conv)
    }
  }
  const timeCategories = categorizeConversationsByTime(ungroupedConvs)
  for (const [key, convs] of timeCategories) {
    dragLists[key] = convs
  }
  emptyUngroupedList.value = []
}

function onGroupDragEnd(event: any) {
  isDragging.value = false

  // Restore original collapsed state
  for (const gid of collapsedBeforeDrag.value) {
    groupStore.collapseGroup(gid)
  }
  collapsedBeforeDrag.value = new Set()

  // Clear drag hover
  dragExpandedGroupId.value = null
  dragHoverTargetGroupId = null
  if (dragHoverTimer) {
    clearTimeout(dragHoverTimer)
    dragHoverTimer = null
  }

  stopEdgeScroll()
  document.removeEventListener('mousemove', handleDragMouseMove)
  document.removeEventListener('touchmove', handleDragMouseMove)
  cancelDragMoveRaf()

  if (!event.moved) return
  const items = groupStore.groups.map((group, index) => ({
    id: group.id,
    sort_order: index
  }))
  try {
    groupStore.reorderGroups(items)
  } catch (e) {
    console.error('Failed to reorder groups:', e)
  }
}

function processDragChanges() {
  const items: { id: string; sort_order: number; group_id: string | null }[] = []
  const groupIds = new Set(groupStore.groups.map(g => g.id))

  for (const conv of emptyUngroupedList.value) {
    conv.group_id = null
    items.push({ id: conv.id, sort_order: items.length, group_id: null })
  }

  for (const [key, convs] of Object.entries(dragLists)) {
    if (!Array.isArray(convs) || convs.length === 0) continue
    const isGroup = groupIds.has(key)
    for (let i = 0; i < convs.length; i++) {
      const conv = convs[i]
      if (isGroup) {
        conv.group_id = key
        conv.sort_order = i
        items.push({ id: conv.id, sort_order: i, group_id: key })
      } else {
        conv.group_id = null
        items.push({ id: conv.id, sort_order: items.length, group_id: null })
      }
    }
  }

  if (items.length > 0) {
    const itemMap = new Map(items.map(it => [it.id, it.group_id]))
    for (const conv of chatStore.conversations) {
      const newGroupId = itemMap.get(conv.id)
      if (newGroupId !== undefined) {
        conv.group_id = newGroupId
      }
    }

    groupStore.reorderConversations(items).then(() => {
      nextTick(() => syncDragLists())
    }).catch((e: Error) => {
      console.error('Failed to reorder conversations:', e)
    })
  }
}

// ─── Drag Hover Expansion ──────────────────────────────────────

let dragHoverTimer: ReturnType<typeof setTimeout> | null = null
let dragHoverTargetGroupId: string | null = null
const DRAG_HOVER_DELAY = 300

function handleDragMouseMove(e: MouseEvent | TouchEvent) {
  pendingDragMoveEvent = e
  if (dragMoveRaf !== null) {
    return
  }
  dragMoveRaf = requestAnimationFrame(() => {
    dragMoveRaf = null
    const event = pendingDragMoveEvent
    pendingDragMoveEvent = null
    if (!event || !isDragging.value) return
    processDragMouseMove(event)
  })
}

function processDragMouseMove(e: MouseEvent | TouchEvent) {
  if (!isDragging.value) return

  const clientX = 'touches' in e ? e.touches[0].clientX : e.clientX
  const clientY = 'touches' in e ? e.touches[0].clientY : e.clientY

  // First check group headers (always visible, even when collapsed)
  let foundGroupId: string | null = null
  const groupHeaders = document.querySelectorAll('.group-header')
  for (const header of groupHeaders) {
    const rect = header.getBoundingClientRect()
    if (clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom) {
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
        if (rect.height > 0 && clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom) {
          foundGroupId = dragExpandedGroupId.value
        }
      }
    }
  }

  if (foundGroupId) {
    // If hovering over a different group than the current hover target
    if (dragHoverTargetGroupId !== foundGroupId) {
      // Clear previous timer
      if (dragHoverTimer) {
        clearTimeout(dragHoverTimer)
        dragHoverTimer = null
      }
      dragHoverTargetGroupId = foundGroupId
      
      // If this group is already expanded, no need for timer
      if (dragExpandedGroupId.value === foundGroupId) {
        return
      }
      
      // Start new hover timer
      dragHoverTimer = setTimeout(() => {
        if (dragHoverTargetGroupId === foundGroupId) {
          dragExpandedGroupId.value = foundGroupId
        }
        dragHoverTimer = null
      }, DRAG_HOVER_DELAY)
    }
    // If hovering over the same group and it's already expanded, do nothing
  } else {
    // Not hovering over any group - clear timer and collapse
    if (dragHoverTimer) {
      clearTimeout(dragHoverTimer)
      dragHoverTimer = null
    }
    dragHoverTargetGroupId = null
    dragExpandedGroupId.value = null
  }
}

function cancelDragMoveRaf() {
  if (dragMoveRaf !== null) {
    cancelAnimationFrame(dragMoveRaf)
    dragMoveRaf = null
  }
  pendingDragMoveEvent = null
}

// ─── Edge Scroll ───────────────────────────────────────────────

function trackPointerPosition(e: MouseEvent | TouchEvent) {
  if ('touches' in e && e.touches.length > 0) {
    lastPointerPosition.value = { x: e.touches[0].clientX, y: e.touches[0].clientY }
  } else if (!('touches' in e)) {
    lastPointerPosition.value = { x: e.clientX, y: e.clientY }
  }
}

function startEdgeScroll() {
  if (edgeScrollRaf) return
  lastPointerPosition.value = null

  document.addEventListener('mousemove', trackPointerPosition)
  document.addEventListener('touchmove', trackPointerPosition, { passive: true })

  const scrollStep = () => {
    if (!conversationListEl.value || !lastPointerPosition.value || (!isDragging.value && !isMarqueeSelecting.value)) {
      stopEdgeScroll()
      return
    }

    const rect = conversationListEl.value.getBoundingClientRect()
    const { x, y } = lastPointerPosition.value

    const topDist = y - rect.top
    const bottomDist = rect.bottom - y

    if (topDist >= 0 && topDist < EDGE_SCROLL_THRESHOLD) {
      const speed = Math.max(2, EDGE_SCROLL_SPEED * (1 - topDist / EDGE_SCROLL_THRESHOLD))
      conversationListEl.value.scrollTop -= speed
    } else if (bottomDist >= 0 && bottomDist < EDGE_SCROLL_THRESHOLD) {
      const speed = Math.max(2, EDGE_SCROLL_SPEED * (1 - bottomDist / EDGE_SCROLL_THRESHOLD))
      conversationListEl.value.scrollTop += speed
    }

    edgeScrollRaf = requestAnimationFrame(scrollStep)
  }

  edgeScrollRaf = requestAnimationFrame(scrollStep)
}

function stopEdgeScroll() {
  if (edgeScrollRaf) {
    cancelAnimationFrame(edgeScrollRaf)
    edgeScrollRaf = null
  }
  document.removeEventListener('mousemove', trackPointerPosition)
  document.removeEventListener('touchmove', trackPointerPosition)
  lastPointerPosition.value = null
}

// ─── Marquee Selection ─────────────────────────────────────────

function onConversationListMouseDown(e: MouseEvent) {
  if (!selectionModeActive.value) return
  if (e.button !== 0) return

  const target = e.target as HTMLElement

  // Don't start marquee if clicking on interactive elements
  if (
    target.closest('input[type="checkbox"]') ||
    target.closest('button') ||
    target.closest('.group-header') ||
    target.closest('.conversation-menu') ||
    target.closest('.time-category-header')
  ) {
    return
  }

  const conversationList = conversationListEl.value
  if (!conversationList) return

  // Check if click is within the conversation list area
  const listRect = conversationList.getBoundingClientRect()
  if (e.clientX < listRect.left || e.clientX > listRect.right || e.clientY < listRect.top || e.clientY > listRect.bottom) {
    return
  }

  // Store initial position but don't start marquee yet
  marqueeAnchor.value = { x: e.clientX, y: e.clientY }
  marqueeEnd.value = null
  marqueePreviousSelection.value = new Set(selectedConversationIds.value)

  document.addEventListener('mousemove', onMarqueeMouseMove)
  document.addEventListener('mouseup', onMarqueeMouseUp)

  e.preventDefault()
}

function onMarqueeMouseMove(e: MouseEvent) {
  pendingMarqueeMoveEvent = e
  if (marqueeMoveRaf !== null) {
    return
  }
  marqueeMoveRaf = requestAnimationFrame(() => {
    marqueeMoveRaf = null
    const event = pendingMarqueeMoveEvent
    pendingMarqueeMoveEvent = null
    if (!event || !marqueeAnchor.value || !conversationListEl.value) return
    processMarqueeMouseMove(event)
  })
}

function processMarqueeMouseMove(e: MouseEvent) {
  if (!marqueeAnchor.value || !conversationListEl.value) return

  const dx = e.clientX - marqueeAnchor.value.x
  const dy = e.clientY - marqueeAnchor.value.y

  // Only start marquee if moved more than 5 pixels
  if (!isMarqueeSelecting.value && Math.sqrt(dx * dx + dy * dy) < 5) {
    return
  }

  if (!isMarqueeSelecting.value) {
    isMarqueeSelecting.value = true
    startEdgeScroll()
  }

  marqueeEnd.value = { x: e.clientX, y: e.clientY }

  const anchor = marqueeAnchor.value
  const left = Math.min(anchor.x, e.clientX)
  const top = Math.min(anchor.y, e.clientY)
  const right = Math.max(anchor.x, e.clientX)
  const bottom = Math.max(anchor.y, e.clientY)

  const rows = conversationListEl.value.querySelectorAll('.conversation-row')
  const newlySelected = new Set<string>()

  // If modifier key is pressed, start from previous selection
  const useModifier = e.shiftKey || e.ctrlKey || e.metaKey
  if (useModifier) {
    for (const id of marqueePreviousSelection.value) {
      newlySelected.add(id)
    }
  }

  for (const row of rows) {
    const rowRect = row.getBoundingClientRect()
    const id = row.querySelector('.conversation-item')?.getAttribute('data-id')
    if (!id) continue

    // Check if row intersects with marquee
    const intersects = rowRect.left < right && rowRect.right > left && rowRect.top < bottom && rowRect.bottom > top

    if (intersects) {
      if (useModifier && marqueePreviousSelection.value.has(id)) {
        newlySelected.delete(id)
      } else {
        newlySelected.add(id)
      }
    }
  }

  selectedConversationIds.value = newlySelected
}

function cancelMarqueeMoveRaf() {
  if (marqueeMoveRaf !== null) {
    cancelAnimationFrame(marqueeMoveRaf)
    marqueeMoveRaf = null
  }
  pendingMarqueeMoveEvent = null
}

function onMarqueeMouseUp() {
  cancelMarqueeMoveRaf()

  if (isMarqueeSelecting.value) {
    stopEdgeScroll()
    suppressClickAfterMarqueeUntil = Date.now() + 50
  }

  isMarqueeSelecting.value = false
  marqueeAnchor.value = null
  marqueeEnd.value = null

  document.removeEventListener('mousemove', onMarqueeMouseMove)
  document.removeEventListener('mouseup', onMarqueeMouseUp)
}

// Batch operation popup
const batchPopupVisible = ref(false)
const batchPopupMode = ref<'export' | 'delete'>('export')
const batchPopupSelected = ref<Set<string>>(new Set())
const batchPopupPending = ref(false)
const batchPopupProgress = ref('')
// When the popup is opened from the assistant modal, target that assistant's
// conversations instead of the currently active assistant's list.
const batchPopupAssistantId = ref<string | null>(null)
const batchPopupConversations = ref<Conversation[]>([])

const batchPopupList = computed(() =>
  batchPopupAssistantId.value ? batchPopupConversations.value : chatStore.conversations
)

async function openBatchExport(assistantId: string | null = null) {
  toolsMenuOpen.value = false
  batchPopupMode.value = 'export'
  batchPopupSelected.value = new Set()
  batchPopupPending.value = false
  batchPopupProgress.value = ''
  batchPopupAssistantId.value = assistantId
  if (assistantId) {
    batchPopupConversations.value = await assistantApi.getAssistantConversations(assistantId)
  } else {
    batchPopupConversations.value = []
  }
  batchPopupVisible.value = true
}

async function openBatchDelete(assistantId: string | null = null) {
  toolsMenuOpen.value = false
  batchPopupMode.value = 'delete'
  batchPopupSelected.value = new Set()
  batchPopupPending.value = false
  batchPopupProgress.value = ''
  batchPopupAssistantId.value = assistantId
  if (assistantId) {
    batchPopupConversations.value = await assistantApi.getAssistantConversations(assistantId)
  } else {
    batchPopupConversations.value = []
  }
  batchPopupVisible.value = true
}

function closeBatchPopup() {
  batchPopupVisible.value = false
  batchPopupSelected.value = new Set()
  showBulkDeleteDialog.value = false
  bulkDeleteContext.value = 'selection'
  batchPopupAssistantId.value = null
  batchPopupConversations.value = []
}

function toggleBatchPopupSelect(id: string) {
  const next = new Set(batchPopupSelected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  batchPopupSelected.value = next
}

function toggleBatchPopupSelectAll() {
  if (batchPopupSelected.value.size === batchPopupList.value.length) {
    batchPopupSelected.value = new Set()
  } else {
    batchPopupSelected.value = new Set(batchPopupList.value.map(c => c.id))
  }
}

async function handleBatchPopupConfirm() {
  const ids = Array.from(batchPopupSelected.value)
  if (ids.length === 0) return
  if (batchPopupMode.value === 'export') {
    batchPopupPending.value = true
    try {
      batchPopupProgress.value = '导出中...'
      const targetAssistantId = batchPopupAssistantId.value || assistantStore.currentAssistantId || ''
      await chatApi.exportConversations(targetAssistantId, ids)
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
    batchPopupConversations.value = batchPopupConversations.value.filter(c => !ids.includes(c.id))
    await chatStore.loadConversations(assistantStore.currentAssistantId)
    if (currentId && ids.includes(currentId) && route.path === '/') {
      await navigateWithMobileHistory(router, '/', { replace: true })
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

function enterExportMode() {
  closeAllMenus()
  cancelEditTitle()
  selectionMode.value = 'export'
  selectedConversationIds.value = new Set()
  selectedGroupIds.value = new Set()
  deleteGroupWithConversations.value = false
  selectionProgress.value = ''
}

function enterDeleteMode() {
  closeAllMenus()
  cancelEditTitle()
  selectionMode.value = 'delete'
  selectedConversationIds.value = new Set()
  selectedGroupIds.value = new Set()
  deleteGroupWithConversations.value = false
  selectionProgress.value = ''
  showBulkDeleteDialog.value = false
  bulkDeleteConfirmMessage.value = ''
  bulkDeleteWithConversations.value = false
  bulkDeleteContext.value = 'selection'
}

function exitSelectionMode() {
  closeSwipeActions()
  selectionMode.value = null
  selectedConversationIds.value = new Set()
  selectedGroupIds.value = new Set()
  deleteGroupWithConversations.value = false
  selectionPending.value = false
  selectionProgress.value = ''
}

function toggleSelectAll() {
  if (selectedConversationIds.value.size === chatStore.conversations.length) {
    selectedConversationIds.value = new Set()
  } else {
    selectedConversationIds.value = new Set(chatStore.conversations.map(c => c.id))
  }
}

function toggleSelect(id: string) {
  const nextSelected = new Set(selectedConversationIds.value)
  if (nextSelected.has(id)) {
    nextSelected.delete(id)
  } else {
    nextSelected.add(id)
  }
  selectedConversationIds.value = nextSelected
}

function toggleGroupSelect(groupId: string) {
  const nextSelected = new Set(selectedGroupIds.value)
  if (nextSelected.has(groupId)) {
    nextSelected.delete(groupId)
  } else {
    nextSelected.add(groupId)
  }
  selectedGroupIds.value = nextSelected
}

async function handleSelectionConfirm() {
  if (isExportMode.value) {
    await handleExport()
    return
  }

  if (isDeleteMode.value) {
    await handleBulkDelete()
  }
}

async function handleExport() {
  if (selectedConversationIds.value.size === 0 || !assistantStore.currentAssistantId) return

  selectionPending.value = true
  selectionProgress.value = '正在导出...'

  try {
    const result = await chatApi.exportConversations(
      assistantStore.currentAssistantId,
      Array.from(selectedConversationIds.value)
    )
    if (result.success) {
      selectionProgress.value = result.path ? `已保存到 ${result.path}` : '导出成功！'
    } else {
      selectionProgress.value = result.error || '导出失败'
    }
    setTimeout(() => {
      exitSelectionMode()
    }, 1200)
  } catch (e) {
    console.error('Failed to export:', e)
    selectionProgress.value = '导出失败'
  } finally {
    selectionPending.value = false
  }
}

async function handleBulkDelete() {
  if (!hasAnySelection.value) return

  const convCount = selectedConversationIds.value.size
  const groupCount = selectedGroupIds.value.size

  let confirmMessage = '确定要删除选中的'
  if (convCount > 0 && groupCount > 0) {
    confirmMessage += `${convCount}个对话和${groupCount}个分组吗？`
  } else if (convCount > 0) {
    confirmMessage += `${convCount}个对话吗？`
  } else {
    confirmMessage += `${groupCount}个分组吗？`
  }

  bulkDeleteConfirmMessage.value = confirmMessage
  bulkDeleteWithConversations.value = false
  showBulkDeleteDialog.value = true
}

async function confirmBulkDelete() {
  if (bulkDeleteContext.value === 'batchPopup') {
    await executeBatchPopupDelete()
    return
  }

  const convCount = selectedConversationIds.value.size
  const groupCount = selectedGroupIds.value.size

  showBulkDeleteDialog.value = false
  selectionPending.value = true
  selectionProgress.value = '正在删除...'

  try {
    if (bulkDeleteWithConversations.value && groupCount > 0) {
      const groupIds = Array.from(selectedGroupIds.value)
      const convsInGroups = chatStore.conversations.filter(c => c.group_id && selectedGroupIds.value.has(c.group_id))
      const convIdsInGroups = new Set(convsInGroups.map(c => c.id))
      const allConvIds = new Set([...selectedConversationIds.value, ...convIdsInGroups])
      if (allConvIds.size > 0) {
        const ids = Array.from(allConvIds)
        const currentId = chatStore.currentConversationId
        await chatStore.bulkDeleteConversations(ids)
        if (currentId && ids.includes(currentId) && route.path === '/') {
          await navigateWithMobileHistory(router, '/', { replace: true })
        }
      }
      await groupStore.bulkDeleteGroups(groupIds, false)
      for (const gid of groupIds) {
        chatStore.conversations = chatStore.conversations.filter(c => c.group_id !== gid)
      }
    } else {
      if (convCount > 0) {
        const ids = Array.from(selectedConversationIds.value)
        const currentId = chatStore.currentConversationId
        await chatStore.bulkDeleteConversations(ids)
        if (currentId && ids.includes(currentId) && route.path === '/') {
          await navigateWithMobileHistory(router, '/', { replace: true })
        }
      }
      if (groupCount > 0) {
        const groupIds = Array.from(selectedGroupIds.value)
        await groupStore.bulkDeleteGroups(groupIds, false)
        for (const gid of groupIds) {
          for (const conv of chatStore.conversations) {
            if (conv.group_id === gid) {
              conv.group_id = null
            }
          }
        }
      }
    }
    selectionProgress.value = '删除成功！'
    setTimeout(() => {
      exitSelectionMode()
    }, 1200)
  } catch (e) {
    console.error('Failed to bulk delete:', e)
    selectionProgress.value = '删除失败'
  } finally {
    selectionPending.value = false
  }
}

function handleSwipeSaveToNote(convId: string, convTitle: string) {
  closeSwipeActions()
  saveToNoteConvId.value = convId
  saveToNoteConvTitle.value = convTitle
  notesStore.loadNotebooks()
  showNotebookPicker.value = true
}

function handleMenuSaveToNote() {
  if (!menuTargetConv.value) return
  const conv = menuTargetConv.value
  closeAllMenus()
  saveToNoteConvId.value = conv.id
  saveToNoteConvTitle.value = conv.title || '新对话'
  notesStore.loadNotebooks()
  showNotebookPicker.value = true
}

async function handleNotebookSelected(notebookId: string) {
  showNotebookPicker.value = false
  const convId = saveToNoteConvId.value
  if (!convId) return
  try {
    const conversation = await chatApi.getConversation(convId)
    const msgs = conversation.messages || []
    if (msgs.length === 0) {
      showToast('对话无消息内容', 'error')
      return
    }
    const content = msgs.map(m => {
      const role = m.role === 'user' ? '**用户**' : '**助手**'
      return `${role}\n\n${m.content}`
    }).join('\n\n---\n\n')
    const title = saveToNoteConvTitle.value || '对话记录'
    await notesStore.createNote(notebookId, { title, content })
    showToast('已添加到笔记', 'success')
  } catch (e) {
    console.error('Failed to save conversation to notebook:', e)
    showToast('添加到笔记失败', 'error')
  }
  saveToNoteConvId.value = null
  saveToNoteConvTitle.value = ''
}

function startNewNote() {
  notesStore.loadNotebooks()
  showNewNotePicker.value = true
}

async function handleNewNoteNotebookSelected(notebookId: string) {
  showNewNotePicker.value = false
  try {
    const note = await notesStore.createNote(notebookId, { content: '' })
    await navigateWithMobileHistory(router, `/notes/${notebookId}/${note.id}`)
    if (isMobileViewport()) emit('close-drawer')
  } catch (e) {
    console.error('Failed to create note:', e)
    showToast('创建笔记失败', 'error')
  }
}

async function handleSavePermissions(perms: Record<string, boolean>) {
  savingPermissions.value = true
  try {
    await auth.updatePermissions(perms)
    showToast('权限设置已保存', 'success')
    showSystemSettingsDialog.value = false
  } catch (e) {
    console.error('Failed to save permissions:', e)
    showToast('保存权限设置失败', 'error')
  } finally {
    savingPermissions.value = false
  }
}

function handleHotwordsSave() {
  showToast('热词配置已保存', 'success')
  showSystemSettingsDialog.value = false
}

function handleLogout() {
  emit('logout')
}

function handleSkillsUpdated() {
  showToast('技能已更新', 'success')
}

function handleSectionNavigation() {
  emit('close-drawer')
}

const emit = defineEmits<{
  logout: []
  'close-drawer': []
}>()
</script>

<style scoped>
.sidebar {
  width: var(--sidebar-width);
  height: var(--sidebar-height, 100%);
  background-color: var(--sidebar-card-bg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-right: 1px solid var(--panel-border);
  border-radius: var(--sidebar-card-radius);
  box-shadow: var(--sidebar-card-shadow);
  border: var(--sidebar-card-border);
  transition: transform var(--transition-normal);
}

.sidebar-header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--panel-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sidebar-nav {
  display: flex;
  gap: var(--nav-pill-gap, 4px);
  padding: var(--nav-pill-padding, 8px 12px);
  border-bottom: 1px solid var(--panel-border);
  background: var(--nav-pill-bg, transparent);
  border-radius: var(--nav-pill-radius, 0);
}

.nav-tab {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 12px;
  border-radius: var(--radius-pill);
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-light);
  text-decoration: none;
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.nav-tab:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.nav-tab.active {
  background-color: var(--color-primary);
  color: #fff;
}

.nav-tab:active {
  transform: scale(0.96);
}

.hide-on-mobile {
  display: flex;
}

.close-drawer-btn {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
}

.close-drawer-btn:hover {
  color: var(--color-text);
  background-color: var(--color-hover);
}

.close-drawer-btn:active {
  transform: scale(0.96);
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--color-primary);
}

.logo-icon {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  object-fit: contain;
}

.logo-text {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text);
}

/* ─── Sidebar Toolbar (assistant + tools) ──────────────────────── */

.sidebar-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px 8px;
}

.toolbar-assistant {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background-color var(--transition-fast);
  min-width: 0;
}

.toolbar-assistant:hover {
  background-color: var(--color-hover);
}

.toolbar-assistant svg:first-child {
  flex-shrink: 0;
  color: var(--color-text-light);
}

.toolbar-assistant-name {
  flex: 1;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.toolbar-arrow {
  flex-shrink: 0;
  color: var(--color-text-light);
  transition: transform var(--transition-fast);
}

.toolbar-arrow.open {
  transform: rotate(180deg);
}

.toolbar-search-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: var(--radius-sm);
  color: var(--color-text);
  background-color: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  transition: color var(--transition-fast), background-color var(--transition-fast), border-color var(--transition-fast), transform var(--transition-fast);
  cursor: pointer;
}

.toolbar-search-btn:hover {
  background-color: var(--color-hover);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.toolbar-search-btn:active {
  transform: scale(0.96);
}

/* ─── Tools Dropdown ─────────────────────────────────────────── */

.tools-dropdown {
  margin: 0 12px 8px;
  background-color: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  box-shadow: var(--panel-shadow);
  max-height: 320px;
  overflow-y: auto;
}

.tools-section {
  padding: 4px 0;
}

.tools-section-title {
  padding: 6px 12px;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-light);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.tools-item {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  color: var(--color-text);
  font-size: 13px;
  text-align: left;
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.tools-item:hover {
  background-color: var(--color-hover);
}

.tools-item.active {
  background-color: var(--color-hover);
  color: var(--color-primary);
}

.tools-item:active {
  transform: scale(0.99);
}

.tools-item-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tools-item-actions {
  display: flex;
  gap: 2px;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.tools-item:hover .tools-item-actions,
.tools-item.active .tools-item-actions {
  opacity: 1;
}

.tools-action-btn {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
}

.tools-action-btn:hover {
  color: var(--color-primary);
  background-color: var(--color-sidebar);
}

.tools-action-btn:active {
  transform: scale(0.96);
}

.tools-action-btn.delete:hover {
  color: var(--color-error);
}

.tools-divider {
  height: 1px;
  background-color: var(--color-border);
  margin: 2px 0;
}

.tools-item.create-item {
  color: var(--color-primary);
  gap: 8px;
  justify-content: flex-start;
}

.create-item:hover {
  background-color: var(--color-hover);
}

.selection-bar {
  padding: 8px 12px;
  margin: 0 0 8px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
}

.selection-bar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.select-all-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
}

.select-all-label input {
  cursor: pointer;
}

.selected-count {
  font-size: 12px;
  color: var(--color-text-light);
}

.selection-bar-actions {
  display: flex;
  gap: 8px;
}

.selection-confirm-btn {
  flex: 1;
  padding: 8px 12px;
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast), opacity var(--transition-fast);
}

.selection-confirm-btn:active:not(:disabled) {
  transform: scale(0.96);
}

.export-confirm-btn {
  background-color: var(--color-primary);
}

.export-confirm-btn:hover:not(:disabled) {
  background-color: var(--color-primary-dark);
}

.delete-confirm-btn {
  background-color: var(--color-error);
}

.delete-confirm-btn:hover:not(:disabled) {
  background-color: #a02c2c;
}

.selection-confirm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.export-cancel-btn,
.selection-cancel-btn {
  padding: 8px 12px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: border-color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.export-cancel-btn:hover {
  border-color: var(--color-text-light);
}

.export-cancel-btn:active,
.selection-cancel-btn:active {
  transform: scale(0.96);
}

.selection-progress {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-primary);
  text-align: center;
}

.group-checkbox {
  margin-right: 6px;
  cursor: pointer;
  flex-shrink: 0;
}

.delete-group-option {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-light);
  margin-bottom: 8px;
  padding: 4px 0;
}

.delete-group-option input {
  cursor: pointer;
}

.conversation-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}



.conversation-menu {
  position: fixed;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 1000;
  min-width: 140px;
  padding: 4px 0;
}

.menu-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  color: var(--color-text);
  font-size: 13px;
  text-align: left;
  transition: background-color var(--transition-fast);
}

.menu-item:hover {
  background-color: var(--color-hover);
}

.menu-item.delete {
  color: var(--color-error);
}

.menu-item.delete:hover {
  background-color: rgba(229, 62, 62, 0.08);
}

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-light);
  font-size: 13px;
}

.sidebar-footer {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 12px;
  border-top: 1px solid var(--color-border);
}

.user-info-wrapper {
  width: 100%;
  position: relative;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast);
}

.user-info:hover {
  background-color: var(--color-hover);
}

.user-avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  flex-shrink: 0;
  border-radius: 50%;
  background-color: var(--color-border);
  color: var(--color-text-light);
  font-size: 12px;
  font-weight: 500;
}

.user-name {
  font-size: 12px;
  color: var(--color-text-light);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100px;
}

.user-menu-overlay {
  position: fixed;
  inset: 0;
  z-index: 10001;
}

.user-menu {
  position: fixed;
  min-width: 160px;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  padding: 4px;
  z-index: 10000;
}

.user-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--color-text);
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast);
  cursor: pointer;
}

.user-menu-item:hover {
  background-color: var(--color-hover);
}

.user-menu-item.logout {
  color: var(--color-error);
}

.user-menu-item.logout:hover {
  background-color: rgba(229, 62, 62, 0.08);
}

.theme-toggle-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.theme-toggle-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.theme-toggle-btn:active {
  transform: scale(0.96);
}

.settings-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px 14px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.settings-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.settings-btn:active {
  transform: scale(0.96);
}

.admin-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px 14px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.admin-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.admin-btn:active {
  transform: scale(0.96);
}

@media (max-width: 767px) {
  .hide-on-mobile {
    display: none !important;
  }

  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    width: var(--drawer-width);
    max-width: 320px;
    z-index: 999;
    transform: translateX(-100%);
    border-right: none;
    border-radius: 0 var(--shell-workbench-radius) var(--shell-workbench-radius) 0;
    box-shadow: var(--frame-shadow);
    padding-bottom: env(safe-area-inset-bottom, 0px);
  }

  .sidebar.drawer-open {
    transform: translateX(0);
  }

  .sidebar-header {
    padding: 16px;
  }

  .sidebar-nav {
    padding: 8px;
    gap: 4px;
  }

  .nav-tab {
    flex: 1;
    padding: 8px 12px;
    font-size: 12px;
  }

  .sidebar-toolbar {
    padding: 0 8px 8px;
    gap: 8px;
  }

  .toolbar-search-btn {
    width: 40px;
    height: 40px;
  }

  .new-chat-btn {
    margin: 0 8px 8px;
    width: calc(100% - 16px);
  }

  .selection-bar {
    margin: 0 8px 8px;
  }

  .conversation-list {
    padding: 4px;
  }

  .tools-item-actions {
    opacity: 1;
    gap: 2px;
  }

  .tools-action-btn {
    min-width: 32px;
    min-height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 8px;
  }

  .tools-item {
    padding: 10px 12px;
  }
}

/* Search bar */
.search-bar {
  padding: 10px 12px;
  margin: 2px 0;
}

.search-input-wrapper {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background-color: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.search-input-wrapper:focus-within {
  border-color: var(--panel-border-strong);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary) 10%, transparent);
}

.search-icon {
  flex-shrink: 0;
  color: var(--color-text-light);
}

.search-input {
  flex: 1;
  border: none;
  outline: none;
  font-size: 13px;
  color: var(--color-text);
  background: transparent;
  min-width: 0;
}

.search-input::placeholder {
  color: var(--color-text-light);
}

.search-clear-btn {
  flex-shrink: 0;
  padding: 2px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast);
}

.search-clear-btn:hover {
  color: var(--color-text);
}

/* Search results */
.search-results {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.search-empty {
  padding: 24px 16px;
  text-align: center;
  color: var(--color-text-light);
  font-size: 13px;
}

.search-result-item {
  padding: 10px 16px;
  cursor: pointer;
  border-bottom: 1px solid var(--color-border);
  transition: background-color var(--transition-fast);
}

.search-result-item:hover {
  background-color: var(--color-hover);
}

.search-result-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.search-result-snippet {
  font-size: 12px;
  color: var(--color-text-light);
  line-height: 1.4;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.snippet-role {
  font-weight: 500;
  color: var(--color-text);
  margin-right: 4px;
}

.search-result-snippet :deep(mark) {
  background-color: var(--color-primary);
  color: white;
  padding: 0 2px;
  border-radius: 2px;
}

/* ---------- Desktop inline notes panel + sidebar body ---------- */

/* Sidebar swap transition (conversations ↔ notes) */
.sidebar-swap-enter-active,
.sidebar-swap-leave-active {
  transition: opacity 0.2s cubic-bezier(0.22, 1, 0.36, 1),
              transform 0.2s cubic-bezier(0.22, 1, 0.36, 1);
}

.sidebar-swap-enter-from {
  opacity: 0;
  transform: translateX(-12px);
}

.sidebar-swap-leave-to {
  opacity: 0;
  transform: translateX(12px);
}

.sidebar-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.notes-panel-inline {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background-color: var(--surface-panel-strong);
}

.notes-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px 6px;
  position: sticky;
  top: 0;
  background: var(--color-white);
  z-index: 1;
  border-bottom: 1px solid var(--color-border);
}

.notes-panel-title {
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--color-text-light);
}

.notes-panel-open-btn {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
}
.notes-panel-open-btn:hover { color: var(--color-primary); background-color: var(--color-hover); }
.notes-panel-open-btn:active { transform: scale(0.96); }
.new-note-btn {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
}
.new-note-btn:hover { color: var(--color-primary); background-color: var(--color-hover); }
.new-note-btn:active { transform: scale(0.96); }

.notes-panel-loading {
  padding: 12px 14px;
  font-size: 12px;
  color: var(--color-text-light);
}

.notes-panel-list {
  padding: 4px 0;
}

.np-notebook { }

/* Desktop-only "首页" row — same visual hierarchy as a notebook row. */
.np-home {
  outline: none;
}
/* Ensure the home row stretches to the full sidebar width so the active
  background reads like the rest of the notebook tree. */
.np-notebook.np-home {
  display: block;
}
.np-home {
  margin: 0 8px 8px;
}
.np-home:focus-visible .np-notebook-row {
  background-color: var(--color-hover);
}
.np-home.active .np-notebook-row {
  background-color: color-mix(in srgb, var(--color-primary) 14%, var(--surface-panel-subtle));
  box-shadow: inset 0 0 0 1px var(--panel-border-strong);
  font-weight: 600;
}
.np-chevron-placeholder {
  visibility: hidden;
}

.np-notebook-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  margin: 0 8px;
  border-radius: var(--shell-workbench-radius);
  cursor: pointer;
  user-select: none;
}
.np-notebook-row:hover { background-color: var(--color-hover); }
.np-notebook-row.active {
  background-color: color-mix(in srgb, var(--color-primary) 14%, var(--surface-panel-subtle));
  box-shadow: inset 0 0 0 1px var(--panel-border-strong);
}
.np-notebook-row.active .np-nb-name {
  color: var(--color-primary);
}
.np-notebook-row.active .np-count {
  background: color-mix(in srgb, var(--color-primary) 16%, var(--surface-panel-strong));
  color: var(--color-primary);
}

.np-chevron {
  transition: transform var(--transition-fast);
  color: var(--color-text-light);
  flex-shrink: 0;
}
.np-chevron.expanded { transform: rotate(90deg); }

.np-nb-name {
  flex: 1;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.np-count {
  font-size: 11px;
  color: var(--color-text-light);
  background: var(--color-bg);
  padding: 1px 6px;
  border-radius: 10px;
  font-variant-numeric: tabular-nums;
  min-width: 18px;
  text-align: center;
}

.np-notes-list { padding-left: 18px; }

.np-note-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  cursor: pointer;
  border-radius: var(--shell-workbench-radius);
  margin: 5px 8px;
}
.np-note-row:hover { background-color: var(--color-hover); }
.np-note-row svg { flex-shrink: 0; color: var(--color-text-light); }
.np-note-row.active {
  background-color: color-mix(in srgb, var(--color-primary) 18%, var(--surface-panel-strong));
  box-shadow: inset 0 0 0 1px var(--panel-border-strong);
}
.np-note-row.active svg,
.np-note-row.active .np-note-title {
  color: var(--color-primary);
}
.np-note-row.active .np-note-title {
  font-weight: 600;
}

.np-note-title {
  font-size: 13px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.np-note-loading, .np-empty {
  padding: 6px 12px;
  font-size: 12px;
  color: var(--color-text-light);
}

.np-menu-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 3px 5px;
  opacity: 0;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
  flex-shrink: 0;
}
.np-notebook-row:hover .np-menu-btn,
.np-note-row:hover .np-menu-btn,
.np-menu-btn.active {
  opacity: 1;
}
.np-menu-btn:hover {
  color: var(--color-text);
  background-color: var(--color-hover);
}
@media (hover: none) {
  .np-menu-btn { opacity: 1; }
}

.np-context-menu {
  position: fixed;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 1000;
  min-width: 140px;
  padding: 4px 0;
}

.np-move-options {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 240px;
  overflow-y: auto;
}
.np-move-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: 13px;
  text-align: left;
  transition: background-color var(--transition-fast);
}
.np-move-option:hover {
  background-color: var(--color-hover);
}
.np-move-option.active {
  background-color: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
}

/* ─── Action Buttons ─────────────────────────────────────────── */

.action-buttons {
  display: flex;
  gap: 8px;
  padding: 12px 12px 12px;
}

.new-chat-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex: 1;
  padding: 12px 16px;
  background-color: var(--color-primary);
  color: white;
  border-radius: var(--radius-md);
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast), box-shadow var(--transition-fast);
}

.new-chat-btn:hover {
  background-color: var(--color-primary-dark);
  transform: translateY(-1px);
}

.new-chat-btn:active {
  transform: translateY(0) scale(0.96);
}

.new-group-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  padding: 0;
  background-color: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  transition: color var(--transition-fast), background-color var(--transition-fast), border-color var(--transition-fast), transform var(--transition-fast);
  flex-shrink: 0;
}

.new-group-btn:hover {
  background-color: var(--color-hover);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.new-group-btn:active {
  transform: scale(0.96);
}

/* ─── Conversation Groups ─────────────────────────────────────── */

.conversation-group {
  margin-bottom: 4px;
}

.time-category-section {
  margin-bottom: 8px;
}

.time-category-header {
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-light);
  text-transform: uppercase;
  letter-spacing: 0.3px;
  user-select: none;
}

.groups-directory {
  margin-top: 8px;
}

.groups-directory-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  user-select: none;
  transition: background-color var(--transition-fast);
}

.groups-directory-header:hover {
  background-color: var(--color-hover);
}

.directory-chevron {
  transition: transform var(--transition-fast);
  color: var(--color-text-light);
  flex-shrink: 0;
}

.directory-chevron.expanded {
  transform: rotate(90deg);
}

.directory-name {
  flex: 1;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.directory-count {
  font-size: 11px;
  color: var(--color-text-light);
  background: var(--color-bg);
  padding: 1px 6px;
  border-radius: 10px;
  font-variant-numeric: tabular-nums;
  min-width: 18px;
  text-align: center;
}

.groups-directory-content {
  padding-left: 4px;
}

.group-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  user-select: none;
  transition: background-color var(--transition-fast);
}

.group-header:hover {
  background-color: var(--color-hover);
}

.group-header.group-selected {
  background-color: rgba(239, 68, 68, 0.15);
}

.group-header.group-focus {
  background-color: color-mix(in srgb, var(--color-primary) 12%, transparent);
  /* 焦点带高度减 2px×2，避免与组内首个激活会话的焦点色块粘连 */
  padding-top: 6px;
  padding-bottom: 6px;
}

.group-header.group-focus + .group-conversations:not(.group-convs-collapsed) {
  margin-top: 2px;
}

.group-header.group-focus .group-name {
  color: var(--color-primary-dark);
}

.group-header.group-focus .group-count {
  background: color-mix(in srgb, var(--color-primary) 16%, var(--surface-panel-strong));
  color: var(--color-primary-dark);
}

.group-chevron {
  transition: transform var(--transition-fast);
  color: var(--color-text-light);
  flex-shrink: 0;
}

.group-chevron.expanded {
  transform: rotate(90deg);
}

.group-color-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.group-name {
  flex: 1;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.group-count {
  font-size: 11px;
  color: var(--color-text-light);
  background: var(--color-bg);
  padding: 1px 6px;
  border-radius: 10px;
  font-variant-numeric: tabular-nums;
  min-width: 18px;
  text-align: center;
}

.group-actions {
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.group-header:hover .group-actions {
  opacity: 1;
}

.group-action-btn {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
}

.group-action-btn:hover {
  color: var(--color-primary);
  background-color: var(--color-sidebar);
}

.group-action-btn:active {
  transform: scale(0.96);
}

.group-action-btn.delete:hover {
  color: var(--color-error);
}

.group-conversations {
  padding-left: 12px;
  display: grid;
  grid-template-rows: 1fr;
  opacity: 1;
  transition: grid-template-rows 0.25s cubic-bezier(0.25, 1, 0.5, 1),
              opacity 0.2s ease;
}

/* Instant collapse during drag to prevent SortableJS position interference */
.sidebar-dragging .group-conversations {
  transition: none !important;
}

/* Collapsed groups during drag: show as droppable targets with min-height */
.sidebar-dragging .group-convs-collapsed {
  grid-template-rows: 1fr !important;
  opacity: 1 !important;
  overflow: hidden !important;
  padding: 0 !important;
  min-height: 40px !important;
  border: 2px dashed var(--color-primary) !important;
  border-radius: var(--radius-sm) !important;
  margin: 4px 0 !important;
  background-color: rgba(59, 130, 246, 0.05) !important;
}

.sidebar-dragging .group-conv-drag-list {
  transition: none !important;
  min-height: 24px !important;
}

.group-convs-collapsed {
  grid-template-rows: 0fr;
  opacity: 0;
  overflow: hidden;
  padding: 0;
  margin: 0;
  border: none;
}

.group-conversations > * {
  min-height: 0;
  overflow: hidden;
}

.group-conv-drag-list {
  min-height: 4px;
  transition: min-height 0.2s ease, background-color 0.2s ease, border-color 0.2s ease;
}

.sidebar-dragging .conversation-group.drag-hover .group-conv-drag-list:empty {
  min-height: 48px;
  border: 2px dashed var(--color-primary);
  border-radius: var(--radius-sm);
  margin: 4px 0;
  background-color: rgba(59, 130, 246, 0.05);
  transition: min-height 0.2s ease, background-color 0.2s ease;
}

.ungrouped-drop-zone {
  margin-top: 8px;
  padding: 4px 0;
}

.ungrouped-drop-zone-label {
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-light);
  text-transform: uppercase;
  letter-spacing: 0.3px;
  user-select: none;
}

.ungrouped-empty-drag-list {
  min-height: 40px;
  border: 2px dashed var(--panel-border);
  border-radius: var(--radius-md);
  margin: 0 4px;
  transition: border-color var(--transition-fast), background-color var(--transition-fast);
}

.ungrouped-empty-drag-list:empty {
  display: block;
}

/* ─── Menu Divider ────────────────────────────────────────────── */

.menu-divider {
  height: 1px;
  background-color: var(--color-border);
  margin: 4px 0;
}

/* ─── Move Group Menu ─────────────────────────────────────────── */

.move-group-overlay {
  position: fixed;
  inset: 0;
  z-index: 999;
}

.move-group-menu {
  min-width: 160px;
}

.move-group-menu .menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.move-group-menu .menu-item.active {
  background-color: var(--color-hover);
  color: var(--color-primary);
}

.menu-section-label {
  padding: 6px 12px 2px;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  opacity: 0.75;
  letter-spacing: 0.02em;
}

/* ─── Modal / Dialog ──────────────────────────────────────────── */

.modal-overlay {
  position: fixed;
  inset: 0;
  background-color: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000;
  padding: 16px;
}

.modal-content {
  background-color: var(--color-white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  min-width: 320px;
  max-width: 400px;
  width: 100%;
  overflow: hidden;
}

.modal-title {
  padding: 16px 20px 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.modal-body {
  padding: 16px 20px;
}

.modal-actions {
  display: flex;
  gap: 8px;
  padding: 0 20px 16px;
  justify-content: flex-end;
}

.modal-btn {
  padding: 8px 16px;
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast), opacity var(--transition-fast);
}

.modal-btn:active {
  transform: scale(0.96);
}

.modal-btn.cancel {
  background-color: var(--color-bg);
  color: var(--color-text);
}

.modal-btn.cancel:hover {
  background-color: var(--color-hover);
}

.modal-btn.confirm {
  background-color: var(--color-primary);
  color: white;
}

.modal-btn.confirm:hover {
  background-color: var(--color-primary-dark);
}

.modal-btn.delete {
  background-color: var(--color-error);
  color: white;
}

.modal-btn.delete:hover {
  background-color: #a02c2c;
}

.form-group {
  margin-bottom: 16px;
}

.form-group label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  margin-bottom: 6px;
}

.form-group input,
.modal-body input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-size: 14px;
  color: var(--color-text);
  background: var(--color-white);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.form-group input:focus,
.modal-body input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: var(--input-container-shadow-focus, 0 0 0 3px rgba(122, 163, 90, 0.15));
}

.form-group select.form-select {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-size: 14px;
  color: var(--color-text);
  background: var(--color-white);
  transition: border-color var(--transition-fast);
  appearance: auto;
}

.form-group select.form-select:focus {
  outline: none;
  border-color: var(--color-primary);
}

.color-picker {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.color-option {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: 2px solid transparent;
  cursor: pointer;
  transition: transform var(--transition-fast), border-color var(--transition-fast);
}

.color-option:hover {
  transform: scale(1.1);
}

.color-option.active {
  border-color: var(--color-text);
  transform: scale(1.15);
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
}

.checkbox-label input {
  cursor: pointer;
}

/* ─── Draggable ───────────────────────────────────────────────── */

.sidebar-dragging .ungrouped-empty-drag-list {
  min-height: 48px;
  border: 2px dashed var(--color-primary);
  background-color: rgba(59, 130, 246, 0.06);
  transition: min-height 0.2s ease, background-color 0.2s ease, border-color 0.2s ease;
}

.sidebar-dragging .time-category-section .ungrouped-conv-drag-list:empty {
  min-height: 48px;
  border: 2px dashed var(--color-primary);
  border-radius: var(--radius-sm);
  margin: 4px 0;
  background-color: rgba(59, 130, 246, 0.04);
  transition: min-height 0.2s ease, background-color 0.2s ease;
}

.sidebar-dragging .conversation-group.drag-hover {
  background-color: rgba(59, 130, 246, 0.1) !important;
  border-radius: var(--radius-sm);
  outline: 2px solid var(--color-primary);
  outline-offset: -2px;
  transition: background-color 0.2s ease, outline-color 0.2s ease;
}

.sidebar-dragging [data-draggable="true"] {
  cursor: grab;
}

.sidebar-dragging [data-draggable="true"]:active {
  cursor: grabbing;
}

.sortable-ghost {
  opacity: 0.35;
  background-color: var(--color-hover);
  border-radius: var(--radius-sm);
  transition: opacity 0.15s ease;
}

.sortable-chosen {
  z-index: 10;
}

.sortable-drag {
  opacity: 0.9;
  box-shadow: 0 4px 16px rgba(90, 130, 60, 0.12);
  border-radius: var(--radius-sm);
}

.sortable-fallback {
  opacity: 0.8;
  box-shadow: 0 4px 16px rgba(90, 130, 60, 0.12);
}

.group-drop-target {
  outline: 2px dashed var(--color-primary);
  outline-offset: -2px;
  border-radius: var(--radius-md);
  background-color: rgba(59, 130, 246, 0.08);
}

/* ─── Mobile Adaptations ──────────────────────────────────────── */

@media (max-width: 767px) {
  .action-buttons {
    padding: 12px 8px 8px;
  }

  .new-group-btn {
    width: 40px;
    height: 40px;
    padding: 0;
  }

  .group-actions {
    opacity: 1;
  }

  .groups-directory-header {
    padding: 10px 12px;
  }

  .time-category-header {
    padding: 8px 12px;
    font-size: 11px;
  }

  .group-action-btn {
    min-width: 32px;
    min-height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .modal-content {
    margin: 16px;
    min-width: auto;
  }
}

/* ─── Marquee Selection Overlay ───────────────────────────────── */

.marquee-overlay {
  position: fixed;
  border: 1px solid var(--color-primary);
  background-color: rgba(59, 130, 246, 0.1);
  z-index: 1000;
  pointer-events: none;
}

/* ─── Spotlight Search Modal ─────────────────────────────────── */

.spotlight-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(4px);
  z-index: 10000;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 20vh;
}

.spotlight-modal {
  width: 560px;
  max-width: 90vw;
  background: var(--surface-panel, #fff);
  border-radius: 12px;
  box-shadow: 0 25px 60px rgba(0, 0, 0, 0.3);
  overflow: hidden;
  animation: spotlightIn 0.15s ease-out;
}

@keyframes spotlightIn {
  from { opacity: 0; transform: translateY(-10px) scale(0.97); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.spotlight-input-wrapper {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--panel-border, #e5e7eb);
}

.spotlight-search-icon {
  flex-shrink: 0;
  color: var(--color-text-light);
}

.spotlight-input {
  flex: 1;
  border: none;
  outline: none;
  font-size: 16px;
  color: var(--color-text);
  background: transparent;
}

.spotlight-input::placeholder {
  color: var(--color-text-light);
}

.spotlight-results {
  max-height: 400px;
  overflow-y: auto;
}

.spotlight-status {
  padding: 24px 20px;
  text-align: center;
  color: var(--color-text-light);
  font-size: 14px;
}

.spotlight-result-item {
  padding: 12px 20px;
  cursor: pointer;
  border-bottom: 1px solid var(--panel-border, #e5e7eb);
  transition: background var(--transition-fast);
}

.spotlight-result-item:hover {
  background: var(--color-hover);
}

.spotlight-result-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 4px;
}

.spotlight-result-snippet {
  font-size: 12px;
  color: var(--color-text-light);
  margin-top: 3px;
}

.spotlight-result-snippet--clickable {
  cursor: pointer;
  padding: 3px 4px;
  border-radius: 4px;
  transition: background-color 0.12s ease;
}

.spotlight-result-snippet--clickable:hover {
  background: var(--color-hover);
}

.spotlight-snippet-role {
  font-weight: 500;
  color: var(--color-text);
  margin-right: 4px;
}

.spotlight-snippet-text :deep(mark) {
  background: var(--color-primary);
  color: #fff;
  padding: 0 2px;
  border-radius: 2px;
}

/* ─── Batch Operation Popup ──────────────────────────────────── */

.batch-popup-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 10001;
  display: flex;
  align-items: center;
  justify-content: center;
}

.batch-popup {
  width: 480px;
  max-width: 90vw;
  max-height: 80vh;
  background: var(--surface-panel, #fff);
  border-radius: 12px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.3);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: spotlightIn 0.15s ease-out;
}

.batch-popup-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--panel-border, #e5e7eb);
}

.batch-popup-header h2 {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0;
}

.batch-popup-close {
  color: var(--color-text-light);
  padding: 8px;
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
}

.batch-popup-close:hover {
  color: var(--color-text);
  background: var(--color-hover);
}

.batch-popup-close:active {
  transform: scale(0.96);
}

.batch-popup-body {
  flex: 1;
  overflow-y: auto;
}

.batch-popup-toolbar {
  padding: 10px 20px;
  border-bottom: 1px solid var(--panel-border, #e5e7eb);
}

.batch-select-all {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
}

.batch-popup-list {
  padding: 4px 0;
}

.batch-popup-empty {
  padding: 32px 20px;
  text-align: center;
  color: var(--color-text-light);
  font-size: 14px;
}

.batch-popup-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 20px;
  cursor: pointer;
  transition: background var(--transition-fast);
  border-bottom: 1px solid var(--panel-border, #e5e7eb);
}

.batch-popup-item:hover {
  background: var(--color-hover);
}

.batch-popup-item.selected {
  background: rgba(122, 163, 90, 0.06);
}

.batch-popup-item-info {
  flex: 1;
  min-width: 0;
}

.batch-popup-item-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.batch-popup-item-meta {
  font-size: 12px;
  color: var(--color-text-light);
  margin-top: 2px;
}

.batch-popup-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--panel-border, #e5e7eb);
}

.batch-popup-status {
  flex: 1;
  font-size: 13px;
  color: var(--color-primary);
}

.batch-popup-cancel {
  padding: 8px 16px;
  font-size: 13px;
  color: var(--color-text);
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.batch-popup-cancel:hover {
  background: var(--color-hover);
}

.batch-popup-cancel:active {
  transform: scale(0.96);
}

.batch-popup-confirm {
  padding: 8px 20px;
  font-size: 13px;
  font-weight: 500;
  color: #fff;
  background: var(--color-primary);
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast), opacity var(--transition-fast), transform var(--transition-fast);
}

.batch-popup-confirm:hover:not(:disabled) {
  opacity: 0.9;
}

.batch-popup-confirm:active:not(:disabled) {
  transform: scale(0.96);
}

.batch-popup-confirm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.batch-popup-confirm.batch-popup-delete {
  background: var(--color-error);
}

/* ─── Tools Item Danger ──────────────────────────────────────── */

.tools-item.danger {
  color: var(--color-error);
}

.tools-item.danger:hover {
  background: rgba(220, 60, 60, 0.08);
}

.tools-item:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>

<style>
.sortable-fallback {
  opacity: 0.9 !important;
  background-color: var(--color-white, #ffffff) !important;
  box-shadow: var(--shadow-lg, 0 -3px 12px rgba(90, 130, 60, 0.08), 0 18px 36px rgba(90, 130, 60, 0.14)) !important;
  border-radius: var(--radius-md, 14px) !important;
  pointer-events: none !important;
  z-index: 10001 !important;
  transform: none !important;
}
</style>
