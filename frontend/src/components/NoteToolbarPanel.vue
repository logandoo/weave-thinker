<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div
      v-if="modelValue"
      ref="panelRef"
      class="note-toolbar-panel"
      :data-menu="menuId"
      role="dialog"
      :aria-label="title"
      :style="panelStyle"
      @click="onPanelClick"
    >
      <div class="note-toolbar-panel-head">
        <strong>{{ title }}</strong>
        <button class="note-toolbar-panel-close" type="button" aria-label="关闭菜单" @click="close(true)">×</button>
      </div>
      <div class="note-toolbar-panel-body">
        <slot />
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from 'vue'

const props = defineProps<{
  modelValue: boolean
  menuId: string
  title: string
  anchor?: HTMLElement | null
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const panelRef = ref<HTMLElement | null>(null)
const panelStyle = ref<CSSProperties>({})
// Last interaction modality (capture phase, module-less but component-scoped).
// Pointer-opened menus must not paint a focus ring on the first row; keyboard
// opens MUST keep one (WCAG 2.4.7) — the two cases are not the same.
let lastInputWasPointer = true

function refreshPosition(force = false) {
  if (typeof window === 'undefined') return
  if (!props.modelValue && !force) return
  if (window.innerWidth <= 767) {
    panelStyle.value = { left: '8px', right: '8px', bottom: '8px', top: 'auto', maxHeight: '72vh' }
    return
  }
  const anchor = props.anchor
  const panel = panelRef.value
  if (!anchor) return
  const rect = anchor.getBoundingClientRect()
  const width = panel?.offsetWidth || 172
  const preferredHeight = panel?.offsetHeight || 420
  const below = window.innerHeight - rect.bottom - 12
  const above = rect.top - 12
  const useBelow = below >= preferredHeight || below >= above
  const maxHeight = Math.max(140, Math.min(preferredHeight, useBelow ? below : above))
  const height = Math.min(preferredHeight, maxHeight)
  panelStyle.value = {
    left: `${Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))}px`,
    top: `${useBelow
      ? Math.min(rect.bottom + 7, window.innerHeight - height - 12)
      : Math.max(12, rect.top - height - 7)}px`,
    width: `${width}px`,
    maxHeight: `${maxHeight}px`,
  }
}

function onWindowResize() {
  if (props.modelValue) refreshPosition(true)
}

function close(restoreFocus = false) {
  emit('update:modelValue', false)
  if (restoreFocus) nextTick(() => props.anchor?.focus())
}

function focusables(): HTMLElement[] {
  const root = panelRef.value
  if (!root) return []
  return Array.from(root.querySelectorAll<HTMLElement>('button, [href], [tabindex]:not([tabindex="-1"])'))
    .filter((el) => el.offsetParent !== null)
}

function onPanelClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (!target.closest('button')) return
  // Actions may synchronously open a picker/dialog anchored to the clicked
  // button. Close the panel on the next task so that the anchor rect is still
  // measurable while the handler runs.
  window.setTimeout(() => {
    emit('update:modelValue', false)
    // Keyboard users must not be dropped on <body> after running a command.
    // If the command moved focus (find bar, dialog), leave it alone.
    nextTick(() => {
      const active = document.activeElement
      if (!active || active === document.body) props.anchor?.focus()
    })
  }, 0)
}

function onDocumentPointerDown(event: PointerEvent) {
  lastInputWasPointer = true
  if (!props.modelValue) return
  panelRef.value?.classList.remove('no-focus-ring')
  panelRef.value?.classList.remove('keyboard-open')
  const target = event.target as Node
  if (panelRef.value?.contains(target) || props.anchor?.contains(target)) return
  close(false)
}

function onDocumentKeydown(event: KeyboardEvent) {
  lastInputWasPointer = false
  if (!props.modelValue) return
  panelRef.value?.classList.remove('no-focus-ring')
  if (event.key === 'Escape') {
    event.preventDefault()
    close(true)
    return
  }
  if (event.key === 'Tab') {
    close(true)
    return
  }
  if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const items = focusables()
  const current = items.indexOf(document.activeElement as HTMLElement)
  if (current < 0 || items.length === 0) return
  event.preventDefault()
  let next = current
  if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = (current + 1) % items.length
  if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = (current - 1 + items.length) % items.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = items.length - 1
  items[next]?.focus()
}

watch(() => props.modelValue, async (open) => {
  if (!open) return
  // Clear any width/position captured before the previous close; otherwise a
  // resize while closed bakes 560px (then 210px after clamping) into every
  // later open.
  panelStyle.value = {}
  await nextTick()
  refreshPosition(true)
  requestAnimationFrame(() => refreshPosition(true))
  // Prefer the first text action over structural widgets (e.g. the 6×6 table
  // grid), otherwise the insert menu opens on a 1×1 table cell.
  const firstAction = panelRef.value?.querySelector<HTMLElement>('.note-toolbar-menu-item')
  const keyboardOpen = !lastInputWasPointer
  panelRef.value?.classList.toggle('no-focus-ring', !keyboardOpen)
  panelRef.value?.classList.toggle('keyboard-open', keyboardOpen)
  ;(firstAction || focusables()[1])?.focus()
})

onMounted(() => {
  document.addEventListener('pointerdown', onDocumentPointerDown, true)
  document.addEventListener('keydown', onDocumentKeydown, true)
  window.addEventListener('resize', onWindowResize)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown, true)
  document.removeEventListener('keydown', onDocumentKeydown, true)
  window.removeEventListener('resize', onWindowResize)
})
</script>

<style>
.note-toolbar-panel {
  position: fixed;
  z-index: 10000;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  width: max-content;
  min-width: 172px;
  max-width: min(210px, calc(100vw - 24px));
  border: 1px solid var(--panel-border-strong, rgba(0, 0, 0, .25));
  border-radius: var(--radius-md, 12px);
  background: var(--surface-panel-strong, var(--color-white, #fff));
  box-shadow: var(--shadow-lg, 0 18px 45px rgba(0, 0, 0, .18));
  color: var(--color-text, #222);
  font-size: 13px;
}
.note-toolbar-panel-head {
  display: flex;
  align-items: center;
  min-height: 38px;
  padding: 3px 6px 3px 12px;
  border-bottom: 1px solid var(--panel-border, rgba(0, 0, 0, .12));
  background: var(--surface-panel-subtle, var(--surface-panel-strong, var(--color-bg-secondary, #f5f5f5)));
}
.note-toolbar-panel-head strong { margin-right: auto; font-size: 13px; font-weight: 600; }
.note-toolbar-panel-close {
  width: 28px;
  height: 28px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm, 8px);
  background: transparent;
  color: var(--color-text-light, inherit);
  font-size: 18px;
  line-height: 1;
}
.note-toolbar-panel-close:hover,
.note-toolbar-panel-close:focus-visible {
  border-color: var(--panel-border-strong, rgba(0, 0, 0, .24));
  background: var(--color-hover, var(--color-white, #fff));
  color: var(--color-text, inherit);
}
.note-toolbar-panel-body {
  min-height: 0;
  overflow: auto;
  padding: 6px;
}
.note-toolbar-group-label {
  margin: 8px 8px 2px;
  color: var(--color-text-light, #777);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .02em;
}
.note-toolbar-group-label:first-child { margin-top: 2px; }
/* 单列紧凑菜单：每个动作一行，行高压到 34px，避免两列网格留下的空洞 */
.note-toolbar-menu-grid {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.note-toolbar-menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 32px;
  padding: 5px 9px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm, 8px);
  background: transparent;
  color: inherit;
  font-size: 13px;
  text-align: left;
  white-space: nowrap;
}
.note-toolbar-menu-item:focus { outline: none; }
.note-toolbar-menu-item:focus-visible {
  outline: 2px solid var(--color-text, #222);
  outline-offset: -2px;
}
.note-toolbar-panel.no-focus-ring .note-toolbar-menu-item:focus-visible { outline: none; }
.note-toolbar-panel.keyboard-open .note-toolbar-menu-item:focus {
  outline: 2px solid var(--color-text, #222);
  outline-offset: -2px;
}
.note-toolbar-menu-item:hover {
  background: var(--color-hover, var(--color-bg-secondary, #f0f0f0));
}
.note-toolbar-menu-item.active {
  background: var(--color-hover, var(--color-bg-secondary, #f0f0f0));
  font-weight: 600;
}
.note-toolbar-menu-item svg {
  width: 18px;
  height: 18px;
  flex: 0 0 auto;
}
.note-toolbar-menu-item .heading-icon {
  width: 18px;
  height: 18px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border: 1px solid currentColor;
  border-radius: 3px;
  font-size: 10px;
  font-weight: 700;
}
.note-toolbar-table-grid {
  display: grid;
  grid-template-columns: repeat(6, 24px);
  gap: 3px;
  width: max-content;
  margin: 4px 8px 8px;
}
.note-toolbar-table-cell {
  width: 24px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--panel-border-strong, rgba(0, 0, 0, .25));
  border-radius: var(--radius-sm, 4px);
  background: transparent;
}
.note-toolbar-table-cell:hover,
.note-toolbar-table-cell.active {
  background: var(--color-primary, #7aa35a);
  border-color: var(--color-primary, #7aa35a);
}
@media (max-width: 767px) {
  .note-toolbar-panel { width: auto; max-width: none; box-shadow: 0 -8px 28px rgba(0, 0, 0, .18); }
  .note-toolbar-panel-body { max-height: calc(72vh - 40px); padding: 8px; }
  .note-toolbar-menu-item { min-height: 40px; white-space: normal; }
  .note-toolbar-table-grid { grid-template-columns: repeat(6, 28px); }
  .note-toolbar-table-cell { width: 28px; height: 28px; }
}
@media (prefers-reduced-motion: reduce) {
  .note-toolbar-panel,
  .note-toolbar-menu-item,
  .note-toolbar-table-cell { transition: none; }
}
</style>
