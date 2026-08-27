<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div v-if="userMessages.length > 0" class="conversation-timeline" :class="{ 'zen-timeline': zen }" ref="timelineRef" @wheel.prevent="onTimelineWheel">
    <div class="timeline-track" ref="trackRef" :style="trackTransformStyle">
      <div class="timeline-line"></div>
      <div
        v-for="(msg, idx) in userMessages"
        :key="msg.id"
        class="timeline-point"
        :class="{ active: activeIdx === idx, hovered: hoveredIdx === idx }"
        :data-timeline-idx="idx"
        @mouseenter="onPointEnter(idx, msg)"
        @mouseleave="onPointLeave"
        @click="scrollToMessage(msg.id, idx)"
      >
        <div class="point-dot"></div>
      </div>
    </div>
    <div v-if="needsScroll" class="timeline-scrollbar" ref="scrollbarRef" @mousedown="onScrollbarMouseDown">
      <div class="scrollbar-knob" :style="knobStyle" ref="knobRef"></div>
    </div>
  </div>

  <!-- Preview tooltip teleported to body to avoid clipping by overflow container -->
  <Teleport to="body">
    <Transition name="preview">
      <div
        v-if="hoveredIdx !== null && previewMessage"
        class="timeline-tooltip"
        :style="previewStyle"
      >
        <div class="tooltip-arrow"></div>
        <div class="tooltip-content">{{ getPreviewText(previewMessage.content) }}</div>
        <div class="tooltip-time">{{ formatTime(previewMessage.created_at) }}</div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useChatStore } from '@/stores/chat'
import type { Message } from '@/types'

const emit = defineEmits<{ (e: 'jump-to-message', messageId: string): void }>()

const props = defineProps<{ zen?: boolean }>()

const chatStore = useChatStore()
const timelineRef = ref<HTMLDivElement | null>(null)
const trackRef = ref<HTMLDivElement | null>(null)
const scrollbarRef = ref<HTMLDivElement | null>(null)
const knobRef = ref<HTMLDivElement | null>(null)
const hoveredIdx = ref<number | null>(null)
const activeIdx = ref<number | null>(null)
const jumpTargetIdx = ref<number | null>(null)
const previewMessage = ref<Message | null>(null)
const scrollOffset = ref(0)

const trackTransformStyle = computed(() => {
  if (!needsScroll.value || scrollOffset.value === 0) return {}
  return { transform: `translateY(-${scrollOffset.value}px)` }
})

const userMessages = computed(() => {
  return chatStore.currentMessages.filter(m => m.role === 'user')
})

const POINT_HEIGHT = 44
const TIMELINE_MAX_HEIGHT = '50vh'
const TIMELINE_MAX_HEIGHT_PX = typeof window !== 'undefined' ? window.innerHeight * 0.5 : 500

const contentHeight = computed(() => {
  if (userMessages.value.length === 0) return 0
  return userMessages.value.length * POINT_HEIGHT + 20
})

const needsScroll = computed(() => {
  return contentHeight.value > TIMELINE_MAX_HEIGHT_PX
})

const maxScrollOffset = computed(() => {
  if (!needsScroll.value) return 0
  return Math.max(0, contentHeight.value - TIMELINE_MAX_HEIGHT_PX)
})

const knobStyle = computed(() => {
  if (!needsScroll.value) return {}
  const ratio = TIMELINE_MAX_HEIGHT_PX / contentHeight.value
  const knobHeight = Math.max(20, TIMELINE_MAX_HEIGHT_PX * ratio)
  const knobTop = (scrollOffset.value / maxScrollOffset.value) * (TIMELINE_MAX_HEIGHT_PX - knobHeight)
  return {
    height: `${knobHeight}px`,
    top: `${knobTop}px`,
  }
})

function onTimelineWheel(e: WheelEvent) {
  if (!needsScroll.value) return
  const delta = e.deltaY > 0 ? 30 : -30
  scrollOffset.value = Math.max(0, Math.min(maxScrollOffset.value, scrollOffset.value + delta))
}

let isDraggingKnob = false
let dragStartY = 0
let dragStartOffset = 0

function onScrollbarMouseDown(e: MouseEvent) {
  const knob = knobRef.value
  if (!knob) return
  if (knob.contains(e.target as Node)) {
    isDraggingKnob = true
    dragStartY = e.clientY
    dragStartOffset = scrollOffset.value
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'grabbing'
    e.preventDefault()
    return
  }
  const scrollbar = scrollbarRef.value
  if (!scrollbar) return
  const rect = scrollbar.getBoundingClientRect()
  const clickRatio = (e.clientY - rect.top) / rect.height
  scrollOffset.value = Math.max(0, Math.min(maxScrollOffset.value, clickRatio * contentHeight.value - TIMELINE_MAX_HEIGHT_PX / 2))
}

function onMouseMove(e: MouseEvent) {
  if (!isDraggingKnob) return
  const ratio = TIMELINE_MAX_HEIGHT_PX / contentHeight.value
  const knobHeight = Math.max(20, TIMELINE_MAX_HEIGHT_PX * ratio)
  const trackHeight = TIMELINE_MAX_HEIGHT_PX - knobHeight
  const deltaY = e.clientY - dragStartY
  const deltaOffset = (deltaY / trackHeight) * maxScrollOffset.value
  scrollOffset.value = Math.max(0, Math.min(maxScrollOffset.value, dragStartOffset + deltaOffset))
}

function onMouseUp() {
  if (!isDraggingKnob) return
  isDraggingKnob = false
  document.body.style.userSelect = ''
  document.body.style.cursor = ''
}

onMounted(() => {
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
})

onUnmounted(() => {
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
})

function getPreviewText(content: string): string {
  const stripped = content
    .replace(/\[note-ref:[^\]]*\]\n[\s\S]*?\n\[\/note-ref\]\n*/g, '')
    .replace(/\[file-ref:[^\]]*\]\n[\s\S]*?\n\[\/file-ref\]\n*/g, '')
    .trim()
  return stripped.length > 120 ? stripped.slice(0, 120) + '...' : stripped
}

function formatTime(iso: string): string {
  try {
    const normalized = iso.endsWith('Z') || iso.includes('+') || iso.includes('-', 10) ? iso : iso + 'Z'
    const d = new Date(normalized)
    const month = d.getMonth() + 1
    const day = d.getDate()
    const h = d.getHours().toString().padStart(2, '0')
    const min = d.getMinutes().toString().padStart(2, '0')
    return `${month}/${day} ${h}:${min}`
  } catch {
    return ''
  }
}

const previewStyle = computed(() => {
  if (hoveredIdx.value === null || !timelineRef.value) return {}
  const point = timelineRef.value.querySelector(`[data-timeline-idx="${hoveredIdx.value}"]`)
  if (!point) return {}
  const rect = point.getBoundingClientRect()
  if (props.zen) {
    const panel = timelineRef.value.closest('.zen-left-panel') as HTMLElement | null
    if (panel) {
      return {
        position: 'fixed',
        right: `${window.innerWidth - rect.left + 12}px`,
        top: `${rect.top + rect.height / 2}px`,
        transform: 'translateY(-50%)',
        zIndex: '10001',
      }
    }
  }
  return {
    position: 'fixed',
    right: `${window.innerWidth - rect.left + 12}px`,
    top: `${rect.top + rect.height / 2}px`,
    transform: 'translateY(-50%)',
    zIndex: '10001',
  }
})

function onPointEnter(idx: number, msg: Message) {
  hoveredIdx.value = idx
  previewMessage.value = msg
}

function onPointLeave() {
  hoveredIdx.value = null
  previewMessage.value = null
}

function scrollToMessage(messageId: string, idx: number) {
  activeIdx.value = idx
  jumpTargetIdx.value = idx
  scrollTimelineToActive()
  emit('jump-to-message', messageId)
}

let lastScrollTop = 0
let scrollDirection: 'up' | 'down' = 'down'
let rafId: number | null = null

function detectScrollDirection() {
  const messageList = document.querySelector('.message-list')
  if (!messageList) return
  const currentScrollTop = messageList.scrollTop
  scrollDirection = currentScrollTop > lastScrollTop ? 'down' : 'up'
  lastScrollTop = currentScrollTop
}

function updateActiveIndex() {
  const msgs = userMessages.value
  if (msgs.length === 0) {
    activeIdx.value = null
    return
  }

  const messageList = document.querySelector('.message-list')
  if (!messageList) return

  const listRect = messageList.getBoundingClientRect()
  const viewportTop = listRect.top
  const viewportBottom = listRect.bottom
  const viewportHeight = listRect.height
  const viewportCenter = viewportTop + viewportHeight / 2

  const renderedEls = messageList.querySelectorAll('[data-message-id]')
  const elMap = new Map<string, HTMLElement>()
  renderedEls.forEach((el) => {
    const id = (el as HTMLElement).dataset.messageId
    if (id) elMap.set(id, el as HTMLElement)
  })

  type MsgInfo = { idx: number; elTop: number; elBottom: number; elCenter: number; visibleRatio: number }
  const msgInfos: MsgInfo[] = []

  for (let i = 0; i < msgs.length; i++) {
    const el = elMap.get(msgs[i].id)
    if (!el) continue
    const rect = el.getBoundingClientRect()
    const elTop = rect.top
    const elBottom = rect.bottom
    const elHeight = rect.height || 1
    const elCenter = elTop + elHeight / 2
    const visibleHeight = Math.max(0, Math.min(elBottom, viewportBottom) - Math.max(elTop, viewportTop))
    const visibleRatio = Math.min(1, visibleHeight / elHeight)

    msgInfos.push({ idx: i, elTop, elBottom, elCenter, visibleRatio })
  }

  if (msgInfos.length === 0) return

  // If we just jumped to a specific timeline point, keep it active once it enters the viewport.
  if (jumpTargetIdx.value !== null) {
    const targetMsg = msgs[jumpTargetIdx.value]
    if (targetMsg) {
      const targetEl = elMap.get(targetMsg.id)
      if (targetEl) {
        const rect = targetEl.getBoundingClientRect()
        const elTop = rect.top
        const elBottom = rect.bottom
        const elHeight = rect.height || 1
        const visibleHeight = Math.max(0, Math.min(elBottom, viewportBottom) - Math.max(elTop, viewportTop))
        const visibleRatio = Math.min(1, visibleHeight / elHeight)
        if (visibleRatio > 0) {
          activeIdx.value = jumpTargetIdx.value
          jumpTargetIdx.value = null
          scrollTimelineToActive()
          return
        }
      }
    }
  }

  const mostlyVisible = msgInfos.filter(m => m.visibleRatio >= 0.5)
  if (mostlyVisible.length > 0) {
    if (mostlyVisible.length === 1) {
      activeIdx.value = mostlyVisible[0].idx
    } else {
      mostlyVisible.sort((a, b) => a.elTop - b.elTop)
      activeIdx.value = scrollDirection === 'down' ? mostlyVisible[mostlyVisible.length - 1].idx : mostlyVisible[0].idx
    }
    scrollTimelineToActive()
    return
  }

  const partiallyVisible = msgInfos.filter(m => m.visibleRatio > 0)
  if (partiallyVisible.length > 0) {
    partiallyVisible.sort((a, b) => b.visibleRatio - a.visibleRatio)
    activeIdx.value = partiallyVisible[0].idx
    scrollTimelineToActive()
    return
  }

  msgInfos.sort((a, b) => a.elCenter - b.elCenter)

  let betweenIdx = 0
  for (let i = 0; i < msgInfos.length - 1; i++) {
    const curr = msgInfos[i]
    const next = msgInfos[i + 1]
    if (viewportCenter >= curr.elCenter && viewportCenter <= next.elCenter) {
      const distToCurr = Math.abs(viewportCenter - curr.elCenter)
      const distToNext = Math.abs(viewportCenter - next.elCenter)
      if (distToCurr === distToNext) {
        betweenIdx = scrollDirection === 'down' ? next.idx : curr.idx
      } else {
        betweenIdx = distToCurr < distToNext ? curr.idx : next.idx
      }
      activeIdx.value = betweenIdx
      scrollTimelineToActive()
      return
    }
  }

  if (viewportCenter < msgInfos[0].elCenter) {
    activeIdx.value = msgInfos[0].idx
  } else {
    activeIdx.value = msgInfos[msgInfos.length - 1].idx
  }
  scrollTimelineToActive()
}

function scrollTimelineToActive() {
  if (activeIdx.value === null || !needsScroll.value) return
  if (isDraggingKnob || hoveredIdx.value !== null) return
  const pointTop = activeIdx.value * POINT_HEIGHT + 20
  const pointBottom = pointTop + POINT_HEIGHT
  const visibleTop = scrollOffset.value
  const visibleBottom = scrollOffset.value + TIMELINE_MAX_HEIGHT_PX
  const margin = POINT_HEIGHT
  if (pointTop < visibleTop + margin || pointBottom > visibleBottom - margin) {
    const target = pointTop - TIMELINE_MAX_HEIGHT_PX / 2 + POINT_HEIGHT / 2
    scrollOffset.value = Math.max(0, Math.min(maxScrollOffset.value, target))
  }
}

let scrollHandler: (() => void) | null = null
let observer: IntersectionObserver | null = null
let mutationObserver: MutationObserver | null = null

function observeUserMessages() {
  if (!observer) return
  const messageList = document.querySelector('.message-list')
  if (!messageList) return
  const rendered = messageList.querySelectorAll('[data-message-id]')
  rendered.forEach((el) => {
    observer?.observe(el)
  })
}

function scheduleUpdate() {
  if (rafId !== null) return
  rafId = requestAnimationFrame(() => {
    detectScrollDirection()
    observeUserMessages()
    updateActiveIndex()
    rafId = null
  })
}

onMounted(() => {
  const messageList = document.querySelector('.message-list')

  scrollHandler = () => {
    scheduleUpdate()
  }

  if (messageList) {
    messageList.addEventListener('scroll', scrollHandler, { passive: true })
    lastScrollTop = messageList.scrollTop
  }

  observer = new IntersectionObserver(
    () => {
      scheduleUpdate()
    },
    {
      root: messageList,
      threshold: [0, 0.1, 0.25, 0.5, 0.75, 1],
    }
  )

  mutationObserver = new MutationObserver(() => {
    scheduleUpdate()
  })
  if (messageList) {
    mutationObserver.observe(messageList, { childList: true, subtree: true })
  }

  nextTick(() => {
    observeUserMessages()
    updateActiveIndex()
  })
})

onUnmounted(() => {
  if (rafId !== null) {
    cancelAnimationFrame(rafId)
    rafId = null
  }
  if (scrollHandler) {
    const messageList = document.querySelector('.message-list')
    if (messageList) {
      messageList.removeEventListener('scroll', scrollHandler)
    }
  }
  if (observer) {
    observer.disconnect()
  }
  if (mutationObserver) {
    mutationObserver.disconnect()
  }
})

watch(() => chatStore.currentMessages.length, () => {
  nextTick(() => {
    observeUserMessages()
    updateActiveIndex()
  })
})
</script>

<style scoped>
.conversation-timeline {
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: stretch;
  z-index: 100;
  max-height: 50vh;
  overflow: hidden;
}

.timeline-track {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
  padding: 20px 8px;
  position: relative;
  height: fit-content;
  transition: transform 0.1s ease-out;
}

.timeline-scrollbar {
  width: 6px;
  background-color: transparent;
  border-radius: 3px;
  position: relative;
  margin: 20px 0;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.2s ease;
}

.conversation-timeline:hover .timeline-scrollbar {
  opacity: 1;
}

.scrollbar-knob {
  position: absolute;
  left: 0;
  right: 0;
  background-color: var(--color-text-light);
  border-radius: 3px;
  opacity: 0.4;
  transition: opacity 0.15s ease, background-color 0.15s ease;
  min-height: 20px;
  cursor: grab;
}

.scrollbar-knob:hover,
.scrollbar-knob:active {
  opacity: 0.8;
  background-color: var(--color-primary);
}

.timeline-line {
  position: absolute;
  top: 20px;
  bottom: 20px;
  left: calc(50% + 0px);
  width: 2px;
  background-color: var(--color-border);
  transform: translateX(-50%);
  z-index: 0;
}

.timeline-point {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  cursor: pointer;
  flex-shrink: 0;
}

.point-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: var(--color-text-light);
  border: 2px solid var(--surface-workbench);
  transition: all 0.2s ease;
  opacity: 0.5;
}

.timeline-point:hover .point-dot,
.timeline-point.active .point-dot {
  background-color: var(--color-primary);
  opacity: 1;
  transform: scale(1.3);
}

.timeline-point.active .point-dot {
  width: 10px;
  height: 10px;
  box-shadow: 0 0 0 3px rgba(53, 133, 197, 0.2);
}

@media (max-width: 767px) {
  .conversation-timeline {
    display: none;
  }
}

.zen-timeline {
  position: absolute;
  right: 12px;
}
</style>

<style>
.timeline-tooltip {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  padding: 10px 14px;
  max-width: 320px;
  min-width: 120px;
  box-shadow: var(--shadow-md);
  pointer-events: none;
}

.tooltip-arrow {
  position: absolute;
  right: -6px;
  top: 50%;
  transform: translateY(-50%);
  width: 0;
  height: 0;
  border-top: 6px solid transparent;
  border-bottom: 6px solid transparent;
  border-left: 6px solid var(--surface-panel-strong);
}

.tooltip-content {
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text);
  word-break: break-word;
}

.tooltip-time {
  font-size: 11px;
  color: var(--color-text-light);
  margin-top: 4px;
}

.preview-enter-active,
.preview-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}

.preview-enter-from,
.preview-leave-to {
  opacity: 0;
  transform: translateY(-50%) translateX(4px);
}
</style>
