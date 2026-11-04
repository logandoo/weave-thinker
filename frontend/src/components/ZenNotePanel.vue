<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="zen-note-panel" @pointerover="onTipPointer" @focusin="onTipPointer">
    <div v-if="!currentNoteId" class="zen-note-empty">
      <div class="empty-content">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--color-text-light)" stroke-width="1.5">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/>
          <polyline points="10 9 9 9 8 9"/>
        </svg>
        <p>选择或创建一个笔记</p>
        <button class="create-note-btn" @click="createNewNote">新建笔记</button>
      </div>
    </div>

    <template v-else>
      <div class="zen-note-header">
        <input
          v-model="noteTitle"
          class="zen-title-input"
          placeholder="标题（可选）"
          @input="hasChanges = true"
        />
        <div class="toolbar-right">
          <button v-if="hasChanges" class="save-btn" @click="saveNote" :disabled="saving">
            {{ saving ? '保存中...' : '保存' }}
          </button>
          <div class="header-actions">
            <button class="export-btn" @click="showExportPicker = true" title="导出">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
              </svg>
            </button>
            <button class="delete-btn" @click="confirmDelete" title="删除">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
          </div>
        </div>
      </div>

      <div class="zen-note-toolbar" role="toolbar" aria-label="文本格式" @keydown="onPrimaryToolbarKeydown">
        <div class="toolbar-left">
          <div class="heading-dropdown-wrap">
            <button
              class="toolbar-btn toolbar-primary-btn"
              data-primary-tool="heading"
              :class="{ active: currentHeadingLevel }"
              ref="headingBtnRef"
              @mousedown.prevent
              @click="toggleHeadingMenu"
              aria-haspopup="dialog"
              :aria-expanded="showHeadingMenu"
              tabindex="0"
              :aria-label="currentHeadingLevel ? currentHeadingLevel.toUpperCase() : '正文'" :data-tip="currentHeadingLevel ? currentHeadingLevel.toUpperCase() : '正文'"
            >
              <NoteToolbarIcon name="heading" />
              <span class="toolbar-primary-label">{{ currentHeadingLevel ? currentHeadingLevel.toUpperCase() : '正文' }}</span>
            </button>
          </div>
          <button class="toolbar-btn toolbar-primary-btn" data-primary-tool="bold" tabindex="-1" :class="{ active: isBold }" @mousedown.prevent @click="toggleInlineFormat('**', '**')" data-tip="黑体" aria-label="黑体" :aria-pressed="isBold">
            <NoteToolbarIcon name="bold" /><span class="toolbar-primary-label">黑体</span>
          </button>
          <button class="toolbar-btn toolbar-primary-btn" data-primary-tool="italic" tabindex="-1" :class="{ active: isItalic }" @mousedown.prevent @click="toggleInlineFormat('*', '*')" data-tip="斜体" aria-label="斜体" :aria-pressed="isItalic">
            <NoteToolbarIcon name="italic" /><span class="toolbar-primary-label">斜体</span>
          </button>
          <button ref="charBtnRef" class="toolbar-btn toolbar-primary-btn" data-primary-tool="character" tabindex="-1" @mousedown.prevent @click="toggleCharacterMenu" data-tip="字符" aria-label="字符" aria-haspopup="dialog" :aria-expanded="showCharacterMenu">
            <NoteToolbarIcon name="character" /><span class="toolbar-primary-label">字符</span>
          </button>
          <button ref="paragraphBtnRef" class="toolbar-btn toolbar-primary-btn" data-primary-tool="paragraph" tabindex="-1" @mousedown.prevent @click="toggleParagraphMenu" data-tip="段落" aria-label="段落" aria-haspopup="dialog" :aria-expanded="showParagraphMenu">
            <NoteToolbarIcon name="paragraph" /><span class="toolbar-primary-label">段落</span>
          </button>
          <button ref="toolsBtnRef" class="toolbar-btn toolbar-primary-btn" data-primary-tool="tools" tabindex="-1" @mousedown.prevent @click="toggleToolsMenu" data-tip="编辑" aria-label="编辑" aria-haspopup="dialog" :aria-expanded="showToolsMenu">
            <NoteToolbarIcon name="tools" />
            <span class="toolbar-primary-label">编辑</span>
          </button>
          <button ref="listBtnRef" class="toolbar-btn toolbar-primary-btn" data-primary-tool="list" tabindex="-1" @mousedown.prevent @click="toggleListMenu" data-tip="列表" aria-label="列表" aria-haspopup="dialog" :aria-expanded="showListMenu">
            <NoteToolbarIcon name="list" />
            <span class="toolbar-primary-label">列表</span>
          </button>
          <button ref="insertBtnRef" class="toolbar-btn toolbar-primary-btn" data-primary-tool="insert" tabindex="-1" @mousedown.prevent @click="toggleInsertMenu" data-tip="插入" aria-label="插入" aria-haspopup="dialog" :aria-expanded="showInsertMenu">
            <NoteToolbarIcon name="insert" />
            <span class="toolbar-primary-label">插入</span>
          </button>
          <button ref="tocBtnRef" class="toolbar-btn toolbar-primary-btn" data-primary-tool="toc" tabindex="-1" :class="{ active: showToc }" @mousedown.prevent @click="toggleToc" data-tip="目录" aria-label="目录" aria-controls="zen-note-toc-panel" :aria-expanded="showToc">
            <NoteToolbarIcon name="toc" />
            <span class="toolbar-primary-label">目录</span>
          </button>
          <button class="toolbar-btn toolbar-voice-btn" data-primary-tool="voice" tabindex="-1" :class="{ active: isRecording, recording: isRecording }" @mousedown.prevent @click="startVoiceNote" :disabled="isProcessing" :data-tip="isRecording ? '停止语音输入' : '语音输入'" :aria-label="isRecording ? '停止语音输入' : '语音输入'" :aria-pressed="isRecording">
            <NoteToolbarIcon :name="isRecording ? 'stop' : 'voice'" />
          </button>
          <div class="toolbar-spacer"></div>
          <WordCountButton ref="wordCountRef" :get-text="wordCountSourceText" />
        </div>
      </div>

      <NoteToolbarPanel v-model="showListMenu" menu-id="list" title="列表" :anchor="listBtnRef">
        <button class="note-toolbar-menu-item" @mousedown.prevent @click="toggleLinePrefix('- ')"><NoteToolbarIcon name="list" /><span>项目符号列表</span></button>
        <button class="note-toolbar-menu-item" @mousedown.prevent @click="toggleLinePrefix('1. ')"><NoteToolbarIcon name="orderedList" /><span>编号列表</span></button>
        <button class="note-toolbar-menu-item" @mousedown.prevent="wysiwygEditorRef?.saveEditorSelection()" @click="handleRestartNumbering()"><NoteToolbarIcon name="restartNumbering" /><span>从这里重新编号</span></button>
      </NoteToolbarPanel>

      <NoteToolbarPanel v-model="showInsertMenu" menu-id="insert" title="插入" :anchor="insertBtnRef">
        <div class="note-toolbar-group-label">内容</div>
        <div class="note-toolbar-menu-grid">
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="insertLink"><NoteToolbarIcon name="link" /><span>插入链接</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="openMathInsertDialog"><NoteToolbarIcon name="math" /><span>公式</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="openCodeInsertDialog"><NoteToolbarIcon name="code" /><span>代码块</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="triggerImageUpload"><NoteToolbarIcon name="image" /><span>图片</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="triggerAudioUpload"><NoteToolbarIcon name="audio" /><span>音频</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="triggerVideoUpload"><NoteToolbarIcon name="video" /><span>视频</span></button>
          <button v-if="editorPrefs.endnoteEnabled" class="note-toolbar-menu-item" @mousedown.prevent @click="insertEndnote"><NoteToolbarIcon name="endnote" /><span>尾注</span></button>
        </div>
        <div class="note-toolbar-group-label">表格</div>
        <div class="note-toolbar-table-grid" role="group" aria-label="选择表格大小">
          <button
            v-for="cell in 36"
            :key="'znt-' + cell"
            class="note-toolbar-table-cell"
            :class="{ active: Math.ceil(cell / 6) <= tablePickerHoverRow && ((cell - 1) % 6) + 1 <= tablePickerHoverCol }"
            :aria-label="`插入 ${Math.ceil(cell / 6)} 行 ${((cell - 1) % 6) + 1} 列表格`"
            @mouseenter="onTablePickerHover(Math.ceil(cell / 6), ((cell - 1) % 6) + 1)"
            @focus="onTablePickerHover(Math.ceil(cell / 6), ((cell - 1) % 6) + 1)"
            @click="onTablePickerHover(Math.ceil(cell / 6), ((cell - 1) % 6) + 1); insertTableFromMenu()"
          />
        </div>
      </NoteToolbarPanel>

      <NoteToolbarPanel v-model="showCharacterMenu" menu-id="character" title="字符" :anchor="charBtnRef">
        <div class="note-toolbar-menu-grid">
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="toggleHighlightColorPicker"><NoteToolbarIcon name="highlight" /><span>高亮</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="toggleFontColorPicker"><NoteToolbarIcon name="fontColor" /><span>字号与颜色</span></button>
          <button class="note-toolbar-menu-item" :class="{ active: isSup }" @mousedown.prevent @click="applySuperscript"><NoteToolbarIcon name="superscript" /><span>上标</span></button>
          <button class="note-toolbar-menu-item" :class="{ active: isSub }" @mousedown.prevent @click="applySubscript"><NoteToolbarIcon name="subscript" /><span>下标</span></button>
          <button class="note-toolbar-menu-item" @pointerdown="wysiwygEditorRef?.saveClearTarget()" @mousedown.prevent @click="handleClearFormatting"><NoteToolbarIcon name="clearFormat" /><span>清除格式</span></button>
        </div>
      </NoteToolbarPanel>

      <NoteToolbarPanel v-model="showParagraphMenu" menu-id="paragraph" title="段落" :anchor="paragraphBtnRef">
        <div class="note-toolbar-menu-grid">
          <button class="note-toolbar-menu-item" :class="{ active: isQuote }" @mousedown.prevent @click="toggleLinePrefix('> ')"><NoteToolbarIcon name="quote" /><span>引用</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="onObjectAlign('left')"><NoteToolbarIcon name="alignLeft" /><span>左对齐</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="onObjectAlign('center')"><NoteToolbarIcon name="alignCenter" /><span>居中</span></button>
          <button class="note-toolbar-menu-item" @mousedown.prevent @click="onObjectAlign('right')"><NoteToolbarIcon name="alignRight" /><span>右对齐</span></button>
        </div>
      </NoteToolbarPanel>

      <NoteToolbarPanel v-model="showToolsMenu" menu-id="tools" title="编辑" :anchor="toolsBtnRef">
        <div class="note-toolbar-menu-grid">
          <button class="note-toolbar-menu-item" @click="openFindBar(false)"><NoteToolbarIcon name="search" /><span>查找</span></button>
          <button class="note-toolbar-menu-item" @click="openFindBar(true)"><NoteToolbarIcon name="replace" /><span>查找替换</span></button>
        </div>
      </NoteToolbarPanel>

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
          <button class="find-nav-btn" :disabled="matchPositions.length === 0" @click="findPrev" data-tip="上一个" aria-label="上一个">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
          </button>
          <button class="find-nav-btn" :disabled="matchPositions.length === 0" @click="findNext" data-tip="下一个" aria-label="下一个">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <button class="find-close-btn" @click="closeFindBar" data-tip="关闭" aria-label="关闭">
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
          <button class="find-action-btn" :disabled="matchPositions.length === 0" @click="replaceCurrent" data-tip="替换" aria-label="替换">替换</button>
          <button class="find-action-btn" :disabled="matchPositions.length === 0" @click="replaceAll" data-tip="全部替换" aria-label="全部替换">全部</button>
        </div>
      </div>

      <div class="zen-note-content">
        <div v-if="showToc" id="zen-note-toc-panel" ref="tocPanelRef" class="toc-panel" role="dialog" aria-label="目录">
          <div class="toc-header">
            <span>目录</span>
            <button class="toc-close-btn" aria-label="关闭目录" @click="closeToc">
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
            >
              <button
                v-if="item.hasChildren"
                class="toc-toggle"
                :aria-label="collapsedToc.has(item.index) ? '展开子标题' : '收起子标题'"
                :aria-expanded="!collapsedToc.has(item.index)"
                @click.stop="toggleTocCollapse(item.index)"
              >
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" :class="{ 'toc-toggle-collapsed': collapsedToc.has(item.index) }">
                  <polyline points="6 9 12 15 18 9"/>
                </svg>
              </button>
              <span v-else class="toc-toggle-spacer"></span>
              <button class="toc-link" @click="scrollToHeading(item.id)">{{ item.text }}</button>
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
      </div>
    </template>

    <NoteToolbarPanel v-model="showHeadingMenu" menu-id="heading" title="正文" :anchor="headingBtnRef">
      <button class="note-toolbar-menu-item" :class="{ active: !currentHeadingLevel }" @mousedown.prevent @click="applyHeading('p')"><NoteToolbarIcon name="paragraph" /><span>正文</span></button>
      <button class="note-toolbar-menu-item" :class="{ active: currentHeadingLevel === 'h1' }" @mousedown.prevent @click="applyHeading('h1')"><span class="heading-icon">H1</span><span>标题 1</span></button>
      <button class="note-toolbar-menu-item" :class="{ active: currentHeadingLevel === 'h2' }" @mousedown.prevent @click="applyHeading('h2')"><span class="heading-icon">H2</span><span>标题 2</span></button>
      <button class="note-toolbar-menu-item" :class="{ active: currentHeadingLevel === 'h3' }" @mousedown.prevent @click="applyHeading('h3')"><span class="heading-icon">H3</span><span>标题 3</span></button>
      <button class="note-toolbar-menu-item" :class="{ active: currentHeadingLevel === 'h4' }" @mousedown.prevent @click="applyHeading('h4')"><span class="heading-icon">H4</span><span>标题 4</span></button>
    </NoteToolbarPanel>
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
            <h3>{{ mathDialogMode === 'insert' ? '插入数学公式' : '编辑数学公式' }}</h3>
            <button class="math-dialog-close" @click="closeMathEditDialog">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="math-dialog-content">
            <div class="math-mode-toggle">
              <button
                class="math-mode-btn"
                :class="{ active: !mathEditDisplayMode }"
                @click="mathEditDisplayMode = false"
              >行内</button>
              <button
                class="math-mode-btn"
                :class="{ active: mathEditDisplayMode }"
                @click="mathEditDisplayMode = true"
              >块级</button>
            </div>
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
            <button class="math-dialog-save" @click="saveMathEdit">{{ mathDialogMode === 'insert' ? '插入' : '保存' }}</button>
          </div>
        </div>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="showCodeInsertDialog" class="math-dialog-overlay" @click="showCodeInsertDialog = false">
        <div class="math-dialog" @click.stop>
          <div class="math-dialog-header">
            <h3>插入代码块</h3>
            <button class="math-dialog-close" @click="showCodeInsertDialog = false">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="math-dialog-content">
            <input
              v-model="codeInsertLang"
              class="code-lang-input"
              placeholder="语言（可选，如 python / js / sh）"
            />
            <textarea
              v-model="codeInsertText"
              class="math-edit-textarea code-insert-textarea"
              placeholder="粘贴或输入代码..."
            ></textarea>
          </div>
          <div class="math-dialog-footer">
            <button class="math-dialog-cancel" @click="showCodeInsertDialog = false">取消</button>
            <button class="math-dialog-save" @click="confirmCodeInsert">插入</button>
          </div>
        </div>
      </div>
    </Teleport>
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
      <div v-if="showFontColorPicker" class="font-color-picker-teleport" :style="fontColorPickerStyle" @click.stop @keydown.esc.stop.prevent="closeFontColorPickerFromKeyboard">
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
      <div v-if="showHighlightColorPicker" class="font-color-picker-teleport" :style="highlightColorPickerStyle" @click.stop @keydown.esc.stop.prevent="closeHighlightPickerFromKeyboard">
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
      <ExportProgressDialog
        :visible="exporting || exportStatus === 'success'"
        :format="exportFormat"
        :status="exportStatus"
        :progress="exportProgress"
        @cancel="cancelExport"
        @close="closeExportDialog"
      />
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useNotesStore } from '@/stores/notes'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useAnchoredPickerStyle } from '@/composables/pickerAnchoring'
import { useToolbarTip } from '@/composables/toolbarTip'
import { useEditorPrefsStore } from '@/stores/editorPrefs'
import { readSelectionFontSize } from '@/composables/editorFontSize'
import { useFindReplace } from '@/composables/useFindReplace'

import { useAsrStreaming } from '@/composables/useAsrStreaming'
import { useAsrHotwords } from '@/composables/useAsrHotwords'
import { renderMarkdownToHtml, renderMermaidBlocks, renderSingleMermaidBlock, fixLostMermaidBlocks, renderEchartsBlocks, fixLostEchartsBlocks, attachMathEditListeners, katexModule, renderMathSafe } from '@/composables/useMarkdown'
import ExportProgressDialog from './ExportProgressDialog.vue'
import WysiwygEditor from './WysiwygEditor.vue'
import WordCountButton from './WordCountButton.vue'
import NoteToolbarPanel from './NoteToolbarPanel.vue'
import NoteToolbarIcon from './NoteToolbarIcon.vue'
import { resolveImageUrl, uploadImage, uploadMedia } from '@/api/imageUpload'
import { visibleEditorText } from '@/utils/noteWordCount'

const props = defineProps<{
  notebookId: string
  noteId: string
}>()

const emit = defineEmits<{
  selectNote: [notebookId: string, noteId: string]
}>()

const notesStore = useNotesStore()
const { show: showToast } = useToast()
const { confirm: showConfirm } = useConfirmDialog()
const { hotwords: asrHotwords } = useAsrHotwords()

const noteTitle = ref('')
const noteContent = ref('')
const hasChanges = ref(false)
const saving = ref(false)
let initialLoad = true
const AUTO_SAVE_INTERVAL = 5 * 60 * 1000
let autoSaveTimer: ReturnType<typeof setInterval> | null = null
let mermaidEditHandler: EventListener | null = null
let mermaidZoomHandler: EventListener | null = null
let mathEditHandler: EventListener | null = null

const previewRef = ref<HTMLElement | null>(null)
const headingBtnRef = ref<HTMLButtonElement | null>(null)
const findInputRef = ref<HTMLInputElement | null>(null)
const wysiwygEditorRef = ref<InstanceType<typeof WysiwygEditor> | null>(null)
const imageUploadInputRef = ref<HTMLInputElement | null>(null)
const mediaUploadInputRef = ref<HTMLInputElement | null>(null)
const pendingMediaKind = ref<'audio' | 'video'>('audio')
// 字数统计（R3，2026-09-13 审计 F-B）：UI/逻辑收口到 WordCountButton.vue；
// 这里只提供「当前编辑器可见文本」取文函数（点击时才读 DOM）。
const wordCountRef = ref<InstanceType<typeof WordCountButton> | null>(null)
function wordCountSourceText(): string {
  const container = wysiwygEditorRef.value?.editorRef || previewRef.value
  return container ? visibleEditorText(container as HTMLElement) : (noteContent.value || '')
}

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
  // 块多选已删除（判据 #5）：对齐作用于原生主选区所在块
  const block = editor.getCurrentBlockElement()
  if (block) {
    editor.setBlockAlignment(block, align)
  }
}

const showHeadingMenu = ref(false)
const showListMenu = ref(false)
const showInsertMenu = ref(false)
const showCharacterMenu = ref(false)
const showParagraphMenu = ref(false)
const showToolsMenu = ref(false)
const listBtnRef = ref<HTMLButtonElement | null>(null)
const insertBtnRef = ref<HTMLButtonElement | null>(null)
const charBtnRef = ref<HTMLButtonElement | null>(null)
const paragraphBtnRef = ref<HTMLButtonElement | null>(null)
const toolsBtnRef = ref<HTMLButtonElement | null>(null)
const currentHeadingLevel = ref<string | null>(null)
const showExportPicker = ref(false)
const showToc = ref(false)
const tocBtnRef = ref<HTMLButtonElement | null>(null)
const tocPanelRef = ref<HTMLElement | null>(null)
const showFontColorPicker = ref(false)
const showHighlightColorPicker = ref(false)
const customHighlightColor = ref('#ffff00')
const customFontColor = ref('#ff0000')
const selectedFontSize = ref('')
const tablePickerHoverRow = ref(0)
const tablePickerHoverCol = ref(0)
const exporting = ref(false)
const exportFormat = ref<'md' | 'pdf'>('md')
const exportStatus = ref<'idle' | 'exporting' | 'success'>('idle')
const exportAbortController = ref<AbortController | null>(null)
const exportTaskId = ref<string | null>(null)
const exportProgress = ref(0)
// 标题菜单定位：**打开瞬间**测量按钮矩形。旧实现是 computed —— 唯一响应式
// 依赖是模板 ref（挂载时赋值一次、之后不变），getBoundingClientRect 又不是
// 响应式的，于是首次求值的矩形被永久缓存：面板拖宽/窗口改比例后按钮已经移位，
// 菜单仍开在按钮的旧位置（16:9 上恰好没移过所以看着是对的）。


const { pickerStyle: fontColorPickerStyle, refreshPickerStyle: refreshFontColorPickerStyle } =
  useAnchoredPickerStyle(charBtnRef)
const { onTipPointer } = useToolbarTip()
const editorPrefs = useEditorPrefsStore()

const { pickerStyle: highlightColorPickerStyle, refreshPickerStyle: refreshHighlightColorPickerStyle } =
  useAnchoredPickerStyle(charBtnRef)

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
  showHighlightColorPicker.value = false
  showFontColorPicker.value = false
  showListMenu.value = false
  showInsertMenu.value = false
  showCharacterMenu.value = false
  showParagraphMenu.value = false
  showToolsMenu.value = false
}

function toggleListMenu() {
  const next = !showListMenu.value
  closeAllToolbarMenus()
  showListMenu.value = next
}

function toggleInsertMenu() {
  const next = !showInsertMenu.value
  closeAllToolbarMenus()
  if (next) wysiwygEditorRef.value?.saveEditorSelection?.()
  showInsertMenu.value = next
}

function toggleCharacterMenu() {
  const next = !showCharacterMenu.value
  closeAllToolbarMenus()
  showCharacterMenu.value = next
}

function toggleParagraphMenu() {
  const next = !showParagraphMenu.value
  closeAllToolbarMenus()
  showParagraphMenu.value = next
}

function toggleToolsMenu() {
  const next = !showToolsMenu.value
  closeAllToolbarMenus()
  showToolsMenu.value = next
}

function onPrimaryToolbarKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const root = event.currentTarget as HTMLElement
  const tools = Array.from(root.querySelectorAll<HTMLElement>('[data-primary-tool]'))
    .filter((el) => !(el as HTMLButtonElement).disabled)
    .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left)
  const current = tools.indexOf(document.activeElement as HTMLElement)
  if (current < 0) return
  event.preventDefault()
  let next = current
  if (event.key === 'ArrowRight') next = (current + 1) % tools.length
  if (event.key === 'ArrowLeft') next = (current - 1 + tools.length) % tools.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = tools.length - 1
  tools.forEach((tool) => { tool.tabIndex = -1 })
  tools[next].tabIndex = 0
  tools[next].focus()
}

function insertTableFromMenu() {
  const rows = tablePickerHoverRow.value
  const cols = tablePickerHoverCol.value
  if (rows < 1 || cols < 1) return
  closeAllToolbarMenus()
  wysiwygEditorRef.value?.restoreEditorSelection?.()
  wysiwygEditorRef.value?.insertTable(rows, cols)
}

function insertLink() {
  wysiwygEditorRef.value?.saveEditorSelection?.()
  wysiwygEditorRef.value?.insertLink?.()
}

function toggleHeadingMenu() {
  const next = !showHeadingMenu.value
  closeAllToolbarMenus()
  updateHeadingButtonState()
  showHeadingMenu.value = next
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
  let bold = false, italic = false, stateKnown = false
  try {
    bold = document.queryCommandState('bold')
    italic = document.queryCommandState('italic')
    stateKnown = true
  } catch {}
  // With a collapsed caret the command state is the pending typing style:
  // after toggling bold OFF inside a <strong>, the ancestor walk must not
  // force the button back to pressed.
  const trustAncestor = !stateKnown || !sel.isCollapsed
  let node: Node | null = sel.anchorNode
  let quote = false, sup = false, sub = false
  while (node && node !== editorEl) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = (node as HTMLElement).tagName
      if (trustAncestor) {
        if (tag === 'B' || tag === 'STRONG') bold = true
        if (tag === 'I' || tag === 'EM') italic = true
      }
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
    // Collapsed-caret bold/italic only sets a pending typing state; no DOM
    // mutation fires selectionchange, so refresh the pressed state explicitly.
    nextTick(() => updateFormatStates())
  }
}

function toggleHighlightColorPicker() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.saveEditorSelection) {
    editor.saveEditorSelection()
  }
  if (showHighlightColorPicker.value) {
    showHighlightColorPicker.value = false
  } else {
    refreshHighlightColorPickerStyle()
    closeAllToolbarMenus()
    showHighlightColorPicker.value = true
    nextTick(() => focusFirstSwatch('.font-color-picker-teleport'))
  }
}

function focusFirstSwatch(selector: string) {
  document.querySelector<HTMLElement>(selector + ' .color-swatch')?.focus()
}

function closeHighlightPickerFromKeyboard() {
  showHighlightColorPicker.value = false
  charBtnRef.value?.focus()
}

function closeFontColorPickerFromKeyboard() {
  showFontColorPicker.value = false
  charBtnRef.value?.focus()
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

function onTablePickerHover(row: number, col: number) {
  tablePickerHoverRow.value = row
  tablePickerHoverCol.value = col
}


// Watch for WysiwygEditor ref to set previewRef
watch(wysiwygEditorRef, (newVal) => {
  if (newVal?.editorRef) {
    previewRef.value = newVal.editorRef
  }
}, { immediate: true })

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
  processedSvg = processedSvg.replace(/width="100%"/, `width="${vbWidth}" height="${vbHeight}"`)
  processedSvg = processedSvg.replace(/max-width:\s*[^;"]+;?/g, '')
  processedSvg = processedSvg.replace(/font-size:\s*\d+(\.\d+)?px/g, 'font-size: 14px')
  
  showMermaidZoomDialog.value = true
  
  nextTick(() => {
    const container = mermaidZoomSvgRef.value
    if (container) {
      container.innerHTML = processedSvg
      const innerSvg = container.querySelector('svg')
      if (innerSvg) {
        innerSvg.style.display = 'block'
        innerSvg.style.maxWidth = 'none'
        innerSvg.setAttribute('width', `${vbWidth * baseScale}`)
        innerSvg.setAttribute('height', `${vbHeight * baseScale}`)
        innerSvg.setAttribute('viewBox', mermaidSvgViewBox.value)
      }
      setupMermaidInlineEditing()
      
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
    const escapedOld = originalText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    mermaidZoomSource.value = mermaidZoomSource.value.replace(new RegExp(escapedOld, 'g'), newText)
    textEl.setAttribute('data-original-text', newText)
    
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
    // 编辑器外直接改树（评审 N1）：显式结束虚拟多 range 会话
    wysiwygEditorRef.value?.clearMultiRanges?.()
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
  mathDialogMode.value = 'edit'
  mathEditTex.value = payload.tex
  mathEditDisplayMode.value = payload.displayMode
  mathEditElement.value = payload.element
  showMathEditDialog.value = true
}

function openMathInsertDialog() {
  wysiwygEditorRef.value?.saveEditorSelection()
  mathDialogMode.value = 'insert'
  mathEditTex.value = ''
  mathEditDisplayMode.value = false
  mathEditElement.value = null
  showMathEditDialog.value = true
}

function openCodeInsertDialog() {
  wysiwygEditorRef.value?.saveEditorSelection()
  codeInsertLang.value = ''
  codeInsertText.value = ''
  showCodeInsertDialog.value = true
}

function confirmCodeInsert() {
  if (!codeInsertText.value.trim()) {
    showCodeInsertDialog.value = false
    return
  }
  wysiwygEditorRef.value?.insertCodeBlock(codeInsertText.value, codeInsertLang.value)
  showCodeInsertDialog.value = false
  wysiwygEditorRef.value?.focus()
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
    const esc = (mathEditTex.value || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    return `<span style="color: var(--color-text-light)">${esc}</span>`
  }
})

function saveMathEdit() {
  if (mathDialogMode.value === 'insert') {
    if (!mathEditTex.value.trim()) {
      showMathEditDialog.value = false
      return
    }
    wysiwygEditorRef.value?.insertMath(mathEditTex.value, mathEditDisplayMode.value)
    showMathEditDialog.value = false
    wysiwygEditorRef.value?.focus()
    return
  }
  const el = mathEditElement.value
  if (!el) {
    showMathEditDialog.value = false
    return
  }
  const newTex = mathEditTex.value
  const displayMode = mathEditDisplayMode.value
  // 唯一构造器 renderMathSafe（Hub 契约：math-editable 形状/data-tex/escapeMathPercent 单点收口）
  const wrapper = document.createElement('div')
  wrapper.innerHTML = renderMathSafe(newTex, displayMode)
  const newEl = wrapper.firstElementChild as HTMLElement
  if (newEl) {
    el.replaceWith(newEl)
    // 编辑器外直接改树（评审 N1）：显式结束虚拟多 range 会话
    wysiwygEditorRef.value?.clearMultiRanges?.()
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

const isDeleting = ref(false)

async function confirmDelete() {
  if (!await showConfirm({ message: '确定要删除这条笔记吗？', danger: true, confirmText: '删除' })) return
  isDeleting.value = true
  try {
    await notesStore.deleteNote(props.noteId)
    emit('selectNote', props.notebookId, '')
  } catch (e) {
    isDeleting.value = false
    console.error('Failed to delete note:', e)
  }
}

async function handleExport(format: 'md' | 'pdf') {
  showExportPicker.value = false
  if (hasChanges.value || saving.value) {
    const saved = await saveNote()
    if (!saved || hasChanges.value) {
      showToast('保存失败，已取消导出', 'error')
      return
    }
  }
  exportFormat.value = format
  exportStatus.value = 'exporting'
  exporting.value = true
  exportProgress.value = 0
  exportTaskId.value = null
  try {
    await notesStore.exportNoteAsync(props.noteId, format, (task) => {
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

function onDocumentClick(e: MouseEvent) {
  if (!showHeadingMenu.value && !showFontColorPicker.value && !showHighlightColorPicker.value) return
  const target = e.target as HTMLElement
  // Don't close menus when clicking TOC button (TOC is not mutually exclusive)
  if (target.closest('button[data-tip="目录"]')) return
  // 拾取器的触发入口在「字符」二级菜单内：该次点击只负责打开拾取器，
  // 不能在同一事件的冒泡阶段把它又关掉（面板是 Teleport，旧 wrap 守卫不覆盖）。
  if (target.closest('.note-toolbar-panel[data-menu="character"]')) return
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
}

function onSelectionChange() {
  updateHeadingButtonState()
}

function onGlobalKeydown(e: KeyboardEvent) {
  // Esc：关闭插入/编辑对话框（公式/代码块）
  if (e.key === 'Escape') {
    if (showMathEditDialog.value) {
      closeMathEditDialog()
      e.preventDefault()
      wysiwygEditorRef.value?.focus()
    } else if (showCodeInsertDialog.value) {
      showCodeInsertDialog.value = false
      e.preventDefault()
      wysiwygEditorRef.value?.focus()
    } else if (showToc.value) {
      closeToc()
      e.preventDefault()
    }
  }
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onGlobalKeydown)
  document.addEventListener('selectionchange', onSelectionChange)
  autoSaveTimer = setInterval(async () => {
    if (hasChanges.value && props.noteId && !saving.value) {
      saving.value = true
      try {
        await wysiwygEditorRef.value?.flushPendingImageUploads?.()
        await wysiwygEditorRef.value?.flushPendingSerialization?.()
        await notesStore.updateNote(props.noteId, {
          title: noteTitle.value || null,
          content: noteContent.value
        })
        hasChanges.value = false
        showToast('已自动保存', 'success')
      } catch (e) {
        console.error('Auto-save failed:', e)
      } finally {
        saving.value = false
      }
    }
  }, AUTO_SAVE_INTERVAL)
  mermaidEditHandler = ((e: CustomEvent) => {
    if (e.detail?.handled) return
    const target = e.target as HTMLElement
    const container = wysiwygEditorRef.value?.editorRef || previewRef.value
    if (!target || !container || !container.contains(target)) return
    e.detail.handled = true
    handleMermaidEdit(e.detail.source, e.detail.id)
  }) as EventListener
  mermaidZoomHandler = ((e: CustomEvent) => {
    if (e.detail?.handled) return
    const target = e.target as HTMLElement
    const container = wysiwygEditorRef.value?.editorRef || previewRef.value
    if (!target || !container || !container.contains(target)) return
    e.detail.handled = true
    handleMermaidZoom(e.detail.source, e.detail.id, e.detail.svg)
  }) as EventListener
  mathEditHandler = ((e: CustomEvent) => {
    if (e.detail?.handled) return
    const target = e.target as HTMLElement
    const container = wysiwygEditorRef.value?.editorRef || previewRef.value
    if (!target || !container || !container.contains(target)) return
    e.detail.handled = true
    handleMathEdit({ tex: e.detail.tex, displayMode: e.detail.displayMode, element: e.detail.element })
  }) as EventListener

  document.addEventListener('mermaid-edit', mermaidEditHandler)
  document.addEventListener('mermaid-zoom', mermaidZoomHandler)
  document.addEventListener('math-edit', mathEditHandler)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onGlobalKeydown)
  document.removeEventListener('selectionchange', onSelectionChange)
  if (autoSaveTimer) {
    clearInterval(autoSaveTimer)
    autoSaveTimer = null
  }
  if (mermaidEditHandler) document.removeEventListener('mermaid-edit', mermaidEditHandler)
  if (mermaidZoomHandler) document.removeEventListener('mermaid-zoom', mermaidZoomHandler)
  if (mathEditHandler) document.removeEventListener('math-edit', mathEditHandler)
})

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

const mathEditTex = ref('')
const mathEditDisplayMode = ref(false)
const mathEditElement = ref<HTMLElement | null>(null)
const mathDialogMode = ref<'edit' | 'insert'>('edit')
const showCodeInsertDialog = ref(false)
const codeInsertLang = ref('')
const codeInsertText = ref('')

const {
  showFindBar, showReplaceBar, findQuery, replaceQuery, matchPositions, currentMatchIndex,
  computeMatches, highlightCurrentMatch, onFindInput, findNext, findPrev,
  onFindKeydown, onReplaceKeydown, replaceCurrent, replaceAll, openFindBar, closeFindBar,
} = useFindReplace({
  getEditor: () => wysiwygEditorRef.value,
  findInputRef,
  onMutate: () => { hasChanges.value = true },
})

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

let asrInsertPos = 0
let asrPreviousLen = 0
let asrNeedNewline = false

function scrollNoteEditorToBottom() {
  // v-html 重渲染是异步的（watch→renderedContent→下一拍 DOM），等渲染帧后再滚
  nextTick(() => {
    requestAnimationFrame(() => {
      const el = wysiwygEditorRef.value?.editorRef
      if (el) {
        el.scrollTop = el.scrollHeight
      }
    })
  })
}

function scrollAsrContentIntoView() {
  // 仅当转写插入点在内容末尾时跟随滚动（中途插入不拽动用户视角）
  if (asrInsertPos + asrPreviousLen >= noteContent.value.length) {
    scrollNoteEditorToBottom()
  }
}

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
    scrollAsrContentIntoView()
  },
  onError(message) {
    showToast(message, 'error')
  },
})
const isRecording = asrStreaming.isRecording
const isProcessing = asrStreaming.isFinishing

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
        scrollAsrContentIntoView()
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
    scrollNoteEditorToBottom()
  } catch (error) {
    asrPreviousLen = 0
    console.error('ASR failed:', error)
    const msg = error instanceof Error ? error.message : '语音识别失败'
    showToast(msg, 'error')
  }
}

const currentNoteId = computed(() => props.noteId)

// Load note when noteId changes
watch(() => props.noteId, async (id) => {
  if (!id) {
    noteTitle.value = ''
    noteContent.value = ''
    hasChanges.value = false
    initialLoad = true
    return
  }
  initialLoad = true
  await notesStore.loadNote(id)
  const note = notesStore.currentNote
  if (note) {
    noteTitle.value = note.title || ''
    noteContent.value = note.content || ''
    hasChanges.value = false
  }
  nextTick(() => {
    initialLoad = false
    wysiwygEditorRef.value?.resetUndoStack()
  })
}, { immediate: true })

let saveInFlight: Promise<boolean> | null = null

async function saveNote(): Promise<boolean> {
  if (!props.noteId) return false
  // Share an in-flight save instead of failing (export/autosave overlap).
  if (saveInFlight) return saveInFlight
  const task = runSave()
  saveInFlight = task
  try {
    return await task
  } finally {
    if (saveInFlight === task) saveInFlight = null
  }
}

async function runSave(): Promise<boolean> {
  saving.value = true
  try {
    // Pasted images upload in the background; saving before they finish would
    // persist markdown with the images still missing (blob:/data: dropped).
    await wysiwygEditorRef.value?.flushPendingImageUploads?.()
    await wysiwygEditorRef.value?.flushPendingSerialization?.()
    await notesStore.updateNote(props.noteId, {
      title: noteTitle.value || null,
      content: noteContent.value
    })
    hasChanges.value = false
    showToast('已保存', 'success')
    return true
  } catch (e) {
    console.error('Failed to save note:', e)
    showToast('保存失败', 'error')
    return false
  } finally {
    saving.value = false
  }
}

function onInput() {
  if (!initialLoad) {
    hasChanges.value = true
  }
}

function onContentChange() {
  if (!initialLoad) {
    hasChanges.value = true
  }
  if (showFindBar.value && findQuery.value) {
    computeMatches()
  }
  // Keep the open word-count popover live while typing
  wordCountRef.value?.refresh()
}

async function createNewNote() {
  if (!props.notebookId) {
    showToast('请先选择一个笔记本', 'error')
    return
  }
  try {
    const note = await notesStore.createNote(props.notebookId, { content: '' })
    emit('selectNote', props.notebookId, note.id)
    showToast('笔记已创建', 'success')
  } catch (e) {
    console.error('Failed to create note:', e)
    showToast('创建笔记失败', 'error')
  }
}

function toggleInlineFormat(prefix: string, suffix: string) {
  if (prefix === '**' && suffix === '**') {
    execCommand('bold')
  } else if (prefix === '*' && suffix === '*') {
    execCommand('italic')
  }
}

function toggleLinePrefix(prefix: string) {
  const editor = wysiwygEditorRef.value
  // 块多选已删除（判据 #5）：前缀类命令作用于原生主选区所在块
  if (prefix === '> ') {
    execCommand('formatBlock', 'blockquote')
  } else if (prefix === '- ') {
    execCommand('insertUnorderedList')
  } else if (prefix === '1. ') {
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
            execCommand('insertOrderedList')
            return
          }
          if (el.tagName === 'LI') {
            const parentOl = el.parentElement
            if (parentOl && parentOl.tagName === 'OL') {
              if (parentOl.parentElement?.tagName === 'LI') {
                return
              }
              execCommand('insertOrderedList')
              return
            }
          }
        }
        node = node.parentNode
      }
    }
    execCommand('insertOrderedList')
  }
}

function handleRestartNumbering() {
  const editor = wysiwygEditorRef.value
  if (!editor || !editor.restartOlNumbering || !editor.findCurrentOl) return
  if (editor.saveEditorSelection) editor.saveEditorSelection()
  if (editor.restoreEditorSelection) editor.restoreEditorSelection()
  const ol = editor.findCurrentOl()
  // 判据 #7：不再静默无操作 —— 无列表上下文/已在首项都给明确反馈
  if (!ol) {
    showToast('请先将光标点到编号列表内，再点「重新编号」', 'error')
    return
  }
  const res = editor.restartOlNumbering(ol)
  if (res === 'already-first') {
    showToast('该项编号已是从 1 开始', 'info')
  } else if (res === 'none') {
    showToast('请先将光标点到编号列表内，再点「重新编号」', 'error')
  }
}

function applyHighlight() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.applyHighlightToSelection) {
    editor.applyHighlightToSelection('#ffff00')
  }
}

function getSelectionFontSize() {
  // wave-2 C2：共享 readSelectionFontSize（按选区真实覆盖段取字号，边界不失真）
  const editorEl = wysiwygEditorRef.value?.editorRef
  selectedFontSize.value = readSelectionFontSize(editorEl) // 共享默认字号表（wave-3 #9 去重）
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
    refreshFontColorPickerStyle()
    closeAllToolbarMenus()
    showFontColorPicker.value = true
    nextTick(() => focusFirstSwatch('.font-color-picker-teleport'))
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
  // 唯一实现走引擎 applyFontSizeToSelection（逐 text-node 包裹，绝不 extractContents
  // 整块包裹 —— 跨块选区 span>p 非法结构即换行/字号失效根因）。与 NoteEditor 同步。
  if (editor.applyFontSizeToSelection) {
    editor.applyFontSizeToSelection(size)
  }
  // 不清选区：格式化后停留原位（wave-2 复审 #2）
  showFontColorPicker.value = false
}

function applySuperscript() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.applySuperscript) {
    editor.applySuperscript()
  } else {
    execCommand('superscript')
  }
}

/** 清除格式（遗留 #1 方案 A）：有选区清选区；折叠光标清整篇（先确认）。 */
async function handleClearFormatting() {
  const editor = wysiwygEditorRef.value
  if (!editor || typeof editor.clearFormatting !== 'function') return
  if (editor.isEditorComposing?.()) {
    showToast('输入法组合中，请稍后再试', 'error')
    return
  }
  // 浏览器/移动端可能在按下与 click 之间折叠选区 —— 用按钮按下瞬间的缓存找回选区，
  // 保证「选了文字 → 只清选中部分」；只有确实没选文字才询问整篇。
  if (!editor.hasTextSelection?.()) editor.restoreClearTarget?.()
  const scope = editor.hasTextSelection?.() ? 'selection' : 'document'
  if (scope === 'document') {
    const ok = await showConfirm({
      message: '未选中文字，将清除整篇笔记的格式（可撤销）。是否继续？',
      confirmText: '清除整篇',
    })
    if (!ok) return
  }
  const done = editor.clearFormatting(scope)
  editor.clearClearTarget?.()
  if (done === 'busy') showToast('输入法组合中，请稍后再试', 'error')
  else if (done) showToast(done === 'selection' ? '已清除选中格式' : '已清除整篇格式', 'success')
}

function applySubscript() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.applySubscript) {
    editor.applySubscript()
  } else {
    execCommand('subscript')
  }
}

function insertEndnote() {
  const editor = wysiwygEditorRef.value
  if (editor && editor.insertEndnote) {
    if (editor.restoreEditorSelection) {
      editor.restoreEditorSelection()
    }
    editor.insertEndnote()
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

function toggleToc() {
  if (showToc.value) {
    closeToc()
  } else {
    showToc.value = true
    nextTick(() => {
      tocPanelRef.value?.querySelector<HTMLElement>('.toc-link, .toc-close-btn')?.focus()
    })
  }
}

function closeToc() {
  showToc.value = false
  nextTick(() => tocBtnRef.value?.focus())
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

function insertHorizontalRule() {
  execCommand('insertHorizontalRule')
}

function insertCodeBlock() {
  execCommand('formatBlock', 'pre')
}

function onTextareaKeydown(e: KeyboardEvent) {
  if (showFindBar.value) {
    if (e.key === 'Escape') {
      e.preventDefault()
      closeFindBar()
      return
    }
  }
}

defineExpose({ hasChanges, saveNote, isDeleting })
</script>

<style scoped>
.zen-note-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  background-color: var(--color-bg);
}

.zen-note-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-light);
}

.empty-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
}

.create-note-btn {
  padding: 10px 24px;
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
  border-radius: var(--radius-md);
  font-weight: 500;
  font-size: 14px;
}

.zen-note-toolbar {
  container-type: inline-size;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 14px;
  background-color: var(--surface-panel-strong, var(--color-white));
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  -ms-overflow-style: none;
}

.toolbar-left::-webkit-scrollbar {
  display: none;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 头部动作区（工作台三按钮波 2026-10-04）：[保存] │(分隔) [导出][删除]——
   主操作与危险操作分组，图标钮右锚定位（保存出现/消失不位移），统一 30px 高。
   分隔线只在保存在场时出现（相邻兄弟选择器），清洁态不留孤分隔。 */
.header-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.save-btn + .header-actions {
  padding-left: 10px;
  border-left: 1px solid var(--color-border);
}

/* 工具栏按钮尺寸统一（判据 #1）：与笔记页同 36×36 方盒（border-box）+ 内容居中；
   皮肤层只保留配色/边框/圆角（覆写同值见 themes/*.css）。 */
.toolbar-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  padding: 0;
  box-sizing: border-box;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
  transition: all var(--transition-fast);
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

.toolbar-primary-btn {
  width: 60px !important;
  min-width: 60px;
  gap: 5px;
  padding: 0;
  justify-content: center;
}
.toolbar-primary-label {
  font-size: 12px;
  white-space: nowrap;
}
.toolbar-voice-btn.recording {
  color: var(--color-danger, #c0392b);
}
/* 工具栏 DOM 顺序即视觉顺序（禁止用 flex order 反排，避免 Tab/读屏顺序与视觉不一致） */

/* 字数统计按钮固定最右（内容不足时由 spacer 撑开；溢出滚动时 sticky 钉在右缘不裁剪） */
.toolbar-spacer {
  flex: 1 1 auto;
  min-width: 8px;
}

.toolbar-divider {
  width: 1px;
  height: 18px;
  background-color: var(--color-border);
  margin: 0 4px;
  flex-shrink: 0;
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

.find-replace-bar {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 12px;
  background-color: var(--surface-panel-strong, var(--color-white));
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

.save-btn {
  height: 30px;
  padding: 0 14px;
  background-color: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  flex-shrink: 0;
}

.save-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.export-btn,
.delete-btn {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  flex-shrink: 0;
  transition: background-color var(--transition-fast), color var(--transition-fast);
}

.export-btn {
  color: var(--color-text-light);
}

.export-btn:hover {
  color: var(--color-primary);
  background-color: var(--color-hover);
}

.delete-btn {
  color: var(--color-error);
}

.delete-btn:hover {
  background-color: color-mix(in srgb, var(--color-error) 8%, transparent);
}

.export-format-overlay {
  position: fixed;
  inset: 0;
  z-index: 998;
  background: rgba(0, 0, 0, 0.4);
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

.zen-note-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background-color: var(--surface-panel-strong, var(--color-white));
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.zen-title-input {
  flex: 1;
  min-width: 0;
  padding: 4px 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
  border: none;
  outline: none;
  background: transparent;
}

.zen-title-input::placeholder {
  color: var(--color-text-light);
}

.zen-note-content {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  position: relative;
}

.wysiwyg-editor-wrapper {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.preview-pane {
  flex: 1;
  padding: 16px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--color-text);
  background-color: var(--color-bg);
  overflow-y: auto;
}

.preview-pane :deep(code) {
  font-family: var(--font-mono);
  background-color: var(--color-code-bg);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}

.preview-pane :deep(pre) {
  background-color: var(--color-code-bg);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--color-border);
}

.preview-pane :deep(.code-block pre) {
  background: transparent;
  border: 0;
  border-radius: 0;
  margin: 0;
  padding: 12px 16px;
}

.preview-pane :deep(pre code) {
  background: none;
  padding: 0;
}

.preview-pane :deep(h1),
.preview-pane :deep(h2),
.preview-pane :deep(h3),
.preview-pane :deep(h4),
.preview-pane :deep(h5),
.preview-pane :deep(h6) {
  margin: 12px 0 8px;
  font-weight: 600;
  line-height: 1.4;
  cursor: pointer;
  scroll-margin-top: 16px;
}

.preview-pane :deep(h1:hover),
.preview-pane :deep(h2:hover),
.preview-pane :deep(h3:hover),
.preview-pane :deep(h4:hover),
.preview-pane :deep(h5:hover),
.preview-pane :deep(h6:hover) {
  text-decoration: underline;
}

.preview-pane :deep(h1) { font-size: 1.4em; }
.preview-pane :deep(h2) { font-size: 1.2em; }
.preview-pane :deep(h3) { font-size: 1.1em; }

.preview-pane :deep(ul),
.preview-pane :deep(ol) {
  padding-left: 1.5em;
  margin: 8px 0;
}

.preview-pane :deep(ul) {
  list-style-type: disc;
}

.preview-pane :deep(ol) {
  list-style-type: none;
  counter-reset: ol-counter;
}

.preview-pane :deep(ol > li) {
  position: relative;
  counter-increment: ol-counter;
}

.preview-pane :deep(ol > li::before) {
  content: counters(ol-counter, ".") ". ";
  position: absolute;
  right: 100%;
  margin-right: 4px;
  white-space: nowrap;
}

.preview-pane :deep(blockquote) {
  border-left: 3px solid var(--color-primary);
  padding-left: 12px;
  margin: 8px 0;
  color: var(--color-text-light);
}

.preview-pane :deep(a) {
  color: var(--color-primary-dark);
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-dark);
  cursor: pointer;
}

.preview-pane :deep(.mermaid-block) {
  position: relative;
  display: flex;
  justify-content: center;
  margin: 12px 0;
  overflow-x: auto;
}

.preview-pane :deep(.mermaid-rendered-content) {
  display: flex;
  justify-content: center;
}

.preview-pane :deep(.mermaid-rendered-content svg) {
  max-width: 100%;
  height: auto;
}

.preview-pane :deep(.echarts-block) {
  position: relative;
  margin: 12px 0;
  overflow-x: auto;
}

.preview-pane :deep(.echarts-block svg) {
  max-width: 100%;
  height: auto;
  display: block;
}

.preview-pane :deep(.echarts-error) {
  background-color: color-mix(in srgb, var(--color-danger, #e53e3e) 10%, transparent);
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
  overflow-x: auto;
}

.preview-pane :deep(.mermaid-controls) {
  position: absolute;
  top: 8px;
  right: 8px;
  display: flex;
  gap: 6px;
  opacity: 0.6;
  transition: opacity 0.2s;
  z-index: 10;
}

.preview-pane :deep(.mermaid-block:hover .mermaid-controls) {
  opacity: 1;
}

.preview-pane :deep(.mermaid-edit-btn),
.preview-pane :deep(.mermaid-zoom-btn) {
  padding: 8px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 2px 8px rgba(90, 130, 60, 0.15);
}

.preview-pane :deep(.mermaid-edit-btn:hover),
.preview-pane :deep(.mermaid-zoom-btn:hover) {
  background-color: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
  box-shadow: 0 4px 12px rgba(90, 130, 60, 0.2);
  transform: scale(1.05);
}

.preview-pane :deep(.math-editable) {
  position: relative;
  border-radius: var(--radius-sm);
  transition: background-color 0.2s;
}

.preview-pane :deep(div.math-editable) {
  display: block;
  text-align: center;
  margin: 8px 0;
}

.preview-pane :deep(span.math-editable) {
  display: inline;
}

.preview-pane :deep(.math-editable:hover) {
  background-color: var(--color-hover);
}

.preview-pane :deep(.math-rendered-content) {
  display: inline;
}

.preview-pane :deep(.math-controls) {
  position: absolute;
  top: -10px;
  right: -4px;
  display: inline-flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
  z-index: 10;
}

.preview-pane :deep(.math-editable:hover .math-controls) {
  opacity: 1;
}

.preview-pane :deep(.math-edit-btn) {
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

.preview-pane :deep(.math-edit-btn:hover) {
  background-color: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
  box-shadow: 0 4px 12px rgba(90, 130, 60, 0.2);
  transform: scale(1.05);
}

.mermaid-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.mermaid-dialog {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  max-width: 700px;
  width: 100%;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.mermaid-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);
}

.mermaid-dialog-header h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
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
  min-height: 200px;
}

.mermaid-edit-textarea {
  width: 100%;
  min-height: 250px;
  max-height: 60vh;
  padding: 12px;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.5;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-code-bg);
  color: var(--color-text);
  resize: vertical;
  box-sizing: border-box;
  outline: none;
}

.mermaid-edit-textarea:focus {
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
  padding: 6px 16px;
  background: var(--color-hover);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: all var(--transition-fast);
}

.mermaid-dialog-cancel:hover {
  background: var(--color-border);
}

.mermaid-dialog-save {
  padding: 6px 16px;
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  transition: all var(--transition-fast);
}

.mermaid-dialog-save:hover {
  background: var(--color-primary-dark);
}

.mermaid-zoom-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.mermaid-zoom-card {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  max-width: 90vw;
  width: 900px;
  height: 85vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.mermaid-zoom-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--color-border);
  flex-wrap: wrap;
  gap: 12px;
}

.mermaid-zoom-header h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
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
  overflow: auto;
  padding: 20px;
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
  max-width: none;
  pointer-events: none;
}

.mermaid-zoom-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  justify-content: center;
}

.zoom-label {
  font-size: 12px;
  color: var(--color-text-light);
}

.zoom-slider {
  width: 120px;
  accent-color: var(--color-primary);
}

.zoom-value {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
  min-width: 36px;
}

.zoom-reset-btn {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
}

.zoom-reset-btn:hover {
  background: var(--color-hover);
  color: var(--color-text);
}

.math-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.math-dialog {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  max-width: 700px;
  width: 100%;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
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
  font-size: 15px;
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
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text);
  background-color: var(--color-code-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  resize: vertical;
  box-sizing: border-box;
  outline: none;
}

.math-edit-textarea:focus {
  border-color: var(--color-primary);
}

.math-mode-toggle {
  display: flex;
  gap: 0;
  margin-bottom: 10px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
  width: fit-content;
}

.math-mode-btn {
  padding: 5px 14px;
  font-size: 12px;
  color: var(--color-text-light);
  background-color: var(--color-bg);
  transition: all var(--transition-fast);
}

.math-mode-btn.active {
  color: var(--color-white);
  background-color: var(--color-primary);
}

.code-lang-input {
  width: 100%;
  padding: 8px 12px;
  margin-bottom: 10px;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--color-text);
  background-color: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.code-lang-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.code-insert-textarea {
  min-height: 180px;
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
  padding: 6px 16px;
  background: var(--color-hover);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  font-size: 13px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.math-dialog-cancel:hover {
  background: var(--color-border);
}

.math-dialog-save {
  padding: 6px 16px;
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.math-dialog-save:hover {
  background-color: var(--color-primary-dark);
}

.toc-panel {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 232px;
  max-height: calc(100% - 24px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--surface-panel-strong, var(--color-white));
  border: 1px solid var(--panel-border-strong, var(--color-border));
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 100;
}

.toc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 8px 8px 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  border-bottom: 1px solid var(--panel-border, var(--color-border));
  flex-shrink: 0;
}

.toc-close-btn {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--color-text-light);
}

.toc-close-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.toc-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 6px;
}

.toc-item {
  display: flex;
  align-items: flex-start;
  gap: 4px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  line-height: 1.4;
  color: var(--color-text);
  cursor: pointer;
  white-space: normal;
  overflow-wrap: break-word;
  word-break: break-word;
}

.toc-item:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.toc-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 16px;
  height: 18px;
  margin-top: 1px;
  padding: 0;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.toc-toggle:focus-visible,
.toc-link:focus-visible {
  outline: 2px solid var(--color-text);
  outline-offset: 1px;
}
.toc-link {
  flex: 1 1 auto;
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  line-height: inherit;
  text-align: left;
  cursor: pointer;
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
  padding: 18px 12px;
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

/* 工作台右栏 = max(320, innerWidth/2)：≤1360 时 9 个 64px 文字入口已放不下，
   收成 36px 图标态（不是只隐藏文字留空盒）。 */
@container (max-width: 660px) {
  .zen-note-toolbar .toolbar-primary-label { display: none; }
  .zen-note-toolbar .toolbar-primary-btn {
    width: 36px !important;
    min-width: 36px !important;
    padding: 0 !important;
    gap: 0;
    justify-content: center;
  }
}

@media (max-width: 1360px) {
  .zen-note-toolbar .toolbar-primary-label { display: none; }
  .zen-note-toolbar .toolbar-primary-btn {
    width: 36px !important;
    min-width: 36px !important;
    padding: 0 !important;
    gap: 0;
    justify-content: center;
  }
}

@media (max-width: 767px) {
  .zen-note-toolbar {
    padding: 6px 8px;
  }

  .zen-note-header {
    padding: 6px 8px;
  }
}

@media (max-width: 400px) {
  .zen-note-toolbar .toolbar-btn,
  .zen-note-toolbar .toolbar-primary-btn {
    width: 30px !important;
    min-width: 30px !important;
    height: 30px !important;
  }
  .zen-note-toolbar { gap: 2px; }
}
@media (max-width: 365px) {
  .zen-note-toolbar .toolbar-btn,
  .zen-note-toolbar .toolbar-primary-btn {
    width: 27px !important;
    min-width: 27px !important;
    height: 27px !important;
  }
  .zen-note-toolbar { gap: 1px; }
}
</style>
