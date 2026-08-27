<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div v-if="media" class="media-lightbox" @click.self="emit('close')" @wheel="onWheel">
      <!-- Image: zoom + pan -->
      <template v-if="kind === 'image'">
        <div class="lightbox-stage" @dblclick="toggleZoom">
          <img
            ref="imgEl"
            :src="url"
            :alt="media.name"
            class="lightbox-img"
            :style="imgStyle"
            @pointerdown="onPointerDown"
            @pointermove="onPointerMove"
            @pointerup="onPointerUp"
            @pointercancel="onPointerUp"
            @contextmenu.prevent
            draggable="false"
          />
        </div>
        <div class="lightbox-toolbar">
          <button class="lb-btn" title="缩小" @click="zoomStep(-0.5)">−</button>
          <span class="lb-zoom-label">{{ Math.round(scale * 100) }}%</span>
          <button class="lb-btn" title="放大" @click="zoomStep(0.5)">＋</button>
          <button class="lb-btn" title="适应窗口" @click="resetTransform">⤢</button>
        </div>
      </template>
      <!-- Video: enlarged player -->
      <template v-else>
        <div class="lightbox-video-stage">
          <video :src="url" class="lightbox-video" controls autoplay playsinline></video>
        </div>
      </template>

      <button class="lightbox-close" title="关闭 (Esc)" @click="emit('close')">×</button>
      <button class="lightbox-download" title="下载" @click="emit('download', media)">⬇</button>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import type { FileAttachment } from '@/types'

const props = defineProps<{
  media: FileAttachment | null
  kind: 'image' | 'video'
  url: string
}>()

const emit = defineEmits<{
  close: []
  download: [file: FileAttachment]
}>()

const MIN_SCALE = 0.4
const MAX_SCALE = 6
const DEFAULT_SCALE = 1

const imgEl = ref<HTMLImageElement | null>(null)
const state = reactive({ scale: DEFAULT_SCALE, tx: 0, ty: 0 })
const drag = reactive({ active: false, startX: 0, startY: 0, origTx: 0, origTy: 0 })

const scale = computed(() => state.scale)

const imgStyle = computed(() => ({
  transform: `translate(${state.tx}px, ${state.ty}px) scale(${state.scale})`,
  cursor: drag.active ? 'grabbing' : 'grab',
  // 拖拽平移期间关闭过渡（否则 50ms 平滑滞后）；结束恢复。
  transition: drag.active ? 'none' : 'transform 0.05s linear',
}))

function resetTransform() {
  state.scale = DEFAULT_SCALE
  state.tx = 0
  state.ty = 0
}

function clampScale(v: number) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, v))
}

function zoomAt(centerX: number, centerY: number, factor: number) {
  const next = clampScale(state.scale * factor)
  const k = next / state.scale
  state.tx = centerX - (centerX - state.tx) * k
  state.ty = centerY - (centerY - state.ty) * k
  state.scale = next
}

function zoomStep(delta: number) {
  // 对称缩放：+0.5 → ×1.5，−0.5 → ÷1.5，加减往返可还原。
  const el = imgEl.value
  const rect = el?.getBoundingClientRect()
  const cx = rect ? rect.left + rect.width / 2 : window.innerWidth / 2
  const cy = rect ? rect.top + rect.height / 2 : window.innerHeight / 2
  const factor = delta >= 0 ? 1 + delta : 1 / (1 - delta)
  zoomAt(cx, cy, factor)
}

function toggleZoom() {
  if (state.scale > DEFAULT_SCALE + 0.01) resetTransform()
  else zoomStep(0.5)
}

function onWheel(e: WheelEvent) {
  e.preventDefault()
  if (props.kind === 'video' || !imgEl.value) return
  const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15
  zoomAt(e.clientX, e.clientY, factor)
}

function onPointerDown(e: PointerEvent) {
  if (!imgEl.value) return
  drag.active = true
  drag.startX = e.clientX
  drag.startY = e.clientY
  drag.origTx = state.tx
  drag.origTy = state.ty
  imgEl.value.setPointerCapture(e.pointerId)
}

function onPointerMove(e: PointerEvent) {
  if (!drag.active) return
  state.tx = drag.origTx + (e.clientX - drag.startX)
  state.ty = drag.origTy + (e.clientY - drag.startY)
}

function onPointerUp() {
  drag.active = false
  // 缩放回到 1 时不保留平移：图片必须回到舞台居中，防止被拖出可视区。
  if (state.scale <= DEFAULT_SCALE + 0.01) {
    state.tx = 0
    state.ty = 0
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}

watch(() => props.media, (m) => {
  if (m) resetTransform()
})

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.media-lightbox {
  position: fixed;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.88);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 56px 24px;
  overflow: hidden;
}

.lightbox-stage {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  overflow: hidden;
  touch-action: none;
}

.lightbox-img {
  max-width: 90vw;
  max-height: 88vh;
  border-radius: var(--radius-md);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  user-select: none;
  -webkit-user-drag: none;
  will-change: transform;
}

/* 放大播放区：固定大画幅（16:9 区域），不依赖视频文件自身元数据尺寸 —
   缺元数据的视频在 <video> 默认 300×150 下也不会缩小。 */
.lightbox-video-stage {
  width: min(86vw, 1200px);
  height: min(82vh, 675px);
  display: flex;
  align-items: center;
  justify-content: center;
}

.lightbox-video {
  width: 100%;
  height: 100%;
  border-radius: var(--radius-md);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  background: #000;
  object-fit: contain;
}

.lightbox-toolbar {
  position: absolute;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-radius: 24px;
  background: rgba(255, 255, 255, 0.12);
  backdrop-filter: blur(6px);
}

.lb-btn {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.16);
  color: #fff;
  font-size: 18px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background var(--transition-fast);
}

.lb-btn:hover {
  background: rgba(255, 255, 255, 0.32);
}

.lb-zoom-label {
  min-width: 48px;
  text-align: center;
  color: rgba(255, 255, 255, 0.9);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.lightbox-close {
  position: absolute;
  top: 16px;
  right: 16px;
  width: 38px;
  height: 38px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.15);
  color: white;
  font-size: 24px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.lightbox-close:hover {
  background: rgba(255, 255, 255, 0.3);
}

.lightbox-download {
  position: absolute;
  top: 16px;
  right: 62px;
  width: 38px;
  height: 38px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.15);
  color: white;
  font-size: 18px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.lightbox-download:hover {
  background: rgba(255, 255, 255, 0.3);
}
</style>
