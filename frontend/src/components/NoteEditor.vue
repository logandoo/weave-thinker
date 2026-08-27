<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="note-editor-page">
    <div class="editor-header">
      <button class="back-btn" @click="goBack">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="15 18 9 12 15 6"/>
        </svg>
      </button>
      <input
        v-model="noteTitle"
        class="title-input"
        placeholder="标题（可选）"
        @input="hasChanges = true"
      />
      <div class="header-actions">
        <button v-if="hasChanges" class="save-btn" @click="saveNote" :disabled="saving">
          {{ saving ? '保存中...' : '保存' }}
        </button>
        <button class="export-btn" @click="showExportPicker = true" title="导出">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
        </button>
        <button class="delete-btn" @click="confirmDelete">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
          </svg>
        </button>
      </div>
    </div>

    <div class="editor-toolbar">
      <button class="toolbar-btn" :class="{ active: isBold }" @mousedown.prevent @click="execCommand('bold')" title="粗体">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M6 4h8a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z"/>
          <path d="M6 12h9a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z"/>
        </svg>
      </button>
      <button class="toolbar-btn" :class="{ active: isItalic }" @mousedown.prevent @click="execCommand('italic')" title="斜体">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="19" y1="4" x2="10" y2="4"/>
          <line x1="14" y1="20" x2="5" y2="20"/>
          <line x1="15" y1="4" x2="9" y2="20"/>
        </svg>
      </button>
      <div class="heading-dropdown-wrap">
        <button
          class="toolbar-btn"
          :class="{ active: currentHeadingLevel }"
          ref="headingBtnRef"
          @mousedown.prevent
          @click="toggleHeadingMenu"
          :title="currentHeadingLevel ? currentHeadingLevel.toUpperCase() : '标题'"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M4 12h8"/>
            <path d="M4 18V6"/>
            <path d="M12 18V6"/>
          </svg>
        </button>
      </div>
      <button class="toolbar-btn" :class="{ active: isQuote }" @mousedown.prevent @click="handleBlockquoteCommand" title="引用">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="3" y1="10" x2="3" y2="14"/>
          <line x1="7" y1="6" x2="21" y2="6"/>
          <line x1="7" y1="12" x2="21" y2="12"/>
          <line x1="7" y1="18" x2="21" y2="18"/>
        </svg>
      </button>
      <button class="toolbar-btn" @mousedown.prevent @click="handleListCommand('insertUnorderedList')" title="项目符号列表">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="9" y1="6" x2="21" y2="6"/>
          <line x1="9" y1="12" x2="21" y2="12"/>
          <line x1="9" y1="18" x2="21" y2="18"/>
          <circle cx="4.5" cy="6" r="1.5" fill="currentColor" stroke="none"/>
          <circle cx="4.5" cy="12" r="1.5" fill="currentColor" stroke="none"/>
          <circle cx="4.5" cy="18" r="1.5" fill="currentColor" stroke="none"/>
        </svg>
      </button>
      <button class="toolbar-btn" @mousedown.prevent @click="handleListCommand('insertOrderedList')" title="编号列表">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="10" y1="6" x2="21" y2="6"/>
          <line x1="10" y1="12" x2="21" y2="12"/>
          <line x1="10" y1="18" x2="21" y2="18"/>
          <path d="M4 7V5"/>
          <path d="M4 11h2v5"/>
          <path d="M4 19h3"/>
        </svg>
      </button>
      <button class="toolbar-btn" @mousedown.prevent="wysiwygEditorRef.value?.saveEditorSelection()" @click="handleRestartNumbering()" title="重新编号">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M1 4v6h6"/>
          <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
          <text x="12" y="16" font-size="8" fill="currentColor" stroke="none" text-anchor="middle" font-weight="bold">1</text>
        </svg>
      </button>
      <button class="toolbar-btn" @mousedown.prevent @click="onObjectAlign('left')" title="左对齐">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="3" y1="6" x2="21" y2="6"/><rect x="3" y="9" width="10" height="6" rx="1"/><line x1="3" y1="18" x2="21" y2="18"/>
        </svg>
      </button>
      <button class="toolbar-btn" @mousedown.prevent @click="onObjectAlign('center')" title="居中">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="3" y1="6" x2="21" y2="6"/><rect x="7" y="9" width="10" height="6" rx="1"/><line x1="3" y1="18" x2="21" y2="18"/>
        </svg>
      </button>
      <button class="toolbar-btn" @mousedown.prevent @click="onObjectAlign('right')" title="右对齐">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="3" y1="6" x2="21" y2="6"/><rect x="11" y="9" width="10" height="6" rx="1"/><line x1="3" y1="18" x2="21" y2="18"/>
        </svg>
      </button>
      <div class="table-picker-wrap">
        <button class="toolbar-btn" ref="tableBtnRef" @mousedown.prevent @click="toggleTablePicker" title="插入表格">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="3" width="18" height="18" rx="2"/>
            <line x1="3" y1="9" x2="21" y2="9"/>
            <line x1="3" y1="15" x2="21" y2="15"/>
            <line x1="9" y1="3" x2="9" y2="21"/>
            <line x1="15" y1="3" x2="15" y2="21"/>
          </svg>
        </button>
      </div>
      <div class="toolbar-divider"></div>
      <div class="color-picker-wrap">
        <button class="toolbar-btn highlight-btn" ref="highlightBtnRef" @mousedown.prevent @click="toggleHighlightColorPicker" title="高亮">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 20h9"/>
            <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
          </svg>
        </button>
      </div>
      <div class="color-picker-wrap">
        <button class="toolbar-btn font-color-btn" ref="fontColorBtnRef" @mousedown.prevent @click="toggleFontColorPicker" title="字体">
          <span class="font-color-text">A</span>
        </button>
      </div>
      <button class="toolbar-btn" :class="{ active: isSup }" @mousedown.prevent @click="applySuperscript" title="上标">
        <span class="script-btn-text">A<sup>2</sup></span>
      </button>
      <button class="toolbar-btn" :class="{ active: isSub }" @mousedown.prevent @click="applySubscript" title="下标">
        <span class="script-btn-text">A<sub>2</sub></span>
      </button>
      <div class="toolbar-divider"></div>
      <button class="toolbar-btn" @click="openFindBar(false)" title="查找">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"/>
          <line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
      </button>
      <button class="toolbar-btn" @click="openFindBar(true)" title="查找替换">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M17 1l4 4-4 4"/>
          <path d="M3 11V9a4 4 0 0 1 4-4h14"/>
          <path d="M7 23l-4-4 4-4"/>
          <path d="M21 13v2a4 4 0 0 1-4 4H3"/>
        </svg>
      </button>
      <div class="toolbar-divider"></div>
      <button class="toolbar-btn" :class="{ active: showToc }" @click="showToc = !showToc" title="目录">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="8" y1="13" x2="16" y2="13"/>
          <line x1="8" y1="17" x2="13" y2="17"/>
        </svg>
      </button>
      <button class="toolbar-btn" @click="startVoiceNote" :disabled="isProcessing" title="语音输入">
        <svg v-if="!isRecording" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
          <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
          <line x1="12" y1="19" x2="12" y2="23"/>
          <line x1="8" y1="23" x2="16" y2="23"/>
        </svg>
        <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="recording-icon">
          <rect x="6" y="6" width="12" height="12" rx="2"/>
        </svg>
      </button>
      <div class="toolbar-divider"></div>
      <button class="toolbar-btn" @mousedown.prevent @click="triggerImageUpload" title="插入图片">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
          <circle cx="8.5" cy="8.5" r="1.5"/>
          <polyline points="21 15 16 10 5 21"/>
        </svg>
      </button>
      <button class="toolbar-btn" @mousedown.prevent @click="triggerAudioUpload" title="插入音频">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M9 18V5l12-2v13"/>
          <circle cx="6" cy="18" r="3"/>
          <circle cx="18" cy="16" r="3"/>
        </svg>
      </button>
      <button class="toolbar-btn" @mousedown.prevent @click="triggerVideoUpload" title="插入视频">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22.54 6.42a2.78 2.78 0 0 0-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 0 0-1.94 2A29 29 0 0 0 1 11.75a29 29 0 0 0 .46 5.33A2.78 2.78 0 0 0 3.4 19c1.72.46 8.6.46 8.6.46s6.88 0 8.6-.46a2.78 2.78 0 0 0 1.94-2 29 29 0 0 0 .46-5.25 29 29 0 0 0-.46-5.33z"/>
          <polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
        </svg>
      </button>
    </div>

    <div v-if="showFindBar" class="find-replace-bar">
      <div class="find-row">
        <input
          ref="findInputRef"
          v-model="findQuery"
          class="find-input"
          placeholder="查找..."
          @input="onFindInput"
          @keydown="onFindKeydown"
        />
        <span v-if="findQuery" class="find-count">{{ matchPositions.length === 0 ? '无结果' : `${currentMatchIndex + 1}/${matchPositions.length}` }}</span>
        <button class="find-nav-btn" :disabled="matchPositions.length === 0" @click="findPrev" title="上一个">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
        </button>
        <button class="find-nav-btn" :disabled="matchPositions.length === 0" @click="findNext" title="下一个">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <button class="find-close-btn" @click="closeFindBar" title="关闭">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div v-if="showReplaceBar" class="find-row">
        <input
          v-model="replaceQuery"
          class="find-input"
          placeholder="替换为..."
          @keydown="onReplaceKeydown"
        />
        <button class="find-action-btn" :disabled="matchPositions.length === 0" @click="replaceCurrent" title="替换">替换</button>
        <button class="find-action-btn" :disabled="matchPositions.length === 0" @click="replaceAll" title="全部替换">全部</button>
      </div>
    </div>

      <div class="editor-content">
      <div v-if="showToc" class="toc-panel">
        <div class="toc-header">
          <span>目录</span>
          <button class="toc-close-btn" @click="showToc = false">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <div class="toc-list" v-if="tocItems.length > 0">
          <div
            v-for="item in visibleTocItems"
            :key="item.id"
            class="toc-item"
            :style="{ paddingLeft: (item.level * 16) + 'px' }"
            @click="scrollToHeading(item.id)"
          >
            <span v-if="item.hasChildren" class="toc-toggle" @click.stop="toggleTocCollapse(item.index)">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" :class="{ 'toc-toggle-collapsed': collapsedToc.has(item.index) }">
                <polyline points="6 9 12 15 18 9"/>
              </svg>
            </span>
            <span v-else class="toc-toggle-spacer"></span>
            {{ item.text }}
          </div>
        </div>
        <div v-else class="toc-empty">暂无标题</div>
      </div>
      <input type="file" ref="imageUploadInputRef" accept="image/*" style="display:none" @change="onImageFileSelected" />
      <input type="file" ref="mediaUploadInputRef" accept="audio/*,video/*,.mp3,.wav,.m4a,.ogg,.flac,.aac,.mp4,.webm,.mov,.m4v" style="display:none" @change="onMediaFileSelected" />
      <WysiwygEditor
        ref="wysiwygEditorRef"
        v-model="noteContent"
        class="wysiwyg-editor-wrapper"
        :find-query="findQuery"
        :find-active="showFindBar"
        :find-current-index="currentMatchIndex"
        :image-resolver="resolveImageUrl"
        :image-uploader="handleImageUpload"
        :media-resolver="resolveImageUrl"
        @change="onContentChange"
        @find-request="openFindBar"
        @image-upload-failed="showToast('图片上传失败，保存后将丢失，请重试', 'error')"
      />
      <div class="voice-hint" v-if="isRecording">
        <span class="recording-indicator"></span>
        录音中，点击停止...
      </div>
    </div>

    <div class="raw-transcription" v-if="note?.raw_transcription && note.raw_transcription !== noteContent">
      <div class="transcription-label">原始语音识别:</div>
      <div class="transcription-text">{{ note.raw_transcription }}</div>
    </div>

    <Teleport to="body">
      <div v-if="showExportPicker" class="export-format-overlay" @click="showExportPicker = false">
        <div class="export-format-dialog" @click.stop>
          <h3>选择导出格式</h3>
          <div class="export-format-options">
            <button class="export-format-btn" @click="handleExport('md')">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <span>Markdown (.md)</span>
            </button>
            <button class="export-format-btn" @click="handleExport('pdf')">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
                <line x1="16" y1="13" x2="8" y2="13"/>
                <line x1="16" y1="17" x2="8" y2="17"/>
              </svg>
              <span>PDF (.pdf)</span>
            </button>
          </div>
          <button class="export-cancel-btn" @click="showExportPicker = false">取消</button>
        </div>
      </div>
    </Teleport>
    <Teleport to="body">
      <ExportProgressDialog
        :visible="exporting || exportStatus === 'success'"
        :format="exportFormat"
        :status="exportStatus"
        :progress="exportProgress"
        @cancel="cancelExport"
        @close="closeExportDialog"
      />
    </Teleport>
    <Teleport to="body">
      <div v-if="showFontColorPicker" class="font-color-picker-teleport" :style="fontColorPickerStyle" @click.stop>
        <div class="font-size-row">
          <label class="font-size-label">字号</label>
          <select v-model="selectedFontSize" class="font-size-select" @change="applyFontSize">
            <option value="" disabled hidden>字号</option>
            <option value="12px">12px</option>
            <option value="14px">14px</option>
            <option value="16px">16px</option>
            <option value="18px">18px</option>
            <option value="20px">20px</option>
            <option value="24px">24px</option>
            <option value="28px">28px</option>
            <option value="32px">32px</option>
            <option value="36px">36px</option>
          </select>
        </div>
        <div class="color-grid">
          <button
            v-for="c in fontColors"
            :key="c"
            class="color-swatch"
            :style="{ backgroundColor: c }"
            @click="applyFontColorToSelection(c)"
            :title="c"
          ></button>
        </div>
        <div class="color-input-row">
          <input
            type="color"
            v-model="customFontColor"
            class="color-native-input"
          />
          <button class="color-apply-btn" @click="applyFontColorToSelection(customFontColor)">应用</button>
        </div>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="showHighlightColorPicker" class="font-color-picker-teleport" :style="highlightColorPickerStyle" @click.stop>
        <div class="color-grid">
          <button
            v-for="c in highlightColors"
            :key="c"
            class="color-swatch"
            :style="{ backgroundColor: c }"
            @click="applyHighlightColor(c)"
            :title="c"
          ></button>
        </div>
        <div class="color-input-row">
          <input
            type="color"
            v-model="customHighlightColor"
            class="color-native-input"
          />
          <button class="color-apply-btn" @click="applyHighlightColor(customHighlightColor)">应用</button>
          <button class="color-remove-btn" @click="removeHighlight">取消高亮</button>
        </div>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="showHeadingMenu" class="heading-dropdown-teleport" :style="headingMenuStyle" @click.stop>
        <button class="heading-dropdown-item" :class="{ 'heading-active': currentHeadingLevel === 'h1' }" @click="applyHeading('h1')"><span class="heading-icon">H1</span></button>
        <button class="heading-dropdown-item" :class="{ 'heading-active': currentHeadingLevel === 'h2' }" @click="applyHeading('h2')"><span class="heading-icon">H2</span></button>
        <button class="heading-dropdown-item" :class="{ 'heading-active': currentHeadingLevel === 'h3' }" @click="applyHeading('h3')"><span class="heading-icon">H3</span></button>
        <button class="heading-dropdown-item" :class="{ 'heading-active': currentHeadingLevel === 'h4' }" @click="applyHeading('h4')"><span class="heading-icon">H4</span></button>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="showTablePicker" class="table-picker-teleport" :style="tablePickerStyle" @click.stop>
        <div class="table-picker-grid">
          <div v-for="r in 6" :key="r" class="table-picker-row">
            <button
              v-for="c in 6" :key="c"
              class="table-picker-cell"
              :class="{ active: r <= tablePickerHoverRow && c <= tablePickerHoverCol }"
              @mouseenter="onTablePickerHover(r, c)"
              @click="confirmTablePicker"
            ></button>
          </div>
        </div>
        <div class="table-picker-label">{{ tablePickerHoverRow > 0 ? `${tablePickerHoverRow} × ${tablePickerHoverCol}` : '选择大小' }}</div>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="showMermaidEditDialog" class="mermaid-dialog-overlay" @click="closeMermaidEditDialog">
        <div class="mermaid-dialog" @click.stop>
          <div class="mermaid-dialog-header">
            <h3>编辑 Mermaid 图表</h3>
            <button class="mermaid-dialog-close" @click="closeMermaidEditDialog">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="mermaid-dialog-content">
            <textarea
              v-model="mermaidEditSource"
              class="mermaid-edit-textarea"
              placeholder="输入 mermaid 代码..."
            ></textarea>
          </div>
          <div class="mermaid-dialog-footer">
            <button class="mermaid-dialog-cancel" @click="closeMermaidEditDialog">取消</button>
            <button class="mermaid-dialog-save" @click="saveMermaidEdit">保存</button>
          </div>
        </div>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="showMermaidZoomDialog" class="mermaid-zoom-overlay" @click="closeMermaidZoomDialog" @touchstart.passive="onMermaidPinchStart" @touchmove="onMermaidPinchMove" @touchend.passive="onMermaidPinchEnd">
        <div class="mermaid-zoom-card" @click.stop>
          <div class="mermaid-zoom-header">
            <h3>Mermaid 图表</h3>
            <div class="mermaid-zoom-controls">
              <label class="zoom-label">显示比例:</label>
              <input
                type="range"
                min="25"
                max="300"
                step="5"
                v-model.number="mermaidZoomScale"
                class="zoom-slider"
              />
              <span class="zoom-value">{{ mermaidZoomScale }}%</span>
              <button class="zoom-reset-btn" @click="mermaidZoomScale = 100" title="重置">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
                  <path d="M3 3v5h5"/>
                </svg>
              </button>
            </div>
            <button class="mermaid-zoom-close" @click="closeMermaidZoomDialog">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="mermaid-zoom-content" ref="mermaidZoomContentRef">
            <div class="mermaid-zoom-spacer" :style="mermaidZoomSpacerStyle">
              <div class="mermaid-zoom-svg" :style="mermaidZoomSvgStyle" ref="mermaidZoomSvgRef"></div>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="showMathEditDialog" class="math-dialog-overlay" @click="closeMathEditDialog">
        <div class="math-dialog" @click.stop>
          <div class="math-dialog-header">
            <h3>编辑数学公式</h3>
            <button class="math-dialog-close" @click="closeMathEditDialog">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="math-dialog-content">
            <textarea
              v-model="mathEditTex"
              class="math-edit-textarea"
              placeholder="输入 LaTeX 公式..."
            ></textarea>
            <div class="math-preview-label">预览:</div>
            <div class="math-preview" v-html="mathPreviewHtml"></div>
          </div>
          <div class="math-dialog-footer">
            <button class="math-dialog-cancel" @click="closeMathEditDialog">取消</button>
            <button class="math-dialog-save" @click="saveMathEdit">保存</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { useNotesStore } from '@/stores/notes'
import { useAsrStreaming } from '@/composables/useAsrStreaming'
import { useAsrHotwords } from '@/composables/useAsrHotwords'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { navigateWithMobileHistory } from '@/composables/useMobileNavigation'
import { renderMarkdownToHtml, renderMermaidBlocks, renderSingleMermaidBlock, fixLostMermaidBlocks, renderEchartsBlocks, fixLostEchartsBlocks, attachMathEditListeners, katexModule } from '@/composables/useMarkdown'
import ExportProgressDialog from './ExportProgressDialog.vue'
import WysiwygEditor from './WysiwygEditor.vue'
import { resolveImageUrl, uploadImage, uploadMedia } from '@/api/imageUpload'

const route = useRoute()
const router = useRouter()
const notesStore = useNotesStore()
const { show: showToast } = useToast()
const { confirm: showConfirm, confirmThreeWay } = useConfirmDialog()
const { hotwords: asrHotwords } = useAsrHotwords()

onMounted(() => {
  notesStore.saveLastNotesPath(route.fullPath)
})

onUnmounted(() => {
  notesStore.saveLastNotesPath(route.fullPath)
})

const noteId = computed(() => route.params.noteId as string)
const notebookId = computed(() => route.params.notebookId as string)
const note = computed(() => notesStore.currentNote)

const previewRef = ref<HTMLElement | null>(null)
const findInputRef = ref<HTMLInputElement | null>(null)
const headingBtnRef = ref<HTMLButtonElement | null>(null)
const wysiwygEditorRef = ref<InstanceType<typeof WysiwygEditor> | null>(null)
const noteTitle = ref('')
const noteContent = ref('')
const hasChanges = ref(false)
const saving = ref(false)
let initialLoad = true
const AUTO_SAVE_INTERVAL = 5 * 60 * 1000
let autoSaveTimer: ReturnType<typeof setInterval> | null = null
const isEditing = ref(false)
const savedScrollTop = ref(0)
const showExportPicker = ref(false)
const exporting = ref(false)
const exportFormat = ref<'md' | 'pdf'>('md')
const exportStatus = ref<'idle' | 'exporting' | 'success'>('idle')
const exportAbortController = ref<AbortController | null>(null)
const exportTaskId = ref<string | null>(null)
const exportProgress = ref(0)

const showImageUploadInput = ref(false)
const imageUploadInputRef = ref<HTMLInputElement | null>(null)
const mediaUploadInputRef = ref<HTMLInputElement | null>(null)
const pendingMediaKind = ref<'audio' | 'video'>('audio')

async function handleImageUpload(file: File): Promise<string> {
  const result = await uploadImage(file)
  return result.path
}

async function handleMediaUpload(file: File): Promise<string> {
  const result = await uploadMedia(file)
  return result.path
}

function triggerImageUpload() {
  wysiwygEditorRef.value?.saveEditorSelection()
  imageUploadInputRef.value?.click()
}

function triggerAudioUpload() {
  pendingMediaKind.value = 'audio'
  wysiwygEditorRef.value?.saveEditorSelection()
  mediaUploadInputRef.value?.click()
}

function triggerVideoUpload() {
  pendingMediaKind.value = 'video'
  wysiwygEditorRef.value?.saveEditorSelection()
  mediaUploadInputRef.value?.click()
}

async function onImageFileSelected(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  input.value = ''
  try {
    const src = await handleImageUpload(file)
    wysiwygEditorRef.value?.insertImage(src, file.name)
  } catch (e) {
    console.error('Image upload failed:', e)
    showToast('图片上传失败', 'error')
  }
}

async function onMediaFileSelected(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  input.value = ''
  try {
    const src = await handleMediaUpload(file)
    wysiwygEditorRef.value?.insertMedia(pendingMediaKind.value, src, file.name)
  } catch (e) {
    console.error('Media upload failed:', e)
    showToast(pendingMediaKind.value === 'video' ? '视频上传失败' : '音频上传失败', 'error')
  }
}

function onObjectAlign(align: 'left' | 'center' | 'right') {
  const editor = wysiwygEditorRef.value
  if (!editor) return
  const editorState = editor.imageResizeState
  let img: HTMLImageElement | null = editorState?.img || null
  if (img) {
    editor.setImageAlignment(img, align)
    return
  }
  const multiSels = editor.getMultiSelections?.()
  if (multiSels && multiSels.size > 0) {
    const editorEl = editor.editorRef
    if (editorEl) {
      const blocks = editorEl.querySelectorAll('.multi-selected') as NodeListOf<HTMLElement>
      blocks.forEach(block => {
        if (block.tagName === 'IMG') {
          editor.setImageAlignment(block as unknown as HTMLImageElement, align)
        } else {
          editor.setBlockAlignment(block, align)
        }
      })
    }
    return
  }
  const block = editor.getCurrentBlockElement()
  if (block) {
    editor.setBlockAlignment(block, align)
  }
}

const showFindBar = ref(false)
const showReplaceBar = ref(false)
const showHeadingMenu = ref(false)
const currentHeadingLevel = ref<string | null>(null)
const showToc = ref(false)
const showFontColorPicker = ref(false)
const fontColorBtnRef = ref<HTMLButtonElement | null>(null)
const customFontColor = ref('#ff0000')
const selectedFontSize = ref('')
const showHighlightColorPicker = ref(false)
const highlightBtnRef = ref<HTMLButtonElement | null>(null)
const customHighlightColor = ref('#ffff00')
const showTablePicker = ref(false)
const tableBtnRef = ref<HTMLButtonElement | null>(null)
const tablePickerHoverRow = ref(0)
const tablePickerHoverCol = ref(0)
const showMermaidEditDialog = ref(false)
const showMermaidZoomDialog = ref(false)
const showMathEditDialog = ref(false)
const mermaidEditSource = ref('')
const mermaidEditId = ref('')
const mermaidZoomSource = ref('')
const mermaidZoomId = ref('')
const mermaidZoomScale = ref(100)
const mermaidZoomContentRef = ref<HTMLElement | null>(null)
const mermaidZoomSvgRef = ref<HTMLElement | null>(null)
const mermaidSvgWidth = ref(400)
const mermaidSvgHeight = ref(300)
const mermaidSvgViewBox = ref('0 0 400 300')

const REFERENCE_SIZE = 1200

const mermaidZoomSpacerStyle = computed(() => {
  const scale = mermaidZoomScale.value / 100
  return {
    width: `${mermaidSvgWidth.value * scale}px`,
    height: `${mermaidSvgHeight.value * scale}px`,
  }
})

const mermaidZoomSvgStyle = computed(() => {
  const scale = mermaidZoomScale.value / 100
  return {
    width: `${mermaidSvgWidth.value * scale}px`,
    height: `${mermaidSvgHeight.value * scale}px`,
  }
})

function updateZoomSvgScale() {
  const container = mermaidZoomSvgRef.value
  if (!container) return
  const innerSvg = container.querySelector('svg')
  if (innerSvg) {
    const scale = mermaidZoomScale.value / 100
    const w = mermaidSvgWidth.value * scale
    const h = mermaidSvgHeight.value * scale
    innerSvg.setAttribute('width', `${w}`)
    innerSvg.setAttribute('height', `${h}`)
    innerSvg.setAttribute('viewBox', mermaidSvgViewBox.value)
    innerSvg.style.width = `${w}px`
    innerSvg.style.height = `${h}px`
  }
}
const mathEditTex = ref('')
const mathEditDisplayMode = ref(false)
const mathEditElement = ref<HTMLElement | null>(null)
const headingMenuStyle = computed(() => {
  const btn = headingBtnRef.value
  if (!btn) return {}
  const rect = btn.getBoundingClientRect()
  return {
    position: 'fixed' as const,
    top: `${rect.bottom + 4}px`,
    left: `${rect.left}px`,
    zIndex: 9999,
  }
})

const fontColorPickerStyle = computed(() => {
  const btn = fontColorBtnRef.value
  if (!btn) return {}
  const rect = btn.getBoundingClientRect()
  return {
    position: 'fixed' as const,
    top: `${rect.bottom + 4}px`,
    left: `${rect.left}px`,
    zIndex: 9999,
  }
})

const highlightColorPickerStyle = computed(() => {
  const btn = highlightBtnRef.value
  if (!btn) return {}
  const rect = btn.getBoundingClientRect()
  return {
    position: 'fixed' as const,
    top: `${rect.bottom + 4}px`,
    left: `${rect.left}px`,
    zIndex: 9999,
  }
})

const fontColors = [
  '#000000', '#e60000', '#ff9900', '#ffff00', '#008a00', '#0066cc',
  '#9933ff', '#ffffff', '#facccc', '#ffebcc', '#ffffcc', '#cce8cc',
  '#cce0ff', '#d9c2f0', '#bbbbbb', '#f06666', '#ffc266', '#ffff66',
  '#66b966', '#66a3e0', '#c285ff', '#888888', '#a10000', '#b26b00',
  '#b2b200', '#006100', '#0047b2', '#6b24b2', '#444444', '#5c0000',
  '#663d00', '#666600', '#003700', '#002966', '#3d1466',
]

const highlightColors = [
  '#ffff00', '#ff9900', '#ff6666', '#99ff99', '#99ccff', '#cc99ff',
  '#ffcc99', '#cccccc', '#ff0000', '#00ff00', '#0000ff', '#ff00ff',
  '#00ffff', '#ffffff', '#000000',
]

interface TocItem { id: string; text: string; level: number; index: number; hasChildren: boolean }
const tocItems = ref<TocItem[]>([])
const collapsedToc = ref<Set<number>>(new Set())
const visibleTocItems = computed(() => {
  const items = tocItems.value
  const visible: TocItem[] = []
  let skipBelowLevel = Infinity
  for (const item of items) {
    if (item.level > skipBelowLevel) continue
    skipBelowLevel = Infinity
    if (collapsedToc.value.has(item.index)) skipBelowLevel = item.level
    visible.push(item)
  }
  return visible
})

function closeAllToolbarMenus() {
  showHeadingMenu.value = false
  showTablePicker.value = false
  showHighlightColorPicker.value = false
  showFontColorPicker.value = false
}

function toggleHeadingMenu() {
  if (showHeadingMenu.value) {
    showHeadingMenu.value = false
  } else {
    closeAllToolbarMenus()
    updateHeadingButtonState()
    showHeadingMenu.value = true
  }
}

const isBold = ref(false)
const isItalic = ref(false)
const isQuote = ref(false)
const isSup = ref(false)
const isSub = ref(false)

function updateHeadingButtonState() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.getCurrentHeadingLevel) {
    currentHeadingLevel.value = editor.getCurrentHeadingLevel()
  } else {
    currentHeadingLevel.value = null
  }
  updateFormatStates()
}

function updateFormatStates() {
  const editorEl = wysiwygEditorRef.value?.editorRef
  if (!editorEl) { isBold.value = isItalic.value = isQuote.value = isSup.value = isSub.value = false; return }
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorEl.contains(sel.anchorNode)) {
    isBold.value = isItalic.value = isQuote.value = isSup.value = isSub.value = false
    return
  }
  let bold = false, italic = false
  try { bold = document.queryCommandState('bold') } catch {}
  try { italic = document.queryCommandState('italic') } catch {}
  let node: Node | null = sel.anchorNode
  let quote = false, sup = false, sub = false
  while (node && node !== editorEl) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = (node as HTMLElement).tagName
      if (tag === 'B' || tag === 'STRONG') bold = true
      if (tag === 'I' || tag === 'EM') italic = true
      if (tag === 'BLOCKQUOTE') quote = true
      if (tag === 'SUP') sup = true
      if (tag === 'SUB') sub = true
    }
    node = node.parentNode
  }
  isBold.value = bold
  isItalic.value = italic
  isQuote.value = quote
  isSup.value = sup
  isSub.value = sub
}

function toggleHighlightColorPicker() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.saveEditorSelection) {
    editor.saveEditorSelection()
  }
  if (showHighlightColorPicker.value) {
    showHighlightColorPicker.value = false
  } else {
    closeAllToolbarMenus()
    showHighlightColorPicker.value = true
  }
}

function applyHighlightColor(color: string) {
  showHighlightColorPicker.value = false
  const editor = wysiwygEditorRef.value
  if (editor) {
    if (editor.restoreEditorSelection) {
      editor.restoreEditorSelection()
    }
    if (editor.saveEditorSelection) {
      editor.saveEditorSelection()
    }
    if (editor.applyHighlightToSelection) {
      editor.applyHighlightToSelection(color)
    }
  }
}

function removeHighlight() {
  showHighlightColorPicker.value = false
  const editor = wysiwygEditorRef.value
  if (editor) {
    if (editor.restoreEditorSelection) {
      editor.restoreEditorSelection()
    }
    if (editor.saveEditorSelection) {
      editor.saveEditorSelection()
    }
    if (editor.applyHighlightToSelection) {
      editor.applyHighlightToSelection('')
    }
  }
}

const tablePickerStyle = computed(() => {
  const btn = tableBtnRef.value
  if (!btn) return {}
  const rect = btn.getBoundingClientRect()
  return {
    position: 'fixed' as const,
    top: `${rect.bottom + 4}px`,
    left: `${rect.left}px`,
    zIndex: 10000,
  }
})

function toggleTablePicker() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.saveEditorSelection) {
    editor.saveEditorSelection()
  }
  if (showTablePicker.value) {
    showTablePicker.value = false
  } else {
    closeAllToolbarMenus()
    showTablePicker.value = true
  }
  tablePickerHoverRow.value = 0
  tablePickerHoverCol.value = 0
}

function onTablePickerHover(row: number, col: number) {
  tablePickerHoverRow.value = row
  tablePickerHoverCol.value = col
}

function confirmTablePicker() {
  const rows = tablePickerHoverRow.value
  const cols = tablePickerHoverCol.value
  if (rows < 1 || cols < 1) return
  showTablePicker.value = false
  const editor = wysiwygEditorRef.value
  if (editor) {
    if (editor.restoreEditorSelection) {
      editor.restoreEditorSelection()
    }
    editor.insertTable(rows, cols)
  }
}

const findQuery = ref('')
const replaceQuery = ref('')
const matchPositions = ref<number[]>([])
const currentMatchIndex = ref(-1)

let pinchStartDistance = 0
let pinchStartScale = 100

function onMermaidPinchStart(e: TouchEvent) {
  if (e.touches.length !== 2) return
  const dx = e.touches[0].clientX - e.touches[1].clientX
  const dy = e.touches[0].clientY - e.touches[1].clientY
  pinchStartDistance = Math.hypot(dx, dy)
  pinchStartScale = mermaidZoomScale.value
}

function onMermaidPinchMove(e: TouchEvent) {
  if (e.touches.length !== 2 || pinchStartDistance === 0) return
  e.preventDefault()
  const dx = e.touches[0].clientX - e.touches[1].clientX
  const dy = e.touches[0].clientY - e.touches[1].clientY
  const distance = Math.hypot(dx, dy)
  const ratio = distance / pinchStartDistance
  const newScale = Math.round(pinchStartScale * ratio)
  mermaidZoomScale.value = Math.min(300, Math.max(25, newScale))
}

function onMermaidPinchEnd(e: TouchEvent) {
  if (e.touches.length < 2) {
    pinchStartDistance = 0
  }
}

watch(mermaidZoomScale, () => {
  updateZoomSvgScale()
})

watch(
  [showMermaidEditDialog, showMermaidZoomDialog, showMathEditDialog],
  () => {
    nextTick(() => {
      setTimeout(() => {
        const container = wysiwygEditorRef.value?.editorRef || previewRef.value
        if (container) {
          fixLostMermaidBlocks(container)
          renderMermaidBlocks(container)
          fixLostEchartsBlocks(container)
          renderEchartsBlocks(container)
          attachMathEditListeners(container)
        }
      }, 50)
    })
  }
)

function decodePreviewHash(rawHash: string): string {
  const value = rawHash.replace(/^#/, '')
  if (!value) return ''
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

function scrollPreviewToElement(container: HTMLElement, element: HTMLElement) {
  const containerRect = container.getBoundingClientRect()
  const targetRect = element.getBoundingClientRect()
  const scrollMarginTop = Number.parseFloat(getComputedStyle(element).scrollMarginTop || '0') || 0
  const nextTop = container.scrollTop + (targetRect.top - containerRect.top) - scrollMarginTop
  container.scrollTo({
    top: Math.max(0, nextTop),
    behavior: 'smooth',
  })
}

function onPreviewClick(ev: MouseEvent) {
  const target = ev.target as HTMLElement | null
  const root = previewRef.value
  if (!target || !root) return

  const anchor = target.closest?.('a[href^="#"]') as HTMLAnchorElement | null
  if (anchor && root.contains(anchor)) {
    const hash = anchor.getAttribute('href') || ''
    const id = decodePreviewHash(hash)
    if (id) {
      const el = root.querySelector(`#${CSS.escape(id)}`) as HTMLElement | null
      if (el) {
        ev.preventDefault()
        scrollPreviewToElement(root, el)
        try { history.replaceState(null, '', `#${id}`) } catch { /* ignore */ }
        return
      }
    }
  }

  let node: HTMLElement | null = target
  while (node && node !== root && !/^H[1-6]$/.test(node.tagName)) {
    node = node.parentElement
  }
  if (!node || node === root || !/^H[1-6]$/.test(node.tagName)) return
  const id = node.id
  if (!id) return
  ev.preventDefault()
  scrollPreviewToElement(root, node)
  try {
    history.replaceState(null, '', `#${id}`)
  } catch { /* ignore */ }
}
watch(previewRef, (el, oldEl) => {
  if (oldEl) oldEl.removeEventListener('click', onPreviewClick)
  if (el) el.addEventListener('click', onPreviewClick)
})

let asrInsertPos = 0
let asrPreviousLen = 0
let asrNeedNewline = false

const asrStreaming = useAsrStreaming({
  customHotwords: () => asrHotwords.value,
  onPartial(payload) {
    const text = payload.text || ''
    const prefix = asrNeedNewline ? '\n' : ''
    const fullText = prefix + text
    const before = noteContent.value.substring(0, asrInsertPos)
    const after = noteContent.value.substring(asrInsertPos + (asrPreviousLen > 0 ? asrPreviousLen : 0))
    noteContent.value = before + fullText + after
    asrPreviousLen = fullText.length
    hasChanges.value = true
  },
  onError(message) {
    showToast(message, 'error')
  },
})
const isRecording = asrStreaming.isRecording
const isProcessing = asrStreaming.isFinishing

const isNewEmptyNote = ref(false)

function getDraftKey(id: string) {
  return `chatllm_draft_note_${id}`
}

function saveDraft() {
  const id = noteId.value
  if (!id) return
  const draft = JSON.stringify({ title: noteTitle.value, content: noteContent.value })
  sessionStorage.setItem(getDraftKey(id), draft)
}

function clearDraft() {
  const id = noteId.value
  if (!id) return
  sessionStorage.removeItem(getDraftKey(id))
}

watch([noteTitle, noteContent], () => {
  if (hasChanges.value) {
    saveDraft()
  }
})

watch(noteId, async (newId, oldId) => {
  if (!oldId || !newId || newId === oldId || initialLoad) return
  initialLoad = true
  await notesStore.loadNote(newId)
  if (note.value) {
    noteTitle.value = note.value.title || ''
    noteContent.value = note.value.content || ''
    isNewEmptyNote.value = !noteTitle.value && !noteContent.value
    const draftJson = sessionStorage.getItem(getDraftKey(newId))
    if (draftJson) {
      try {
        const draft = JSON.parse(draftJson)
        if (draft.title !== noteTitle.value || draft.content !== noteContent.value) {
          noteTitle.value = draft.title ?? noteTitle.value
          noteContent.value = draft.content ?? noteContent.value
          hasChanges.value = true
        }
      } catch { /* ignore invalid draft */ }
    }
  }
  hasChanges.value = false
  nextTick(() => {
    initialLoad = false
    wysiwygEditorRef.value?.resetUndoStack()
    void applyPendingSearchHighlight()
  })
})

// wave-9：侧栏/工作台菜单外部重命名当前打开的笔记时同步本地标题，
// 否则下次保存（或草稿恢复）会把旧标题写回。仅在无未保存改动时生效，避免覆盖正在输入的内容。
watch(note, (n) => {
  if (n && n.id === noteId.value && !hasChanges.value && n.title !== noteTitle.value) {
    noteTitle.value = n.title || ''
  }
})

onMounted(async () => {
  initialLoad = true
  await notesStore.loadNote(noteId.value)
  if (note.value) {
    noteTitle.value = note.value.title || ''
    noteContent.value = note.value.content || ''
    isNewEmptyNote.value = !noteTitle.value && !noteContent.value

    const draftJson = sessionStorage.getItem(getDraftKey(noteId.value))
    if (draftJson) {
      try {
        const draft = JSON.parse(draftJson)
        if (draft.title !== noteTitle.value || draft.content !== noteContent.value) {
          noteTitle.value = draft.title ?? noteTitle.value
          noteContent.value = draft.content ?? noteContent.value
          hasChanges.value = true
        }
      } catch { /* ignore invalid draft */ }
    }
  }
  nextTick(() => {
    initialLoad = false
    wysiwygEditorRef.value?.resetUndoStack()
    void applyPendingSearchHighlight()
  })
})

// Watch for WysiwygEditor ref to set previewRef
watch(wysiwygEditorRef, (newVal) => {
  if (newVal?.editorRef) {
    previewRef.value = newVal.editorRef
  }
}, { immediate: true })

// Watch noteContent for changes to set hasChanges (backup for @change event)
watch(noteContent, (newVal, oldVal) => {
  if (!initialLoad && newVal !== oldVal) {
    hasChanges.value = true
  }
})

const allowLeave = ref(false)
const isDeleting = ref(false)

async function goBack() {
  if (isDeleting.value) {
    allowLeave.value = true
    await navigateWithMobileHistory(router, `/notes/${notebookId.value}`)
    return
  }
  if (isNewEmptyNote.value && !noteTitle.value.trim() && !noteContent.value.trim()) {
    await notesStore.deleteNote(noteId.value).catch(() => {})
    allowLeave.value = true
    await navigateWithMobileHistory(router, `/notes/${notebookId.value}`)
    return
  }
  // Flush the deferred serialize so the hasChanges gate sees the latest edits
  // (a route-leave within the same tick as typing must not skip the prompt).
  await wysiwygEditorRef.value?.flushPendingSerialization?.()
  await nextTick()
  if (hasChanges.value) {
    const result = await confirmThreeWay({
      message: '有未保存的更改，是否保存？',
      threeButton: { saveText: '保存', discardText: '不保存', cancelText: '取消' }
    })
    if (result === 'cancel') return
    if (result === 'save') {
      await saveNote()
    } else {
      clearDraft()
      if (note.value) {
        noteTitle.value = note.value.title || ''
        noteContent.value = note.value.content || ''
      }
      hasChanges.value = false
    }
  }
  allowLeave.value = true
  await navigateWithMobileHistory(router, `/notes/${notebookId.value}`)
}

onBeforeRouteLeave(async (_to, _from, next) => {
  if (allowLeave.value || isDeleting.value) {
    next()
    return
  }
  if (isNewEmptyNote.value && !noteTitle.value.trim() && !noteContent.value.trim()) {
    await notesStore.deleteNote(noteId.value).catch(() => {})
    next()
    return
  }
  await wysiwygEditorRef.value?.flushPendingSerialization?.()
  await nextTick()
  if (!hasChanges.value) {
    next()
    return
  }
  const result = await confirmThreeWay({
    message: '有未保存的更改，是否保存？',
    threeButton: { saveText: '保存', discardText: '不保存', cancelText: '取消' }
  })
  if (result === 'cancel') {
    next(false)
    return
  }
  if (result === 'save') {
    await saveNote()
  } else {
    clearDraft()
    if (note.value) {
      noteTitle.value = note.value.title || ''
      noteContent.value = note.value.content || ''
    }
    hasChanges.value = false
  }
  next()
})

onBeforeRouteUpdate(async (_to, _from, next) => {
  if (isDeleting.value) {
    next()
    return
  }
  if (isNewEmptyNote.value && !noteTitle.value.trim() && !noteContent.value.trim()) {
    await notesStore.deleteNote(noteId.value).catch(() => {})
    next()
    return
  }
  await wysiwygEditorRef.value?.flushPendingSerialization?.()
  await nextTick()
  if (!hasChanges.value) {
    next()
    return
  }
  const result = await confirmThreeWay({
    message: '有未保存的更改，是否保存？',
    threeButton: { saveText: '保存', discardText: '不保存', cancelText: '取消' }
  })
  if (result === 'cancel') {
    next(false)
    return
  }
  if (result === 'save') {
    await saveNote()
  } else {
    clearDraft()
    if (note.value) {
      noteTitle.value = note.value.title || ''
      noteContent.value = note.value.content || ''
    }
    hasChanges.value = false
  }
  next()
})

async function saveNote() {
  if (saving.value) return
  saving.value = true
  try {
    // Pasted images upload in the background; saving before they finish would
    // persist markdown with the images still missing (blob:/data: dropped).
    await wysiwygEditorRef.value?.flushPendingImageUploads?.()
    // Serialization is deferred to a microtask; flush so noteContent is fresh.
    await wysiwygEditorRef.value?.flushPendingSerialization?.()
    await notesStore.updateNote(noteId.value, {
      title: noteTitle.value || null,
      content: noteContent.value
    })
    hasChanges.value = false
    clearDraft()
  } catch (e) {
    console.error('Failed to save note:', e)
  } finally {
    saving.value = false
  }
}

async function confirmDelete() {
  if (!await showConfirm({ message: '确定要删除这条笔记吗？', danger: true, confirmText: '删除' })) return
  isDeleting.value = true
  clearDraft()
  try {
    await notesStore.deleteNote(noteId.value)
    await navigateWithMobileHistory(router, `/notes/${notebookId.value}`)
  } catch (e) {
    isDeleting.value = false
    console.error('Failed to delete note:', e)
  }
}

async function handleExport(format: 'md' | 'pdf') {
  showExportPicker.value = false
  exportFormat.value = format
  exportStatus.value = 'exporting'
  exporting.value = true
  exportProgress.value = 0
  exportTaskId.value = null
  try {
    await notesStore.exportNoteAsync(noteId.value, format, (task) => {
      exportTaskId.value = task.id
      exportProgress.value = Math.round(task.progress * 100)
    })
    exportStatus.value = 'success'
    showToast('导出成功', 'success')
  } catch (e: any) {
    exportStatus.value = 'idle'
    if (e?.message === '导出已取消') {
      showToast('已取消导出', 'info')
    } else {
      console.error('Export failed:', e)
      showToast(e?.message || '导出失败', 'error')
    }
  } finally {
    exporting.value = false
    exportAbortController.value = null
  }
}

function cancelExport() {
  if (exportTaskId.value) {
    notesStore.cancelExportTask(exportTaskId.value)
  }
  exportStatus.value = 'idle'
}

function closeExportDialog() {
  exportStatus.value = 'idle'
}

function clearInlineFormatInText(text: string, marker: string): string {
  let result = ''
  let i = 0
  while (i < text.length) {
    if (i <= text.length - marker.length * 2 && text.substring(i, i + marker.length) === marker) {
      const afterOpen = i + marker.length
      let closeIdx = -1
      let search = afterOpen
      while (search < text.length - marker.length + 1) {
        if (text.substring(search, search + marker.length) === marker) {
          closeIdx = search
          break
        }
        search++
      }
      if (closeIdx !== -1) {
        result += text.substring(afterOpen, closeIdx)
        i = closeIdx + marker.length
      } else {
        result += text[i]
        i++
      }
    } else {
      result += text[i]
      i++
    }
  }
  return result
}

function toggleInlineFormat(prefix: string, suffix: string) {
  const textarea = textareaRef.value
  if (!textarea) return

  const scrollTop = textarea.scrollTop
  const start = textarea.selectionStart
  const end = textarea.selectionEnd
  const text = noteContent.value
  const selectedText = text.substring(start, end)

  if (start >= prefix.length && end <= text.length - suffix.length) {
    const beforeSel = text.substring(start - prefix.length, start)
    const afterSel = text.substring(end, end + suffix.length)
    if (beforeSel === prefix && afterSel === suffix) {
      const inner = text.substring(start, end)
      const cleaned = clearInlineFormatInText(inner, prefix)
      const newEnd = start - prefix.length + cleaned.length
      noteContent.value = text.substring(0, start - prefix.length) + cleaned + text.substring(end + suffix.length)
      hasChanges.value = true
      nextTick(() => {
        textarea.focus({ preventScroll: true })
        textarea.scrollTop = scrollTop
        textarea.setSelectionRange(start - prefix.length, newEnd)
      })
      recordState()
      return
    }
  }

  if (selectedText.length > 0) {
    const cleaned = clearInlineFormatInText(selectedText, prefix)
    noteContent.value = text.substring(0, start) + prefix + cleaned + suffix + text.substring(end)
    hasChanges.value = true
    nextTick(() => {
      textarea.focus({ preventScroll: true })
      textarea.scrollTop = scrollTop
      textarea.setSelectionRange(start + prefix.length, start + prefix.length + cleaned.length)
    })
    recordState()
    return
  }

  noteContent.value = text.substring(0, start) + prefix + '文本' + suffix + text.substring(end)
  hasChanges.value = true
  nextTick(() => {
    textarea.focus({ preventScroll: true })
    textarea.scrollTop = scrollTop
    textarea.setSelectionRange(start + prefix.length, start + prefix.length + 2)
  })
  recordState()
}

function insertHorizontalRule() {
  const textarea = textareaRef.value
  if (!textarea) return

  const scrollTop = textarea.scrollTop
  const start = textarea.selectionStart
  const end = textarea.selectionEnd
  const text = noteContent.value

  const before = text.substring(0, start)
  const after = text.substring(end)

  let prefix = ''
  if (before.length > 0 && !before.endsWith('\n\n')) {
    prefix = before.endsWith('\n') ? '\n' : '\n\n'
  }
  let suffix = ''
  if (after.length === 0) {
    suffix = '\n'
  } else if (!after.startsWith('\n\n')) {
    suffix = after.startsWith('\n') ? '\n' : '\n\n'
  }

  const insertion = `${prefix}---${suffix}`
  noteContent.value = before + insertion + after
  hasChanges.value = true
  const cursorPos = start + insertion.length
  nextTick(() => {
    textarea.focus({ preventScroll: true })
    textarea.scrollTop = scrollTop
    textarea.setSelectionRange(cursorPos, cursorPos)
  })
  recordState()
}

function insertCodeBlock() {
  execCommand('formatBlock', 'pre')
}

const LINE_PREFIXES = {
  heading: ['# ', '## ', '### ', '#### '],
  quote: ['> '],
  bullet: ['- '],
  ordered: ['1. '],
}

function getPrefixGroup(prefix: string): string[] {
  for (const group of Object.values(LINE_PREFIXES)) {
    if (group.includes(prefix)) return group
  }
  return [prefix]
}

function stripLinePrefixes(line: string, prefixes: string[]): { stripped: string; removed: string } {
  for (const p of prefixes) {
    if (line.startsWith(p)) {
      return { stripped: line.substring(p.length), removed: p }
    }
  }
  return { stripped: line, removed: '' }
}

function toggleLinePrefix(prefix: string) {
  const textarea = textareaRef.value
  if (!textarea) return

  const scrollTop = textarea.scrollTop
  const start = textarea.selectionStart
  const end = textarea.selectionEnd
  const text = noteContent.value

  const selStartLine = text.lastIndexOf('\n', start - 1) + 1
  const selEndLine = text.indexOf('\n', end)
  const selectionEndLine = selEndLine === -1 ? text.length : selEndLine

  const selectedBlock = text.substring(selStartLine, selectionEndLine)
  const lines = selectedBlock.split('\n')
  const prefixGroup = getPrefixGroup(prefix)
  const isOrderedPrefix = /^\d+\.\s/.test(prefix)

  const allHavePrefix = lines.every(line => line.startsWith(prefix) || (isOrderedPrefix && /^\d+\.\s/.test(line)))

  if (allHavePrefix) {
    let totalRemoved = 0
    let firstLineRemoved = 0
    const stripped = lines.map((line, idx) => {
      let removedLen = 0
      if (isOrderedPrefix) {
        const m = line.match(/^(\d+\.\s)/)
        if (m) removedLen = m[1].length
      } else if (line.startsWith(prefix)) {
        removedLen = prefix.length
      }
      if (idx === 0) firstLineRemoved = removedLen
      totalRemoved += removedLen
      return line.substring(removedLen)
    })
    const result = stripped.join('\n')
    noteContent.value = text.substring(0, selStartLine) + result + text.substring(selectionEndLine)
    hasChanges.value = true
    nextTick(() => {
      textarea.focus({ preventScroll: true })
      textarea.scrollTop = scrollTop
      textarea.setSelectionRange(Math.max(selStartLine, start - firstLineRemoved), Math.max(selStartLine, end - totalRemoved))
    })
    recordState()
    return
  }

  let totalAdded = 0
  let firstLineDelta = 0
  const processed = lines.map((line, idx) => {
    const { stripped, removed } = stripLinePrefixes(line, prefixGroup)
    if (isOrderedPrefix && /^\d+\.\s/.test(removed)) {
      const numPrefix = `${idx + 1}. `
      const delta = numPrefix.length - removed.length
      if (idx === 0) firstLineDelta = delta
      totalAdded += delta
      return numPrefix + stripped
    }
    const newPrefix = isOrderedPrefix ? `${idx + 1}. ` : prefix
    const delta = newPrefix.length - removed.length
    if (idx === 0) firstLineDelta = delta
    totalAdded += delta
    return newPrefix + stripped
  })

  const result = processed.join('\n')
  noteContent.value = text.substring(0, selStartLine) + result + text.substring(selectionEndLine)
  hasChanges.value = true

  nextTick(() => {
    textarea.focus({ preventScroll: true })
    textarea.scrollTop = scrollTop
    textarea.setSelectionRange(start + firstLineDelta, end + totalAdded)
  })
  recordState()
}

function onContentChange() {
  if (!initialLoad) {
    hasChanges.value = true
  }
  // Recompute find/replace matches whenever content changes while find bar is active
  if (showFindBar.value && findQuery.value) {
    computeMatches()
  }
}

function onEditorKeydown(e: KeyboardEvent) {
  if (showFindBar.value) {
    if (e.key === 'Escape') {
      e.preventDefault()
      closeFindBar()
      return
    }
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault()
    openFindBar(false)
    return
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'h') {
    e.preventDefault()
    openFindBar(true)
    return
  }
}

function computeMatches() {
  const text = wysiwygEditorRef.value?.getTextContent() || ''
  const query = findQuery.value
  if (!query) {
    matchPositions.value = []
    currentMatchIndex.value = -1
    return
  }
  const positions: number[] = []
  let idx = 0
  const lowerText = text.toLowerCase()
  const lowerQuery = query.toLowerCase()
  while (idx < lowerText.length) {
    const found = lowerText.indexOf(lowerQuery, idx)
    if (found === -1) break
    positions.push(found)
    idx = found + 1
  }
  matchPositions.value = positions
  if (positions.length === 0) {
    currentMatchIndex.value = -1
  } else if (currentMatchIndex.value >= positions.length) {
    currentMatchIndex.value = 0
  }
}

function highlightCurrentMatch() {
  if (matchPositions.value.length === 0 || currentMatchIndex.value < 0) return
  // Do NOT select text in editor - the highlight CSS already visually marks which match is current.
  // Selecting text would cause Enter key to replace the matched keyword with a newline.
  const editorEl = wysiwygEditorRef.value?.editorRef
  if (!editorEl) return
  const currentMark = editorEl.querySelector('.find-match-current') as HTMLElement | null
  if (currentMark) {
    currentMark.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
}

function onFindInput() {
  computeMatches()
  if (matchPositions.value.length > 0) {
    currentMatchIndex.value = 0
    nextTick(() => highlightCurrentMatch())
  }
}

function findNext() {
  if (matchPositions.value.length === 0) return
  currentMatchIndex.value = (currentMatchIndex.value + 1) % matchPositions.value.length
  nextTick(() => highlightCurrentMatch())
}

function findPrev() {
  if (matchPositions.value.length === 0) return
  currentMatchIndex.value = (currentMatchIndex.value - 1 + matchPositions.value.length) % matchPositions.value.length
  nextTick(() => highlightCurrentMatch())
}

function onFindKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter') {
    e.preventDefault()
    if (e.shiftKey) {
      findPrev()
    } else {
      findNext()
    }
  } else if (e.key === 'Escape') {
    e.preventDefault()
    closeFindBar()
  }
}

function onReplaceKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    closeFindBar()
  }
}

function replaceCurrent() {
  const editor = wysiwygEditorRef.value
  if (!editor || matchPositions.value.length === 0 || currentMatchIndex.value < 0) return
  const pos = matchPositions.value[currentMatchIndex.value]
  const queryLen = findQuery.value.length
  const success = editor.replaceTextRange?.(pos, pos + queryLen, replaceQuery.value)
  if (success) {
    hasChanges.value = true
    computeMatches()
    if (matchPositions.value.length === 0) {
      currentMatchIndex.value = -1
    } else if (currentMatchIndex.value >= matchPositions.value.length) {
      currentMatchIndex.value = 0
    }
    nextTick(() => highlightCurrentMatch())
  }
}

function replaceAll() {
  const query = findQuery.value
  if (!query || matchPositions.value.length === 0) return
  const editor = wysiwygEditorRef.value
  if (!editor) return

  // Replace from last match to first to avoid index shifts
  for (let i = matchPositions.value.length - 1; i >= 0; i--) {
    const pos = matchPositions.value[i]
    editor.replaceTextRange?.(pos, pos + query.length, replaceQuery.value)
  }
  hasChanges.value = true
  computeMatches()
  currentMatchIndex.value = matchPositions.value.length > 0 ? 0 : -1
  nextTick(() => highlightCurrentMatch())
}

function openFindBar(withReplace: boolean) {
  if (showFindBar.value && showReplaceBar.value === withReplace) {
    closeFindBar()
    return
  }
  showFindBar.value = true
  showReplaceBar.value = withReplace
  nextTick(() => {
    findInputRef.value?.focus()
    if (findQuery.value) {
      computeMatches()
      if (matchPositions.value.length > 0) {
        currentMatchIndex.value = 0
        nextTick(() => highlightCurrentMatch())
      }
    }
  })
}

function closeFindBar() {
  showFindBar.value = false
  showReplaceBar.value = false
  matchPositions.value = []
  currentMatchIndex.value = -1
}

// Apply a one-shot search highlight (set when navigating from a note search
// result). Waits for the editor to finish rendering the note content, then
// opens the find bar with the query so the keyword is highlighted and the
// view scrolls to the first match (the exact location of the clicked snippet).
async function applyPendingSearchHighlight() {
  const q = notesStore.noteSearchHighlightQuery
  if (!q || !q.trim()) return
  notesStore.noteSearchHighlightQuery = ''
  // Wait for the WysiwygEditor to render the note content.
  let retries = 0
  while (retries < 40) {
    await nextTick()
    const editorEl = wysiwygEditorRef.value?.editorRef
    if (editorEl && (editorEl.textContent || '').length > 50) break
    await new Promise(r => setTimeout(r, 200))
    retries++
  }
  findQuery.value = q
  showFindBar.value = true
  showReplaceBar.value = false
  currentMatchIndex.value = 0
  await nextTick()
  computeMatches()
  if (matchPositions.value.length > 0) {
    currentMatchIndex.value = 0
    nextTick(() => highlightCurrentMatch())
  }
  findInputRef.value?.focus()
}

function applyHeading(heading: string) {
  showHeadingMenu.value = false
  const editor = wysiwygEditorRef.value
  if (editor && editor.applyHeadingCommand) {
    editor.applyHeadingCommand(heading)
  } else {
    execCommand('formatBlock', heading)
  }
}

function execCommand(command: string, value?: string) {
  if (wysiwygEditorRef.value) {
    wysiwygEditorRef.value.execCommand(command, value)
  }
}

function handleListCommand(command: string) {
  const editor = wysiwygEditorRef.value
  if (editor && editor.getMultiSelections && editor.getMultiSelections() && editor.getMultiSelections().size > 0) {
    if (command === 'insertUnorderedList') {
      editor.applyToListSelections('ul')
    } else {
      editor.applyToListSelections('ol')
    }
    return
  }
  if (command === 'insertOrderedList') {
    const sel = window.getSelection()
    if (sel && sel.rangeCount > 0) {
      let node: Node | null = sel.getRangeAt(0).startContainer
      while (node && node !== editor?.editorRef) {
        if (node.nodeType === Node.ELEMENT_NODE) {
          const el = node as HTMLElement
          if (el.tagName === 'OL') {
            if (el.parentElement?.tagName === 'LI') {
              return
            }
            execCommand(command)
            return
          }
          if (el.tagName === 'LI') {
            const parentOl = el.parentElement
            if (parentOl && parentOl.tagName === 'OL') {
              if (parentOl.parentElement?.tagName === 'LI') {
                return
              }
              execCommand(command)
              return
            }
          }
        }
        node = node.parentNode
      }
    }
  }
  execCommand(command)
}

function handleRestartNumbering() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.restartOlNumbering && editor.findCurrentOl) {
    if (editor.saveEditorSelection) editor.saveEditorSelection()
    if (editor.restoreEditorSelection) editor.restoreEditorSelection()
    const ol = editor.findCurrentOl()
    if (ol) {
      if (editor.restoreEditorSelection) editor.restoreEditorSelection()
      editor.restartOlNumbering(ol)
    }
  }
}

function handleBlockquoteCommand() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.getMultiSelections && editor.getMultiSelections() && editor.getMultiSelections().size > 0) {
    editor.applyBlockquoteToSelections()
    return
  }
  execCommand('formatBlock', 'blockquote')
}

function applyHighlight() {
  showHighlightColorPicker.value = true
}

function getSelectionFontSize() {
  const editorEl = wysiwygEditorRef.value?.editorRef
  if (!editorEl) { selectedFontSize.value = ''; return }
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorEl.contains(sel.anchorNode)) {
    selectedFontSize.value = ''
    return
  }
  const fontSizeOptions = ['12px', '14px', '16px', '18px', '20px', '24px', '28px', '32px', '36px']

  function normalizeSize(raw: string): string {
    if (!raw) return ''
    if (fontSizeOptions.includes(raw)) return raw
    const px = parseFloat(raw)
    if (isNaN(px)) return ''
    // Snap to nearest available option
    let closest = ''
    let minDiff = Infinity
    for (const opt of fontSizeOptions) {
      const optPx = parseFloat(opt)
      const diff = Math.abs(px - optPx)
      if (diff < minDiff) {
        minDiff = diff
        closest = opt
      }
    }
    return closest
  }

  function findSizeFromNode(start: Node | null): string {
    let n = start
    let firstElement: HTMLElement | null = null
    while (n && n !== editorEl) {
      if (n.nodeType === Node.ELEMENT_NODE) {
        const el = n as HTMLElement
        if (!firstElement) firstElement = el
        const fs = el.style?.fontSize
        if (fs) {
          const norm = normalizeSize(fs)
          if (norm) return norm
        }
      }
      n = n.parentNode
    }
    // Fallback: use computed style from the nearest element
    if (firstElement) {
      const computed = getComputedStyle(firstElement).fontSize
      const norm = normalizeSize(computed)
      if (norm) return norm
    }
    return ''
  }

  const anchorSize = findSizeFromNode(sel.anchorNode)
  if (sel.isCollapsed || sel.anchorNode === sel.focusNode) {
    selectedFontSize.value = anchorSize
    return
  }
  const focusSize = findSizeFromNode(sel.focusNode)
  selectedFontSize.value = (anchorSize && focusSize && anchorSize === focusSize) ? anchorSize : ''
}

function toggleFontColorPicker() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.saveEditorSelection) {
    editor.saveEditorSelection()
  }
  if (showFontColorPicker.value) {
    showFontColorPicker.value = false
  } else {
    if (editor && editor.restoreEditorSelection) {
      editor.restoreEditorSelection()
    }
    getSelectionFontSize()
    if (editor && editor.saveEditorSelection) {
      editor.saveEditorSelection()
    }
    closeAllToolbarMenus()
    showFontColorPicker.value = true
  }
}

function applyFontColorToSelection(color: string) {
  showFontColorPicker.value = false
  const editor = wysiwygEditorRef.value
  if (editor) {
    if (editor.restoreEditorSelection) {
      editor.restoreEditorSelection()
    }
    if (editor.saveEditorSelection) {
      editor.saveEditorSelection()
    }
    if (editor.applyFontColor) {
      editor.applyFontColor(color)
    }
  }
}

function applyFontSize() {
  const size = selectedFontSize.value
  const editor = wysiwygEditorRef.value
  if (!editor) return
  if (editor.restoreEditorSelection) {
    editor.restoreEditorSelection()
  }
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0) return
  const range = selection.getRangeAt(0)
  if (range.collapsed) return
  const contents = range.extractContents()
  const span = document.createElement('span')
  if (size) {
    span.style.fontSize = size
  }
  span.appendChild(contents)
  range.insertNode(span)
  if (size) {
    const allInner = span.querySelectorAll('*') as NodeListOf<HTMLElement>
    allInner.forEach(el => { el.style.fontSize = '' })
  }
  selection.removeAllRanges()
  showFontColorPicker.value = false
  const editorEl = editor.editorRef
  if (editorEl) {
    editorEl.dispatchEvent(new Event('input', { bubbles: true }))
  }
}

function applySuperscript() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.applySuperscript) {
    editor.applySuperscript()
  } else {
    execCommand('superscript')
  }
}

function applySubscript() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.applySubscript) {
    editor.applySubscript()
  } else {
    execCommand('subscript')
  }
}

function computeTocItems() {
  const editorEl = wysiwygEditorRef.value?.editorRef
  if (!editorEl) {
    tocItems.value = []
    return
  }
  const headings = editorEl.querySelectorAll('h1, h2, h3, h4, h5, h6')
  const items: TocItem[] = Array.from(headings).map((h, i) => {
    const tag = h.tagName.toLowerCase()
    const level = parseInt(tag.replace('h', ''), 10)
    return { id: h.id || '', text: h.textContent || '', level, index: i, hasChildren: false }
  })
  for (let i = 0; i < items.length; i++) {
    const next = items[i + 1]
    items[i].hasChildren = next ? next.level > items[i].level : false
  }
  tocItems.value = items
}

function toggleTocCollapse(index: number) {
  const s = new Set(collapsedToc.value)
  if (s.has(index)) s.delete(index); else s.add(index)
  collapsedToc.value = s
}

function scrollToHeading(id: string) {
  const editorEl = wysiwygEditorRef.value?.editorRef
  if (!editorEl) return
  const target = editorEl.querySelector(`#${CSS.escape(id)}`) as HTMLElement | null
  if (target) {
    const containerRect = editorEl.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    const scrollMarginTop = Number.parseFloat(getComputedStyle(target).scrollMarginTop || '0') || 0
    const nextTop = editorEl.scrollTop + (targetRect.top - containerRect.top) - scrollMarginTop
    editorEl.scrollTo({ top: Math.max(0, nextTop), behavior: 'smooth' })
  }
}

// Watch content to update TOC (debounced)
let tocDebounceTimer: ReturnType<typeof setTimeout> | null = null
watch([noteContent, () => wysiwygEditorRef.value?.editorRef], () => {
  if (tocDebounceTimer) clearTimeout(tocDebounceTimer)
  tocDebounceTimer = setTimeout(() => computeTocItems(), 300)
})

// Update TOC when toc panel is opened
watch(showToc, (val) => {
  if (val) nextTick(() => computeTocItems())
})

function handleMermaidEdit(source: string, id: string) {
  mermaidEditSource.value = source
  mermaidEditId.value = id
  showMermaidEditDialog.value = true
}

function handleMermaidZoom(source: string, id: string, svg: string) {
  mermaidZoomSource.value = source
  mermaidZoomId.value = id
  mermaidZoomScale.value = 100
  
  // Parse SVG to get viewBox dimensions
  const parser = new DOMParser()
  const doc = parser.parseFromString(svg, 'image/svg+xml')
  const svgEl = doc.querySelector('svg')
  let vbWidth = 400
  let vbHeight = 300
  
  if (svgEl) {
    const vb = svgEl.getAttribute('viewBox')
    if (vb) {
      mermaidSvgViewBox.value = vb
      const parts = vb.split(/\s+/).map(Number)
      vbWidth = parts[2] || 400
      vbHeight = parts[3] || 300
    }
  }
  
  // Calculate scale to normalize all charts to REFERENCE_SIZE
  const maxDim = Math.max(vbWidth, vbHeight)
  const baseScale = REFERENCE_SIZE / maxDim
  
  // Set SVG dimensions at base scale (100% zoom)
  mermaidSvgWidth.value = vbWidth * baseScale
  mermaidSvgHeight.value = vbHeight * baseScale
  
  // Prepare SVG: set explicit dimensions and normalize font
  let processedSvg = svg
  // Remove width="100%" and set explicit width/height
  processedSvg = processedSvg.replace(/width="100%"/, `width="${vbWidth}" height="${vbHeight}"`)
  // Remove max-width constraint
  processedSvg = processedSvg.replace(/max-width:\s*[^;"]+;?/g, '')
  // Normalize font-size for consistency
  processedSvg = processedSvg.replace(/font-size:\s*\d+(\.\d+)?px/g, 'font-size: 14px')
  
  showMermaidZoomDialog.value = true
  
  nextTick(() => {
    const container = mermaidZoomSvgRef.value
    if (container) {
      // Insert SVG with base scale applied as width/height
      container.innerHTML = processedSvg
      const innerSvg = container.querySelector('svg')
      if (innerSvg) {
        innerSvg.style.display = 'block'
        innerSvg.style.maxWidth = 'none'
        // Set explicit width/height to the scaled size
        innerSvg.setAttribute('width', `${vbWidth * baseScale}`)
        innerSvg.setAttribute('height', `${vbHeight * baseScale}`)
        innerSvg.setAttribute('viewBox', mermaidSvgViewBox.value)
      }
      setupMermaidInlineEditing()
      
      // Scroll to center
      const scrollContainer = mermaidZoomContentRef.value
      if (scrollContainer) {
        scrollContainer.scrollLeft = (scrollContainer.scrollWidth - scrollContainer.clientWidth) / 2
        scrollContainer.scrollTop = (scrollContainer.scrollHeight - scrollContainer.clientHeight) / 2
      }
    }
  })
}

function setupMermaidInlineEditing() {
  const svgContainer = mermaidZoomSvgRef.value
  if (!svgContainer) return
  
  const textElements = svgContainer.querySelectorAll('.nodeLabel, .edgeLabel, foreignObject span, foreignObject p')
  textElements.forEach((textEl) => {
    const el = textEl as HTMLElement
    if (el.textContent && el.textContent.trim()) {
      el.contentEditable = 'true'
      el.style.cursor = 'text'
      el.style.outline = 'none'
      el.style.minWidth = '20px'
      el.addEventListener('focus', () => {
        el.style.backgroundColor = 'rgba(100, 149, 237, 0.1)'
        el.style.borderRadius = '2px'
      })
      el.addEventListener('blur', () => {
        el.style.backgroundColor = ''
        handleMermaidTextEdit(el)
      })
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault()
          el.blur()
        }
      })
    }
  })
}

function handleMermaidTextEdit(textEl: HTMLElement) {
  const newText = textEl.textContent || ''
  const originalText = textEl.getAttribute('data-original-text') || ''
  
  if (!originalText) {
    textEl.setAttribute('data-original-text', newText)
    return
  }
  
  if (originalText !== newText && mermaidZoomSource.value) {
    // Update source code
    const escapedOld = originalText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    mermaidZoomSource.value = mermaidZoomSource.value.replace(new RegExp(escapedOld, 'g'), newText)
    textEl.setAttribute('data-original-text', newText)
    
    // Re-render in background
    const container = wysiwygEditorRef.value?.editorRef || previewRef.value
    if (container) {
      const block = container.querySelector(`[data-mermaid-id="${mermaidZoomId.value}"]`)
      if (block) {
        block.setAttribute('data-mermaid-source', mermaidZoomSource.value)
        block.classList.remove('mermaid-rendered')
        block.innerHTML = ''
        nextTick(() => renderSingleMermaidBlock(block as HTMLElement))
      }
    }
  }
}

function saveMermaidEdit() {
  const container = wysiwygEditorRef.value?.editorRef || previewRef.value
  if (!container) return
  
  const block = container.querySelector(`[data-mermaid-id="${mermaidEditId.value}"]`)
  if (block) {
    block.setAttribute('data-mermaid-source', mermaidEditSource.value)
    block.classList.remove('mermaid-rendered')
    block.innerHTML = ''
    nextTick(() => renderSingleMermaidBlock(block as HTMLElement))
  }
  
  showMermaidEditDialog.value = false
}

function closeMermaidEditDialog() {
  showMermaidEditDialog.value = false
}

function closeMermaidZoomDialog() {
  showMermaidZoomDialog.value = false
}

function editMermaidFromZoom() {
  mermaidEditSource.value = mermaidZoomSource.value
  mermaidEditId.value = mermaidZoomId.value
  showMermaidZoomDialog.value = false
  showMermaidEditDialog.value = true
}

function handleMathEdit(payload: { tex: string; displayMode: boolean; element: HTMLElement }) {
  mathEditTex.value = payload.tex
  mathEditDisplayMode.value = payload.displayMode
  mathEditElement.value = payload.element
  showMathEditDialog.value = true
}

const mathPreviewHtml = computed(() => {
  if (!mathEditTex.value) return ''
  try {
    return katexModule.renderToString(mathEditTex.value, {
      displayMode: mathEditDisplayMode.value,
      throwOnError: false,
      output: 'html',
      strict: 'ignore',
    })
  } catch {
    return `<span style="color: var(--color-text-light)">${mathEditTex.value}</span>`
  }
})

function saveMathEdit() {
  const el = mathEditElement.value
  if (!el) {
    showMathEditDialog.value = false
    return
  }
  const newTex = mathEditTex.value
  const displayMode = mathEditDisplayMode.value
  const escaped = newTex.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
  const wrapper = document.createElement('div')
  const tag = displayMode ? 'div' : 'span'
  wrapper.innerHTML = `<${tag} class="math-editable" data-tex="${escaped}" data-display-mode="${displayMode}"></${tag}>`
  const newEl = wrapper.firstElementChild as HTMLElement
  if (newEl) {
    try {
      const innerHtml = katexModule.renderToString(newTex, {
        displayMode,
        throwOnError: false,
        output: 'html',
        strict: 'ignore',
      })
      const innerTag = displayMode ? 'div' : 'span'
      newEl.innerHTML = `<${innerTag} class="math-rendered-content">${innerHtml}</${innerTag}><span class="math-controls"><button class="math-edit-btn" title="编辑公式"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button></span>`
    } catch { /* ignore */ }
    el.replaceWith(newEl)
    nextTick(() => {
      const container = wysiwygEditorRef.value?.editorRef || previewRef.value
      if (container) {
        attachMathEditListeners(container)
      }
    })
  }
  showMathEditDialog.value = false
}

function closeMathEditDialog() {
  showMathEditDialog.value = false
}

function onDocumentClick(e: MouseEvent) {
  if (!showHeadingMenu.value && !showFontColorPicker.value && !showHighlightColorPicker.value && !showTablePicker.value) return
  const target = e.target as HTMLElement
  // Don't close menus when clicking TOC button (TOC is not mutually exclusive)
  if (target.closest('button[title="目录"]')) return
  if (showHeadingMenu.value) {
    if (target.closest('.heading-dropdown-teleport') || target.closest('.heading-dropdown-wrap')) return
    showHeadingMenu.value = false
  }
  if (showFontColorPicker.value) {
    if (target.closest('.font-color-picker-teleport') || target.closest('.color-picker-wrap')) return
    showFontColorPicker.value = false
  }
  if (showHighlightColorPicker.value) {
    if (target.closest('.font-color-picker-teleport') || target.closest('.color-picker-wrap')) return
    showHighlightColorPicker.value = false
  }
  if (showTablePicker.value) {
    if (target.closest('.table-picker-teleport') || target.closest('.table-picker-wrap')) return
    showTablePicker.value = false
  }
}

function onSelectionChange() {
  updateHeadingButtonState()
}

function onGlobalKeydown(e: KeyboardEvent) {
  // Global keyboard shortcuts
}

function onMermaidEdit(e: Event) {
  const ce = e as CustomEvent
  if (ce.detail?.handled) return
  const target = e.target as HTMLElement
  const container = wysiwygEditorRef.value?.editorRef || previewRef.value
  if (!target || !container || !container.contains(target)) return
  ce.detail.handled = true
  handleMermaidEdit(ce.detail.source, ce.detail.id)
}

function onMermaidZoom(e: Event) {
  const ce = e as CustomEvent
  if (ce.detail?.handled) return
  const target = e.target as HTMLElement
  const container = wysiwygEditorRef.value?.editorRef || previewRef.value
  if (!target || !container || !container.contains(target)) return
  ce.detail.handled = true
  handleMermaidZoom(ce.detail.source, ce.detail.id, ce.detail.svg)
}

function onMathEdit(e: Event) {
  const ce = e as CustomEvent
  if (ce.detail?.handled) return
  const target = e.target as HTMLElement
  const container = wysiwygEditorRef.value?.editorRef || previewRef.value
  if (!target || !container || !container.contains(target)) return
  ce.detail.handled = true
  handleMathEdit({ tex: ce.detail.tex, displayMode: ce.detail.displayMode, element: ce.detail.element })
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onGlobalKeydown)
  document.addEventListener('selectionchange', onSelectionChange)
  document.addEventListener('mermaid-edit', onMermaidEdit)
  document.addEventListener('mermaid-zoom', onMermaidZoom)
  document.addEventListener('math-edit', onMathEdit)
  autoSaveTimer = setInterval(async () => {
    if (hasChanges.value && noteId.value && !saving.value) {
      saving.value = true
      try {
        await wysiwygEditorRef.value?.flushPendingImageUploads?.()
        await wysiwygEditorRef.value?.flushPendingSerialization?.()
        await notesStore.updateNote(noteId.value, {
          title: noteTitle.value || null,
          content: noteContent.value
        })
        hasChanges.value = false
        clearDraft()
        showToast('已自动保存', 'success')
      } catch (e) {
        console.error('Auto-save failed:', e)
      } finally {
        saving.value = false
      }
    }
  }, AUTO_SAVE_INTERVAL)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onGlobalKeydown)
  document.removeEventListener('selectionchange', onSelectionChange)
  document.removeEventListener('mermaid-edit', onMermaidEdit)
  document.removeEventListener('mermaid-zoom', onMermaidZoom)
  document.removeEventListener('math-edit', onMathEdit)
  if (autoSaveTimer) {
    clearInterval(autoSaveTimer)
    autoSaveTimer = null
  }
})

async function startVoiceNote() {
  if (isRecording.value) {
    await stopRecording()
    return
  }

  asrInsertPos = noteContent.value.length
  asrPreviousLen = 0
  asrNeedNewline = asrInsertPos > 0 && noteContent.value.length > 0 && !noteContent.value.endsWith('\n')

  try {
    await asrStreaming.start({ chunk_size_sec: 0.5 })
  } catch (err) {
    console.error('Failed to start recording:', err)
    if (!asrStreaming.error.value) {
      showToast(err instanceof Error ? err.message : '录音启动失败', 'error')
    }
  }
}

async function stopRecording() {
  try {
    const result = await asrStreaming.stop()
    if (asrPreviousLen > 0) {
      const finalText = result?.text?.trim()
      if (finalText) {
        const prefix = asrNeedNewline ? '\n' : ''
        const fullText = prefix + finalText
        const before = noteContent.value.substring(0, asrInsertPos)
        const after = noteContent.value.substring(asrInsertPos + asrPreviousLen)
        noteContent.value = before + fullText + after
        asrPreviousLen = fullText.length
      }
      hasChanges.value = true
      asrPreviousLen = 0
      return
    }
    const transcript = result?.text?.trim()
    if (!transcript) {
      return
    }

    const cursorPos = noteContent.value.length

    if (noteContent.value && !noteContent.value.endsWith('\n') && cursorPos > 0) {
      noteContent.value = noteContent.value.substring(0, cursorPos) + '\n' + transcript + noteContent.value.substring(cursorPos)
    } else {
      noteContent.value = noteContent.value.substring(0, cursorPos) + transcript + noteContent.value.substring(cursorPos)
    }
    hasChanges.value = true
  } catch (error) {
    asrPreviousLen = 0
    console.error('ASR failed:', error)
    const msg = error instanceof Error ? error.message : '语音识别失败'
    showToast(msg, 'error')
  }
}
</script>

<style scoped>
.note-editor-page {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background-color: var(--color-bg);
}

.editor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 16px;
  background-color: var(--color-white);
  border-bottom: 1px solid var(--color-border);
}

.back-btn {
  padding: 8px;
  color: var(--color-text);
  border-radius: var(--radius-sm);
}

.header-actions {
  display: flex;
  gap: 8px;
}

.save-btn {
  padding: 6px 16px;
  background-color: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
}

.save-btn:disabled {
  opacity: 0.5;
}

.delete-btn {
  padding: 8px;
  color: var(--color-error);
  border-radius: var(--radius-sm);
}

.export-btn {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
}

.export-btn:hover {
  color: var(--color-primary);
}

.export-format-overlay {
  position: fixed;
  inset: 0;
  z-index: 998;
  background: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
}

.export-format-dialog {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  padding: 24px;
  min-width: 280px;
  box-shadow: 0 8px 32px rgba(90, 130, 60, 0.15);
}

.export-format-dialog h3 {
  margin: 0 0 16px;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.export-format-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.export-format-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--color-hover);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: 14px;
  cursor: pointer;
  transition: all 0.15s;
}

.export-format-btn:hover {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.export-cancel-btn {
  width: 100%;
  margin-top: 12px;
  padding: 10px;
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text-light);
  font-size: 14px;
  cursor: pointer;
}

.export-cancel-btn:hover {
  background: var(--color-hover);
}

.title-input {
  flex: 1;
  min-width: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  border: none;
  outline: none;
  background: transparent;
}

.title-input::placeholder {
  color: var(--color-text-light);
}

.editor-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 12px;
  background-color: var(--color-white);
  border-bottom: 1px solid var(--color-border);
  flex-wrap: nowrap;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  -ms-overflow-style: none;
}

.editor-toolbar::-webkit-scrollbar {
  display: none;
}

.toolbar-btn {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}

.toolbar-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.toolbar-btn.active {
  background-color: var(--color-primary);
  color: white;
}

.toolbar-btn:disabled {
  opacity: 0.5;
}

.heading-dropdown-wrap {
  flex-shrink: 0;
}

.heading-dropdown-teleport {
  background: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  box-shadow: 0 4px 12px rgba(90, 130, 60, 0.12);
  min-width: 48px;
  padding: 4px 0;
}

.heading-dropdown-item {
  display: block;
  width: 100%;
  padding: 6px 12px;
  text-align: left;
  color: var(--color-text);
  font-size: 13px;
  white-space: nowrap;
}

.heading-dropdown-item .heading-icon {
  display: inline-block;
  font-family: var(--font-mono, monospace);
  font-size: 12px;
}

.heading-dropdown-item:hover {
  background-color: var(--color-hover);
  color: var(--color-primary);
}

.heading-dropdown-item.heading-active {
  background-color: var(--color-primary);
  color: white;
}

.heading-dropdown-item.heading-active:hover {
  background-color: var(--color-primary-dark);
  color: white;
}

.align-dropdown-wrap {
  flex-shrink: 0;
}

.align-dropdown-teleport {
  min-width: 120px;
}

.align-dropdown-item {
  display: flex !important;
  align-items: center;
  gap: 8px;
}

.align-dropdown-item svg {
  flex-shrink: 0;
}

.align-dropdown-divider {
  height: 1px;
  background: var(--color-border);
  margin: 4px 0;
}

.recording-icon {
  animation: pulse 1s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.toolbar-divider {
  width: 1px;
  height: 20px;
  background-color: var(--color-border);
  margin: 0 4px;
  flex-shrink: 0;
}

.table-picker-wrap {
  flex-shrink: 0;
}

.color-picker-wrap {
  flex-shrink: 0;
}

.table-picker-teleport {
  background: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  box-shadow: 0 4px 12px rgba(90, 130, 60, 0.12);
  padding: 8px;
}

.table-picker-grid {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.table-picker-row {
  display: flex;
  gap: 2px;
}

.table-picker-cell {
  width: 20px;
  height: 20px;
  border: 1px solid var(--color-border);
  border-radius: 2px;
  background: var(--color-white);
  cursor: pointer;
  padding: 0;
}

.table-picker-cell.active {
  background-color: var(--color-primary);
  border-color: var(--color-primary);
  opacity: 0.7;
}

.table-picker-label {
  text-align: center;
  font-size: 12px;
  color: var(--color-text-light);
  margin-top: 6px;
}

.find-replace-bar {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 12px;
  background-color: var(--color-white);
  border-bottom: 1px solid var(--color-border);
}

.find-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.find-input {
  flex: 1;
  min-width: 80px;
  padding: 4px 8px;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  background-color: var(--color-bg);
  outline: none;
}

.find-input:focus {
  border-color: var(--color-primary);
}

.find-input::placeholder {
  color: var(--color-text-light);
}

.find-count {
  font-size: 12px;
  color: var(--color-text-light);
  white-space: nowrap;
  flex-shrink: 0;
}

.find-nav-btn {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}

.find-nav-btn:hover:not(:disabled) {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.find-nav-btn:disabled {
  opacity: 0.4;
}

.find-close-btn {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}

.find-close-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.find-action-btn {
  padding: 4px 10px;
  font-size: 12px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
  white-space: nowrap;
}

.find-action-btn:hover:not(:disabled) {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.find-action-btn:disabled {
  opacity: 0.4;
}

.editor-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  position: relative;
  min-height: 0;
  overflow: hidden;
}

.wysiwyg-editor-wrapper {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.textarea-wrap {
  flex: 1;
  position: relative;
  min-height: 0;
  overflow: hidden;
}

.highlight-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  padding: 24px;
  font-size: 15px;
  line-height: 1.6;
  font-family: inherit;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow: hidden;
  pointer-events: none;
  z-index: 1;
  color: transparent;
}

.highlight-overlay :deep(mark) {
  background-color: rgba(255, 200, 0, 0.4);
  color: transparent;
  border-radius: 2px;
}

.highlight-overlay :deep(mark.current) {
  background-color: rgba(255, 140, 0, 0.7);
}

.content-textarea {
  position: relative;
  z-index: 2;
  width: 100%;
  height: 100%;
  padding: 24px;
  font-size: 15px;
  line-height: 1.6;
  color: var(--color-text);
  background-color: var(--color-bg);
  border: none;
  outline: none;
  resize: none;
}

.content-textarea.find-active {
  background-color: transparent;
}

.content-textarea::placeholder {
  color: var(--color-text-light);
}

.preview-content {
  flex: 1;
  padding: 24px;
  font-size: 15px;
  line-height: 1.6;
  color: var(--color-text);
  background-color: var(--color-bg);
  overflow-y: auto;
  overscroll-behavior: contain;
}

.preview-content :deep(code) {
  font-family: var(--font-mono);
  background-color: var(--color-code-bg);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}

.preview-content :deep(pre) {
  background-color: var(--color-code-bg);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--color-border);
}

.preview-content :deep(.code-block pre) {
  background: transparent;
  border: 0;
  border-radius: 0;
  margin: 0;
  padding: 12px 16px;
}

.preview-content :deep(pre code) {
  background: none;
  padding: 0;
}

.preview-content :deep(h1),
.preview-content :deep(h2),
.preview-content :deep(h3),
.preview-content :deep(h4),
.preview-content :deep(h5),
.preview-content :deep(h6) {
  margin: 16px 0 8px 0;
  font-weight: 600;
  line-height: 1.4;
  color: var(--color-text);
  cursor: pointer;
  scroll-margin-top: 16px;
}
.preview-content :deep(h1:hover),
.preview-content :deep(h2:hover),
.preview-content :deep(h3:hover),
.preview-content :deep(h4:hover),
.preview-content :deep(h5:hover),
.preview-content :deep(h6:hover) {
  text-decoration: underline;
}

.preview-content :deep(h1) { font-size: 1.5em; border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
.preview-content :deep(h2) { font-size: 1.3em; border-bottom: 1px solid var(--color-border); padding-bottom: 6px; }
.preview-content :deep(h3) { font-size: 1.15em; }

.preview-content :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
  font-size: 13px;
  display: block;
  overflow-x: auto;
}

.preview-content :deep(thead),
.preview-content :deep(tbody) {
  display: table;
  width: 100%;
  table-layout: fixed;
}

.preview-content :deep(th),
.preview-content :deep(td) {
  border: 1px solid var(--color-border);
  padding: 8px 12px;
  text-align: left;
  word-break: normal;
  overflow-wrap: break-word;
}

.preview-content :deep(th) {
  background-color: var(--color-bg);
  font-weight: 600;
}

.preview-content :deep(ul),
.preview-content :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.preview-content :deep(li) {
  margin: 4px 0;
  line-height: 1.6;
}

.preview-content :deep(ul) { list-style-type: disc; }
.preview-content :deep(ol) {
  list-style-type: none;
  counter-reset: ol-counter;
}

.preview-content :deep(ol > li) {
  counter-increment: ol-counter;
}

.preview-content :deep(ol > li::before) {
  content: counters(ol-counter, ".") ". ";
  margin-right: 2px;
}

.preview-content :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 16px;
  border-left: 4px solid var(--color-primary);
  background-color: var(--color-sidebar);
  color: var(--color-text-light);
  font-style: italic;
}

.preview-content :deep(p) {
  margin: 8px 0;
}

.preview-content :deep(p:first-child) { margin-top: 0; }
.preview-content :deep(p:last-child) { margin-bottom: 0; }

.preview-content :deep(a) {
  color: var(--color-primary-dark);
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-dark);
  cursor: pointer;
}

.preview-content :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 16px 0;
}

.preview-content :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-md);
}

.preview-content :deep(.hljs) { background: transparent; }
.preview-content :deep(.hljs-keyword),
.preview-content :deep(.hljs-built_in),
.preview-content :deep(.hljs-name),
.preview-content :deep(.hljs-tag) { color: var(--code-keyword, #d73a49); }
.preview-content :deep(.hljs-string),
.preview-content :deep(.hljs-title),
.preview-content :deep(.hljs-section),
.preview-content :deep(.hljs-attribute),
.preview-content :deep(.hljs-type) { color: var(--code-string, #032f62); }
.preview-content :deep(.hljs-comment),
.preview-content :deep(.hljs-quote) { color: var(--code-comment, #6a737d); font-style: italic; }
.preview-content :deep(.hljs-number) { color: var(--code-number, #005cc5); }
.preview-content :deep(.hljs-function) { color: var(--code-function, #6f42c1); }
.preview-content :deep(.hljs-variable),
.preview-content :deep(.hljs-params) { color: var(--code-variable, #e36209); }

.preview-content :deep(.mermaid-block) {
  display: flex;
  justify-content: center;
  margin: 12px 0;
  overflow-x: auto;
}

.preview-content :deep(.mermaid-block svg) {
  max-width: 100%;
  height: auto;
}

.preview-content :deep(.mermaid-error) {
  background-color: color-mix(in srgb, var(--color-danger, #e53e3e) 10%, transparent);
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
}

.preview-content :deep(.echarts-block) {
  margin: 12px 0;
  overflow-x: auto;
}

.preview-content :deep(.echarts-block svg) {
  max-width: 100%;
  height: auto;
  display: block;
}

.preview-content :deep(.echarts-error) {
  background-color: color-mix(in srgb, var(--color-danger, #e53e3e) 10%, transparent);
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
  overflow-x: auto;
}

.voice-hint {
  position: absolute;
  bottom: 16px;
  left: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background-color: var(--color-error);
  color: white;
  border-radius: var(--radius-md);
  font-size: 13px;
}

.recording-indicator {
  width: 8px;
  height: 8px;
  background-color: white;
  border-radius: 50%;
  animation: blink 1s infinite;
}

@keyframes blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0.3; }
}

.raw-transcription {
  padding: 12px 16px;
  background-color: var(--color-sidebar);
  border-top: 1px solid var(--color-border);
}

.transcription-label {
  font-size: 11px;
  color: var(--color-text-light);
  margin-bottom: 4px;
}

.transcription-text {
  font-size: 13px;
  color: var(--color-text-light);
  white-space: pre-wrap;
}

@media (max-width: 767px) {
  .editor-header {
    padding: 10px 12px;
  }

  .editor-toolbar {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    flex-wrap: nowrap;
  }

  .editor-toolbar::-webkit-scrollbar {
    display: none;
  }

  .title-input {
    font-size: 15px;
  }

  .textarea-wrap {
    flex: 1;
  }

  .content-textarea {
    padding: 12px;
    font-size: 16px;
  }

  .highlight-overlay {
    padding: 12px;
    font-size: 16px;
  }

  .find-replace-bar {
    padding: 4px 8px;
  }

  .find-input {
    min-width: 60px;
  }
}

.preview-content :deep(.mermaid-block) {
  position: relative;
  display: flex;
  justify-content: center;
  margin: 12px 0;
  overflow-x: auto;
}

.preview-content :deep(.mermaid-rendered-content) {
  display: flex;
  justify-content: center;
}

.preview-content :deep(.mermaid-rendered-content svg) {
  max-width: 100%;
  height: auto;
}

.preview-content :deep(.mermaid-controls) {
  position: absolute;
  top: 8px;
  right: 8px;
  display: flex;
  gap: 6px;
  opacity: 0.6;
  transition: opacity 0.2s;
  z-index: 10;
}

.preview-content :deep(.mermaid-block:hover .mermaid-controls) {
  opacity: 1;
}

.preview-content :deep(.mermaid-edit-btn),
.preview-content :deep(.mermaid-zoom-btn) {
  padding: 8px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 2px 8px rgba(90, 130, 60, 0.15);
}

.preview-content :deep(.mermaid-edit-btn:hover),
.preview-content :deep(.mermaid-zoom-btn:hover) {
  background-color: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
  box-shadow: 0 4px 12px rgba(90, 130, 60, 0.2);
  transform: scale(1.05);
}

.preview-content :deep(.math-editable) {
  position: relative;
  border-radius: var(--radius-sm);
  transition: background-color 0.2s;
}

.preview-content :deep(div.math-editable) {
  display: block;
  text-align: center;
  margin: 8px 0;
}

.preview-content :deep(span.math-editable) {
  display: inline;
}

.preview-content :deep(.math-editable:hover) {
  background-color: var(--color-hover);
}

.preview-content :deep(.math-rendered-content) {
  display: inline;
}

.preview-content :deep(.math-controls) {
  position: absolute;
  top: -10px;
  right: -4px;
  display: inline-flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
  z-index: 10;
}

.preview-content :deep(.math-editable:hover .math-controls) {
  opacity: 1;
}

.preview-content :deep(.math-edit-btn) {
  padding: 6px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 2px 8px rgba(90, 130, 60, 0.15);
  line-height: 0;
}

.preview-content :deep(.math-edit-btn:hover) {
  background-color: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
  box-shadow: 0 4px 12px rgba(90, 130, 60, 0.2);
  transform: scale(1.05);
}

.mermaid-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 998;
  background: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
}

.mermaid-dialog {
  background: var(--surface-panel-strong);
  border-radius: var(--radius-lg);
  width: 90%;
  max-width: 600px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--panel-shadow);
}

.mermaid-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);
}

.mermaid-dialog-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.mermaid-dialog-close {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
}

.mermaid-dialog-close:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.mermaid-dialog-content {
  flex: 1;
  padding: 16px 20px;
  overflow-y: auto;
}

.mermaid-edit-textarea {
  width: 100%;
  min-height: 200px;
  padding: 12px;
  font-family: var(--font-mono);
  font-size: 14px;
  line-height: 1.5;
  color: var(--color-text);
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  resize: vertical;
}

.mermaid-edit-textarea:focus {
  outline: none;
  border-color: var(--color-primary);
}

.mermaid-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border);
}

.mermaid-dialog-cancel {
  padding: 8px 16px;
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-light);
  font-size: 14px;
  cursor: pointer;
}

.mermaid-dialog-cancel:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.mermaid-dialog-save {
  padding: 8px 16px;
  background-color: var(--color-primary);
  border: none;
  border-radius: var(--radius-sm);
  color: white;
  font-size: 14px;
  cursor: pointer;
}

.mermaid-dialog-save:hover {
  background-color: var(--color-primary-dark);
}

.mermaid-zoom-overlay {
  position: fixed;
  inset: 0;
  z-index: 998;
  background: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
}

.mermaid-zoom-card {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  max-width: 90vw;
  width: 900px;
  height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(90, 130, 60, 0.2);
  overflow: hidden;
}

.mermaid-zoom-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);
}

.mermaid-zoom-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.mermaid-zoom-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1;
  margin: 0 20px;
}

.zoom-label {
  font-size: 13px;
  color: var(--color-text-light);
  white-space: nowrap;
}

.zoom-slider {
  flex: 1;
  max-width: 200px;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--color-border);
  border-radius: 2px;
  outline: none;
}

.zoom-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  border: 2px solid white;
  box-shadow: 0 1px 3px rgba(90, 130, 60, 0.2);
}

.zoom-slider::-moz-range-thumb {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  border: 2px solid white;
  box-shadow: 0 1px 3px rgba(90, 130, 60, 0.2);
}

.zoom-value {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  min-width: 40px;
  text-align: center;
}

.zoom-reset-btn {
  padding: 6px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}

.zoom-reset-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.mermaid-zoom-close {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
}

.mermaid-zoom-close:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.mermaid-zoom-content {
  flex: 1;
  padding: 20px;
  overflow: auto;
}

.mermaid-zoom-spacer {
  margin: auto;
  pointer-events: none;
  position: relative;
}

.mermaid-zoom-svg {
  pointer-events: none;
}

.mermaid-zoom-svg :deep(svg) {
  display: block;
}

.math-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 998;
  background: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
}

.math-dialog {
  background: var(--surface-panel-strong);
  border-radius: var(--radius-lg);
  width: 90%;
  max-width: 600px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--panel-shadow);
}

.math-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);
}

.math-dialog-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.math-dialog-close {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
}

.math-dialog-close:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.math-dialog-content {
  flex: 1;
  padding: 16px 20px;
  overflow-y: auto;
}

.math-edit-textarea {
  width: 100%;
  min-height: 120px;
  padding: 12px;
  font-family: var(--font-mono);
  font-size: 14px;
  line-height: 1.5;
  color: var(--color-text);
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  resize: vertical;
}

.math-edit-textarea:focus {
  outline: none;
  border-color: var(--color-primary);
}

.math-preview-label {
  font-size: 13px;
  color: var(--color-text-light);
  margin: 12px 0 8px;
}

.math-preview {
  padding: 12px;
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  min-height: 40px;
  overflow-x: auto;
}

.math-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border);
}

.math-dialog-cancel {
  padding: 8px 16px;
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-light);
  font-size: 14px;
  cursor: pointer;
}

.math-dialog-cancel:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.math-dialog-save {
  padding: 8px 16px;
  background-color: var(--color-primary);
  border: none;
  border-radius: var(--radius-sm);
  color: white;
  font-size: 14px;
  cursor: pointer;
}

.math-dialog-save:hover {
  background-color: var(--color-primary-dark);
}

.toc-panel {
  position: absolute;
  top: 0;
  right: 0;
  width: 240px;
  max-height: 100%;
  background: var(--color-white);
  border-left: 1px solid var(--color-border);
  box-shadow: -2px 0 8px rgba(90, 130, 60, 0.05);
  z-index: 100;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.toc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.toc-close-btn {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
}

.toc-close-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.toc-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}

.toc-item {
  padding: 6px 14px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
  white-space: normal;
  overflow-wrap: break-word;
  word-break: break-word;
  transition: all var(--transition-fast);
  display: flex;
  align-items: flex-start;
  gap: 4px;
  line-height: 1.4;
}

.toc-item:hover {
  background-color: var(--color-hover);
  color: var(--color-primary);
}

.toc-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 16px;
  height: 18px;
  cursor: pointer;
  border-radius: 3px;
  margin-top: 1px;
}

.toc-toggle:hover {
  background-color: var(--color-hover);
}

.toc-toggle svg {
  transition: transform 0.2s ease;
}

.toc-toggle-collapsed {
  transform: rotate(-90deg);
}

.toc-toggle-spacer {
  display: inline-block;
  flex-shrink: 0;
  width: 16px;
}

.toc-empty {
  padding: 20px 14px;
  font-size: 13px;
  color: var(--color-text-light);
  text-align: center;
}

.toolbar-btn.active {
  background-color: var(--color-primary);
  color: white;
}

.font-color-picker-teleport {
  background: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: 0 4px 16px rgba(90, 130, 60, 0.15);
  padding: 10px;
  min-width: 200px;
}

.color-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 4px;
  margin-bottom: 8px;
}

.color-swatch {
  width: 24px;
  height: 24px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: pointer;
  transition: transform 0.15s;
}

.color-swatch:hover {
  transform: scale(1.2);
  border-color: var(--color-primary);
}

.color-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.color-native-input {
  width: 28px;
  height: 28px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  padding: 2px;
}

.color-apply-btn {
  padding: 4px 12px;
  font-size: 12px;
  color: white;
  background-color: var(--color-primary);
  border-radius: var(--radius-sm);
}

.color-apply-btn:hover {
  background-color: var(--color-primary-dark);
}

.color-remove-btn {
  padding: 4px 12px;
  font-size: 12px;
  color: var(--color-text-light);
  background-color: var(--color-hover);
  border-radius: var(--radius-sm);
}

.color-remove-btn:hover {
  background-color: var(--color-border);
  color: var(--color-text);
}

.font-size-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--color-border);
}

.font-size-label {
  font-size: 12px;
  color: var(--color-text-light);
  white-space: nowrap;
}

.font-size-select {
  flex: 1;
  padding: 4px 8px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg);
  color: var(--color-text);
  outline: none;
}

.font-size-select:focus {
  border-color: var(--color-primary);
}

.font-color-btn {
  font-weight: bold;
  font-size: 14px;
  padding: 4px 6px;
}

.font-color-text {
  display: inline-block;
  line-height: 1;
}

.script-btn-text {
  font-size: 13px;
  line-height: 1;
  font-family: var(--font-sans, system-ui, -apple-system, sans-serif);
}

.script-btn-text sup,
.script-btn-text sub {
  font-size: 0.65em;
  line-height: 0;
  vertical-align: baseline;
}

.script-btn-text sup {
  vertical-align: super;
}

.script-btn-text sub {
  vertical-align: sub;
}
</style>
