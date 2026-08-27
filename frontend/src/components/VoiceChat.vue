<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="voice-page" :class="`voice-state-${state}`">
    <!-- Header: icon-only controls (no text) -->
    <header class="voice-header">
      <button class="voice-icon-btn" @click="exitVoice" title="退出语音助理" aria-label="退出">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
        </svg>
      </button>
      <div class="voice-header-right">
        <button class="voice-icon-btn" @click="toggleSessionPanel" title="语音会话" aria-label="语音会话">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M3 12a9 9 0 1 0 9-9"/><polyline points="3 4 3 10 9 10"/>
          </svg>
        </button>
        <button class="voice-icon-btn" :class="{ off: !micOn }" @click="toggleMic" :title="micOn ? '关闭麦克风' : '开启麦克风'" :aria-label="micOn ? '关闭麦克风' : '开启麦克风'">
          <svg v-if="micOn" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/>
          </svg>
          <svg v-else width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="1" y1="1" x2="23" y2="23"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"/><path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23"/><line x1="12" y1="19" x2="12" y2="23"/>
          </svg>
        </button>
      </div>
    </header>

    <!-- Session selector panel -->
    <Transition name="slide-panel">
      <div v-if="showSessionPanel" class="session-panel">
        <div class="session-panel-header">
          <span class="session-panel-title">语音会话</span>
          <button class="voice-icon-btn session-new-btn" @click="startNewSession" title="新语音对话" aria-label="新语音对话">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
          </button>
        </div>
        <div class="session-list">
          <button
            v-for="s in sessions"
            :key="s.id"
            class="session-item"
            :class="{ active: s.id === currentSessionId }"
            @click="switchSession(s.id)"
          >
            <span class="session-item-title">{{ s.title }}</span>
            <span class="session-item-time">{{ formatTime(s.updated_at) }}</span>
          </button>
          <div v-if="sessions.length === 0" class="session-empty">暂无语音会话</div>
        </div>
      </div>
    </Transition>
    <Transition name="fade">
      <div v-if="showSessionPanel" class="session-overlay" @click="toggleSessionPanel"></div>
    </Transition>

    <!-- Centered orb stage: voice-only, no text -->
    <div class="voice-stage">
      <div class="voice-orb-wrap">
        <!-- Playback-progress ring: visualises how far the current answer's
             voice playback has actually progressed (tied to the answer text).
             Only shown while the agent is audibly speaking. -->
        <svg v-if="speaking" class="voice-progress-ring" viewBox="0 0 340 340" aria-hidden="true">
          <circle class="voice-progress-track" cx="170" cy="170" :r="RING_R" />
          <circle
            class="voice-progress-fill"
            cx="170"
            cy="170"
            :r="RING_R"
            :style="{ strokeDasharray: RING_CIRC, strokeDashoffset: progressDashoffset }"
          />
        </svg>
        <div class="voice-ripple" v-if="state === 'speak'"></div>
        <div class="voice-ripple delay" v-if="state === 'speak'"></div>
        <!-- Listen-state ripples react to mic level so the user can see their
             voice is being captured. Also shown in dual state (orange): the
             user is speaking over the agent's playback and needs the same
             voice-input feedback. -->
        <div
          class="voice-ripple listen-ripple"
          :class="{ 'dual-ripple': state === 'dual' }"
          v-if="(state === 'listen' || state === 'dual') && micLevel > 0.04"
          :style="{ transform: `scale(${1 + micLevel * 0.8})`, opacity: 0.15 + micLevel * 0.4 }"
        ></div>
        <div class="voice-orb" :class="`orb-${state}`">
          <div class="voice-orb-inner" :style="orbStyle" :data-emotion="emotion"></div>
        </div>
      </div>

      <!-- Interjection overlay: a brief floating text that appears when the
           agent interjects during the user's speech, then fades away. -->
      <Transition name="interjection-fade">
        <div v-if="interjectionText" class="interjection-overlay" :class="`emotion-${emotion}`">
          <span class="interjection-text">{{ interjectionText }}</span>
        </div>
      </Transition>

      <!-- Contextual icon-only actions; absolutely positioned so the orb stays centered -->
      <div class="voice-actions">
        <button v-if="speaking" class="voice-icon-btn voice-action-lg" @click="interrupt" title="打断播报" aria-label="打断">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
        </button>
        <button v-if="state === 'error'" class="voice-icon-btn voice-action-lg" @click="retry" title="重新连接" aria-label="重新连接">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
          </svg>
        </button>
      </div>

      <!-- Error message: shown when the mic cannot be started or the voice
           service fails — otherwise the failure would be silent and the user
           would only see a crossed-out mic icon with no explanation. -->
      <p v-if="state === 'error' && errorMsg" class="voice-error-msg">{{ errorMsg }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useVoiceDuplex } from '@/composables/useVoiceDuplex'
import { useAssistantStore } from '@/stores/assistant'
import { useChatStore } from '@/stores/chat'

const router = useRouter()
const voice = useVoiceDuplex()
const assistantStore = useAssistantStore()
const chatStore = useChatStore()

const state = voice.state
const micOn = voice.micOn
const speaking = voice.speaking
const micLevel = voice.micLevel
const sessions = voice.sessions
const currentSessionId = voice.currentSessionId
const showSessionPanel = voice.showSessionPanel
const playbackProgress = voice.playbackProgress
const emotion = voice.emotion
const interjectionText = voice.interjectionText
const errorMsg = voice.errorMsg

// Progress ring geometry — circumference drives the stroke-dasharray/offset
// that fills the ring in proportion to actual voice-playback progress.
const RING_R = 158
const RING_CIRC = 2 * Math.PI * RING_R
const progressDashoffset = computed(() => RING_CIRC * (1 - playbackProgress.value))

// During listening (or dual — the user talking over playback), scale the orb
// with the real-time mic input level so the user can visually tell whether
// their voice is being recorded. A fast transition keeps it responsive; a
// floor keeps it from going fully flat.
const orbStyle = computed(() => {
  if (state.value !== 'listen' && state.value !== 'dual') return {}
  const scale = 1 + Math.max(0.02, micLevel.value) * 0.32
  return { transform: `scale(${scale})`, transition: 'transform 0.07s ease-out' }
})

function interrupt() {
  voice.interrupt()
}

function toggleMic() {
  voice.toggleMic()
}

async function retry() {
  voice.disconnect()
  try {
    await voice.start(currentSessionId.value)
  } catch { /* error surfaced via state */ }
}

function exitVoice() {
  voice.disconnect()
  // After exiting voice mode, select the 语音助理 assistant and reload its
  // conversations so the sidebar shows only voice sessions (not all
  // assistants' sessions). The voiceAssistantId is provided by the backend
  // in the session event.
  const voiceId = voice.voiceAssistantId.value
  if (voiceId) {
    assistantStore.selectAssistant(voiceId)
    chatStore.loadConversations(voiceId)
  }
  router.push('/')
}

function toggleSessionPanel() {
  showSessionPanel.value = !showSessionPanel.value
  if (showSessionPanel.value) {
    voice.loadSessions()
  }
}

async function startNewSession() {
  await voice.startNewSession()
}

async function switchSession(id: string) {
  await voice.switchSession(id)
}

function formatTime(iso?: string | null): string {
  if (!iso) return ''
  const normalized = iso.endsWith('Z') || iso.includes('+') || iso.includes('-', 10) ? iso : iso + 'Z'
  const d = new Date(normalized)
  const now = new Date()
  const diff = now.getTime() - d.getTime()
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`
  return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

onMounted(async () => {
  try {
    await voice.start()
  } catch { /* error surfaced via state */ }
})
</script>

<style scoped>
.voice-page {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  width: 100vw;
  background:
    radial-gradient(1200px 700px at 50% -10%, color-mix(in srgb, var(--color-primary) 12%, transparent), transparent 60%),
    var(--surface-workbench);
  color: var(--color-text);
  overflow: hidden;
}

.voice-header {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 22px;
}

.voice-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-strong);
  color: var(--color-text);
  cursor: pointer;
  transition: transform var(--transition-fast), background-color var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
}
.voice-icon-btn:hover {
  transform: translateY(-1px);
  border-color: var(--panel-border-strong);
}
.voice-icon-btn.off {
  color: var(--color-error);
  border-color: color-mix(in srgb, var(--color-error) 40%, transparent);
}

/* Centered orb stage */
.voice-stage {
  position: relative;
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  min-height: 0;
}
.voice-orb-wrap {
  position: relative;
  width: 400px;
  height: 400px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.voice-orb {
  width: 300px;
  height: 300px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  z-index: 2;
  transition: transform var(--transition-normal);
}
.voice-orb-inner {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, color-mix(in srgb, var(--color-primary) 85%, white), var(--color-primary-dark));
  box-shadow: 0 0 90px color-mix(in srgb, var(--color-primary) 50%, transparent);
}
.orb-idle .voice-orb-inner,
.orb-connecting .voice-orb-inner {
  background: radial-gradient(circle at 35% 30%, #b9c4b2, #8a9782);
  box-shadow: 0 0 48px rgba(0,0,0,0.1);
  animation: orb-pulse 2.4s ease-in-out infinite;
}
.orb-listen .voice-orb-inner {
  background: radial-gradient(circle at 35% 30%, color-mix(in srgb, var(--color-primary) 80%, white), var(--color-primary-dark));
  box-shadow: 0 0 100px color-mix(in srgb, var(--color-primary) 55%, transparent);
  will-change: transform;
}
.orb-think .voice-orb-inner {
  background: conic-gradient(from 0deg, #8b7ad0, #6a5fb0, #a99ce6, #8b7ad0);
  box-shadow: 0 0 90px rgba(139,122,208,0.55);
  animation: orb-spin 1.6s linear infinite;
}
.orb-speak .voice-orb-inner {
  background: radial-gradient(circle at 35% 30%, color-mix(in srgb, var(--color-success) 80%, white), var(--color-success));
  box-shadow: 0 0 110px color-mix(in srgb, var(--color-success) 60%, transparent);
  animation: orb-breathe 1.1s ease-in-out infinite;
}
.orb-dual .voice-orb-inner {
  background: radial-gradient(circle at 35% 30%, #f0b27a, #e0954a);
  box-shadow: 0 0 96px rgba(224,149,74,0.6);
  /* No keyframe pulse here: in dual state the user is speaking over the
     playback, so the orb scale is driven by the real mic level (inline
     style) — a CSS transform animation would override that feedback. */
  animation: none;
  will-change: transform;
}
.orb-error .voice-orb-inner {
  background: radial-gradient(circle at 35% 30%, #e08a8a, var(--color-error));
  box-shadow: 0 0 80px rgba(216,92,92,0.55);
}

/* Emotion-driven orb tint — when the agent's emotion changes, the orb's
   glow color shifts to reflect the mood. Applied via data-emotion attribute. */
.voice-orb-inner[data-emotion="interested"] {
  box-shadow: 0 0 100px color-mix(in srgb, var(--color-primary) 55%, #f0a040 25%);
}
.voice-orb-inner[data-emotion="excited"] {
  box-shadow: 0 0 120px color-mix(in srgb, #f0a040 60%, var(--color-primary) 20%);
}
.voice-orb-inner[data-emotion="upset"] {
  box-shadow: 0 0 100px color-mix(in srgb, var(--color-primary) 40%, #6090c0 40%);
}
.voice-orb-inner[data-emotion="broken"] {
  box-shadow: 0 0 120px color-mix(in srgb, var(--color-error) 55%, #f0a040 25%);
  animation: orb-shake 0.4s ease-in-out 2;
}

@keyframes orb-shake {
  0%, 100% { transform: translateX(0); }
  25% { transform: translateX(-4px); }
  75% { transform: translateX(4px); }
}

/* Interjection floating overlay */
.interjection-overlay {
  position: absolute;
  top: 18%;
  left: 50%;
  transform: translateX(-50%);
  z-index: 5;
  max-width: 80vw;
  padding: 10px 20px;
  border-radius: 20px;
  background: color-mix(in srgb, var(--surface-panel-strong) 92%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-primary) 30%, transparent);
  backdrop-filter: blur(8px);
  pointer-events: none;
}
.interjection-overlay.emotion-excited {
  border-color: color-mix(in srgb, #f0a040 50%, transparent);
}
.interjection-overlay.emotion-upset {
  border-color: color-mix(in srgb, #6090c0 50%, transparent);
}
.interjection-overlay.emotion-broken {
  border-color: color-mix(in srgb, var(--color-error) 50%, transparent);
  animation: interjection-shake 0.3s ease-in-out 2;
}
.interjection-text {
  font-size: 15px;
  font-weight: 500;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
@keyframes interjection-shake {
  0%, 100% { transform: translateX(-50%); }
  25% { transform: translateX(-54%); }
  75% { transform: translateX(-46%); }
}
.interjection-fade-enter-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.interjection-fade-leave-active {
  transition: opacity 0.6s ease, transform 0.6s ease;
}
.interjection-fade-enter-from {
  opacity: 0;
  transform: translateX(-50%) translateY(8px);
}
.interjection-fade-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-8px);
}

.voice-ripple {
  position: absolute;
  width: 300px;
  height: 300px;
  border-radius: 50%;
  border: 2px solid color-mix(in srgb, var(--color-success) 50%, transparent);
  z-index: 1;
  animation: ripple 1.8s ease-out infinite;
}
.voice-ripple.delay { animation-delay: 0.9s; }

/* Playback-progress ring — fills clockwise (from the top) in proportion to
   how much of the answer's voice has actually been played, tying the visual
   progress to the spoken text. Sits above the orb but ignores pointer events. */
.voice-progress-ring {
  position: absolute;
  width: 86%;
  height: 86%;
  z-index: 3;
  transform: rotate(-90deg);
  pointer-events: none;
}
.voice-progress-track {
  fill: none;
  stroke: color-mix(in srgb, var(--color-success) 20%, transparent);
  stroke-width: 4;
}
.voice-progress-fill {
  fill: none;
  stroke: var(--color-success);
  stroke-width: 5;
  stroke-linecap: round;
  transition: stroke-dashoffset 0.2s linear;
  filter: drop-shadow(0 0 4px color-mix(in srgb, var(--color-success) 60%, transparent));
}
/* Listen-state ripple reacts to mic level via inline style (no keyframe
   animation — the scale/opacity are driven by the real-time audio RMS). */
.voice-ripple.listen-ripple {
  border-color: color-mix(in srgb, var(--color-primary) 60%, transparent);
  animation: none;
  transition: transform 0.07s ease-out, opacity 0.07s ease-out;
}
.voice-ripple.listen-ripple.dual-ripple {
  border-color: rgba(224, 149, 74, 0.7);
}

.voice-actions {
  position: absolute;
  left: 50%;
  bottom: 9%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
}
.voice-action-lg {
  width: 60px;
  height: 60px;
}

/* Error message — small, centered under the orb, only on the error state. */
.voice-error-msg {
  position: absolute;
  left: 50%;
  bottom: 4%;
  transform: translateX(-50%);
  max-width: 80vw;
  margin: 0;
  padding: 8px 16px;
  border-radius: 14px;
  background: color-mix(in srgb, var(--surface-panel-strong) 92%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-error) 45%, transparent);
  font-size: 13px;
  line-height: 1.5;
  text-align: center;
  color: var(--color-error);
  z-index: 5;
}

@keyframes orb-breathe {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.08); }
}
@keyframes orb-pulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50% { transform: scale(1.04); opacity: 0.85; }
}
@keyframes orb-spin {
  to { transform: rotate(360deg); }
}
@keyframes ripple {
  0% { transform: scale(1); opacity: 0.6; }
  100% { transform: scale(2); opacity: 0; }
}

/* Header right group */
.voice-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* Session selector panel */
.session-panel {
  position: fixed;
  top: 0;
  right: 0;
  z-index: 30;
  width: 320px;
  max-width: 85vw;
  height: 100vh;
  height: 100dvh;
  background: var(--surface-panel-strong, #1e1e2e);
  border-left: 1px solid var(--panel-border, rgba(255,255,255,0.1));
  box-shadow: -8px 0 32px rgba(0,0,0,0.3);
  display: flex;
  flex-direction: column;
}
.session-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 20px;
  border-bottom: 1px solid var(--panel-border, rgba(255,255,255,0.08));
}
.session-panel-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text, #e0e0e0);
}
.session-new-btn {
  width: 38px;
  height: 38px;
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
}
.session-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  width: 100%;
  padding: 12px 14px;
  margin-bottom: 4px;
  border: none;
  border-radius: 10px;
  background: transparent;
  color: var(--color-text, #e0e0e0);
  cursor: pointer;
  text-align: left;
  transition: background-color var(--transition-fast, 0.15s);
}
.session-item:hover {
  background: color-mix(in srgb, var(--color-primary, #6c8eef) 12%, transparent);
}
.session-item.active {
  background: color-mix(in srgb, var(--color-primary, #6c8eef) 20%, transparent);
}
.session-item-title {
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.session-item-time {
  font-size: 12px;
  color: var(--color-text-secondary, rgba(224,224,224,0.5));
  margin-top: 2px;
}
.session-empty {
  padding: 32px 16px;
  text-align: center;
  font-size: 14px;
  color: var(--color-text-secondary, rgba(224,224,224,0.4));
}
.session-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 20;
  background: rgba(0,0,0,0.4);
}

/* Transitions */
.slide-panel-enter-active,
.slide-panel-leave-active {
  transition: transform 0.25s ease;
}
.slide-panel-enter-from,
.slide-panel-leave-to {
  transform: translateX(100%);
}
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.25s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

@media (max-width: 767px) {
  .voice-orb-wrap { width: 300px; height: 300px; }
  .voice-orb { width: 220px; height: 220px; }
  .voice-ripple { width: 220px; height: 220px; }
  .voice-actions { bottom: 7%; }
  .voice-action-lg { width: 52px; height: 52px; }
  .session-panel { width: 280px; }
}
</style>
