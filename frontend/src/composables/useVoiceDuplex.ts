// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { computed, onUnmounted, ref } from 'vue'
import {
  createVoiceSession,
  getVoiceSessions,
  getVoiceWebSocketUrl,
  type VoiceEvent,
  type VoiceSession,
  type VoiceState,
} from '@/api/voice'
import { useNotesStore } from '@/stores/notes'
import { openTaskResult } from '@/utils/openTaskResult'
import { RNNoiseNode, rnnoise_loadAssets } from 'simple-rnnoise-wasm'
import { spillChunk, drainSpilledChunks, clearSpilledChunks, getSpilledCount } from './voiceAudioSpill'
import { TOKEN_KEY } from '@/api/client'

export interface VoiceMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  text: string
  kind?: 'notice' | 'tool' | 'deferred' | 'backchannel' | 'ignored' | 'interjection'
  streaming?: boolean
  reason?: string
  emotion?: string
}

function getAudioContextCtor(): typeof AudioContext {
  const ctor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!ctor) {
    throw new Error('当前浏览器不支持音频')
  }
  return ctor
}

function downmixToMono(buffer: AudioBuffer): Float32Array {
  const channelCount = buffer.numberOfChannels
  const sampleCount = buffer.length
  if (channelCount <= 1) {
    return new Float32Array(buffer.getChannelData(0))
  }
  const mono = new Float32Array(sampleCount)
  for (let i = 0; i < sampleCount; i += 1) {
    let s = 0
    for (let c = 0; c < channelCount; c += 1) s += buffer.getChannelData(c)[i]
    mono[i] = s / channelCount
  }
  return mono
}

function resample(input: Float32Array, sourceRate: number, targetRate: number): Float32Array {
  if (sourceRate === targetRate) return input
  const ratio = sourceRate / targetRate
  const outLen = Math.max(1, Math.round(input.length / ratio))
  const out = new Float32Array(outLen)
  for (let i = 0; i < outLen; i += 1) {
    const pos = i * ratio
    const lo = Math.floor(pos)
    const hi = Math.min(lo + 1, input.length - 1)
    const w = pos - lo
    out[i] = input[lo] * (1 - w) + input[hi] * w
  }
  return out
}

function float32ToLEBuffer(input: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(input.length * 4)
  const view = new DataView(buffer)
  for (let i = 0; i < input.length; i += 1) view.setFloat32(i * 4, input[i], true)
  return buffer
}

let _msgId = 1

// Debounced reload of the notes store after the agent notes tool mutates
// notes/notebooks server-side during a voice session, so sidebars & lists
// stay in sync.
let _notesRefreshTimer: ReturnType<typeof setTimeout> | null = null
function scheduleNotesRefresh() {
  if (_notesRefreshTimer) clearTimeout(_notesRefreshTimer)
  _notesRefreshTimer = setTimeout(() => {
    useNotesStore().refreshFromExternalChange()
  }, 500)
}

export function useVoiceDuplex() {
  const state = ref<VoiceState>('idle')
  const connected = ref(false)
  const micOn = ref(false)
  const speaking = ref(false)
  const errorMsg = ref<string | null>(null)
  const partialText = ref('')
  const messages = ref<VoiceMessage[]>([])
  const ttsAvailable = ref(true)
  const micLevel = ref(0)
  const currentSessionId = ref<string | null>(null)
  const sessions = ref<VoiceSession[]>([])
  const showSessionPanel = ref(false)
  const voiceAssistantId = ref<string | null>(null)
  const emotion = ref('calm')
  const interjectionText = ref('')

  let socket: WebSocket | null = null
  // ---- weak-network resilience (2026-11-03) ----
  // Mirrors useAsrStreaming's proven semantics: abnormal close of an
  // ESTABLISHED session auto-reconnects the same conversation with
  // exponential backoff; mic frames produced during the gap go into a
  // drop-oldest ring buffer and are replayed after the new session is
  // ready — recorded speech is never silently dropped.
  const RECONNECT_MAX_ATTEMPTS = 10
  const RECONNECT_BASE_DELAY_MS = 500
  const RECONNECT_MAX_DELAY_MS = 5000
  // e2e hook: shrink the in-memory ring so the IndexedDB spill path is
  // reachable without generating ~100s of real audio (voice_duplex_reconnect
  // S7). DEV-only + floor of 32 chunks so a production page (or an extension)
  // cannot shrink the ring and force every frame through IndexedDB (M5).
  const rawTestCap = (import.meta as any).env?.DEV && typeof window !== 'undefined'
    ? (window as any).__VOICE_TEST_BUFFER_CAP
    : undefined
  const MAX_PENDING_CHUNKS = typeof rawTestCap === 'number' && Number.isFinite(rawTestCap)
    ? Math.max(8, Math.floor(rawTestCap))
    : 1200
  const BACKPRESSURE_THRESHOLD = 256 * 1024
  const FLUSH_INTERVAL_MS = 100
  const MAX_PENDING_CLIENT_EVENTS = 20
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let flushTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectAttempt = 0
  let reconnecting = false
  // Generation counter: start()/disconnect() bump it; a reconnect whose
  // epoch went stale belongs to a dead generation and must not touch state.
  let sessionEpoch = 0
  let userClosed = false      // disconnect() → suppress auto-reconnect
  let sawErrorEvent = false   // backend `error` on this connection → terminal (no retry storm
                              // on fatal config errors); cleared by later session activity
  let terminalClose = false   // 1008 POLICY_VIOLATION (auth) → retrying cannot help
  let micIntentionallyOff = false // user turned the mic off → a late 'ready' must not reopen it
  let pendingChunks: ArrayBuffer[] = []
  let pendingClientEvents: Array<string> = []
  // Spill replay state (see voiceAudioSpill.ts): loaded once per reconnect,
  // drained oldest-first before the in-memory ring.
  let spillLoaded: ArrayBuffer[] | null = null
  let spillDrainStarted = false
  let flushInFlight = false
  // Generation token for the spill store: bumped by clearSpillState() so a
  // drain that was in flight across a teardown can never inject the previous
  // session's audio into the next one (B2).
  let spillEpoch = 0
  let lastConversationId: string | null = null
  // capture graph
  let mediaStream: MediaStream | null = null
  let captureCtx: AudioContext | null = null
  let sourceNode: MediaStreamAudioSourceNode | null = null
  let rnnoiseNode: import('simple-rnnoise-wasm').RNNoiseNode | null = null
  let processorNode: ScriptProcessorNode | null = null
  let muteNode: GainNode | null = null
  // playback graph
  let playCtx: AudioContext | null = null
  let nextStartTime = 0
  let activeSources: AudioBufferSourceNode[] = []
  // When true, incoming PCM audio is dropped (after a barge-in interrupt the
  // TTS consumer may still send a stray chunk before it checks the interrupt
  // flag — those must not be scheduled or the audio "resumes" briefly).
  let droppingAudio = false
  // Playback-progress tracking. In 语音助理 mode the user only ever RECEIVES audio,
  // so we tie playback progress to the answer text: the client reports how far
  // the current audible burst has actually played (played_sec / total_sec) and
  // when it fully drains. Any truncation is then an AUDIO-playback truncation,
  // never a text truncation — the backend reconciles this with the per-segment
  // audio timeline to know exactly how much of the answer was spoken aloud.
  let burstStartTime = 0 // ctx time the current audible burst started playing
  let burstEndTime = 0 // scheduled end (nextStartTime) of the current burst
  let drainedSent = false
  let activeNotificationId: string | null = null
  let activeNoticeBurstSeq = 0
  // A notice→answer transition starts a new burst without cancelling the old
  // audible tail. Keep independent drain clocks until each notice has ACKed.
  let pendingNoticeDrains = new Map<string, { seq: number; endTime: number }>()
  let progressTimer: ReturnType<typeof setInterval> | null = null
  const playbackProgress = ref(0) // 0..1 — drives the orb progress ring

  let currentAssistantId: number | null = null
  let pendingUserMsgId: number | null = null

  function pushMessage(msg: Omit<VoiceMessage, 'id'>): number {
    const id = _msgId++
    messages.value.push({ id, ...msg })
    return id
  }

  function updateMessage(id: number, text: string, streaming: boolean) {
    const m = messages.value.find(x => x.id === id)
    if (m) {
      m.text = text
      m.streaming = streaming
    }
  }

  // ---- playback ----
  function ensurePlayCtx(): AudioContext {
    if (!playCtx) {
      const Ctor = getAudioContextCtor()
      playCtx = new Ctor()
    }
    if (playCtx.state === 'suspended') {
      void playCtx.resume()
    }
    return playCtx
  }

  function resetBurstTiming() {
    burstStartTime = 0
    burstEndTime = 0
    drainedSent = false
    playbackProgress.value = 0
  }

  function stopProgressTimer() {
    if (progressTimer !== null) {
      clearInterval(progressTimer)
      progressTimer = null
    }
  }

  function retainActiveNoticeDrain(nextNotificationId: string | null) {
    if (
      activeNotificationId
      && activeNotificationId !== nextNotificationId
      && !drainedSent
      && burstEndTime > burstStartTime
    ) {
      pendingNoticeDrains.set(activeNotificationId, {
        seq: activeNoticeBurstSeq,
        endTime: burstEndTime,
      })
    }
  }

  function drainFinishedNotices() {
    if (!playCtx) return
    for (const [notificationId, pending] of pendingNoticeDrains) {
      if (playCtx.currentTime < pending.endTime - 0.05) continue
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
          event: 'playback_drained',
          notification_id: notificationId,
          notice_burst_seq: pending.seq,
        }))
        pendingNoticeDrains.delete(notificationId)
      }
    }
  }

  // Periodically report the real playback position to the backend and detect
  // when the burst fully drains. WebAudio's clock advances even in headless /
  // silent output, so this works regardless of actual sound rendering.
  function startProgressTimer() {
    stopProgressTimer()
    progressTimer = setInterval(() => {
      drainFinishedNotices()
      if (!playCtx || burstEndTime <= burstStartTime) return
      const total = burstEndTime - burstStartTime
      const played = Math.max(0, Math.min(playCtx.currentTime - burstStartTime, total))
      playbackProgress.value = total > 0 ? Math.min(1, played / total) : 0
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ event: 'playback_progress', played_sec: played, total_sec: total }))
      }
      if (total > 0 && playCtx.currentTime >= burstEndTime - 0.05 && !drainedSent) {
        drainedSent = true
        playbackProgress.value = 1
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({
            event: 'playback_drained',
            notification_id: activeNotificationId,
            notice_burst_seq: activeNoticeBurstSeq,
          }))
        }
        stopProgressTimer()
      }
    }, 250)
  }

  function schedulePcm16(pcm16: ArrayBuffer) {
    // Drop stray audio chunks that arrive after an interrupt — the backend
    // TTS consumer may still be mid-stream when the interrupt flag is set.
    if (droppingAudio) return
    const ctx = ensurePlayCtx()
    const int16 = new Int16Array(pcm16)
    const float = new Float32Array(int16.length)
    for (let i = 0; i < int16.length; i += 1) float[i] = int16[i] / 32768
    const resampled = resample(float, 24000, ctx.sampleRate)
    const buffer = ctx.createBuffer(1, resampled.length, ctx.sampleRate)
    buffer.copyToChannel(resampled, 0)
    const source = ctx.createBufferSource()
    source.buffer = buffer
    source.connect(ctx.destination)
    const start = Math.max(ctx.currentTime + 0.02, nextStartTime)
    source.start(start)
    nextStartTime = start + buffer.duration
    if (burstStartTime === 0) burstStartTime = start
    burstEndTime = nextStartTime
    activeSources.push(source)
    source.onended = () => {
      activeSources = activeSources.filter(s => s !== source)
    }
  }

  function clearPlayback() {
    for (const s of activeSources) {
      try { s.stop() } catch { /* ignore */ }
    }
    activeSources = []
    pendingNoticeDrains.clear()
    if (playCtx) {
      nextStartTime = playCtx.currentTime + 0.02
    } else {
      nextStartTime = 0
    }
    stopProgressTimer()
    resetBurstTiming()
    activeNotificationId = null
    activeNoticeBurstSeq = 0
  }

  // ---- mic capture ----
  let _rnnoiseReady = false
  // Shared in-flight guard so concurrent callers (start() + late-ready
  // recovery in handleEvent) never run two getUserMedia/capture setups.
  let _capturePromise: Promise<void> | null = null

  // Acoustic near-field gate (background-speech barge-in fix, 2026-08-07):
  // classify the mic input as near-field (user close to the phone — almost
  // certainly the user's own voice) vs far-field (environment speech picked
  // up at a distance: TV/room conversation) and report transitions to the
  // backend via the `audio_proximity` WS event. Far-field speech must never
  // pause playback nor interrupt the current answer.
  // Adaptive noise floor (exponential): drops fast onto quiet frames, rises
  // only slowly so transient loudness never gets "absorbed" into the floor.
  let _proxFloor = 0.01
  let _proxFrameCount = 0
  let _proxNearFrames = 0
  let _proxFarFrames = 0
  let _proxNear = false // last reported state
  let _proxReported = false // first transition ever reported
  let _proxLastSentAt = 0 // heartbeat: last time near was (re)reported
  const PROX_WARMUP_FRAMES = 10 // frames of ambient baseline before gating
  const PROX_NEAR_RATIO = 6.0 // rms >= floor * ratio → near candidate（2026-09-29 4→6：远场抑制）
  const PROX_NEAR_MIN = 0.04 // absolute floor of the near threshold（2026-09-29 0.02→0.04）
  const PROX_NEAR_FRAMES = 2 // consecutive near frames to confirm (~170ms)
  const PROX_FAR_FRAMES = 4 // consecutive far frames to clear (~340ms)
  const PROX_FLOOR_MAX = 0.06 // cap so the near threshold stays ≤ 0.24
  const PROX_HEARTBEAT_MS = 500 // re-report near while it persists (backend
  // gates on a 1.0s freshness window and classifies at EoT flush 1-3s later;
  // without the heartbeat the near evidence would expire mid-utterance and a
  // real barge-in would be misjudged as far-field background — A4.9 C1/C2)

  function sendProximity(near: boolean) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return
    _proxNear = near
    _proxLastSentAt = performance.now()
    socket.send(JSON.stringify({ event: 'audio_proximity', near }))
  }

  function ensureCapture(): Promise<void> {
    if (micOn.value) return Promise.resolve()
    if (_capturePromise) return _capturePromise
    _capturePromise = startCapture().finally(() => {
      _capturePromise = null
    })
    return _capturePromise
  }

  function surfaceCaptureError(err: unknown) {
    errorMsg.value = err instanceof Error ? err.message : '无法访问麦克风'
    state.value = 'error'
  }

  async function _ensureRnnoise(): Promise<typeof rnnoiseNode> {
    if (!captureCtx) return null
    if (!_rnnoiseReady) {
      try {
        const [wasmMod, workletMod] = await Promise.all([
          import('simple-rnnoise-wasm/rnnoise.wasm?url'),
          import('simple-rnnoise-wasm/rnnoise.worklet.js?url'),
        ])
        const assets = rnnoise_loadAssets({
          scriptSrc: workletMod.default as string,
          moduleSrc: wasmMod.default as string,
        })
        await RNNoiseNode.register(captureCtx, assets)
        _rnnoiseReady = true
      } catch (err) {
        console.warn('RNNoise 降噪初始化失败，使用原始音频:', err)
        return null
      }
    }
    try {
      return new RNNoiseNode(captureCtx)
    } catch {
      return null
    }
  }

  async function startCapture() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('当前环境禁止访问麦克风：需通过 HTTPS 或 localhost 访问')
    }
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
    } catch (err) {
      const msg = err instanceof Error ? err.message : ''
      if (msg.includes('Permission') || (err instanceof Error && err.name === 'NotAllowedError')) {
        throw new Error('麦克风权限被拒绝，请允许浏览器访问麦克风')
      }
      if (err instanceof Error && err.name === 'NotFoundError') {
        throw new Error('未找到麦克风设备')
      }
      throw new Error(`无法访问麦克风: ${msg || '未知错误'}`)
    }
    const Ctor = getAudioContextCtor()
    captureCtx = new Ctor({ latencyHint: 'interactive' })
    sourceNode = captureCtx.createMediaStreamSource(mediaStream)
    // —— RNNoise neural noise suppression (browser-side denoising) ——
    // Insert RNNoise between the microphone source and the capture processor.
    // RNNoise uses a recurrent neural network to distinguish speech from noise,
    // preserving voice quality while filtering hum, fan noise, keyboard clicks, etc.
    rnnoiseNode = await _ensureRnnoise()
    processorNode = captureCtx.createScriptProcessor(4096, sourceNode.channelCount, 1)
    muteNode = captureCtx.createGain()
    muteNode.gain.value = 0
    processorNode.onaudioprocess = (e) => {
      if (!micOn.value) {
        micLevel.value = 0
        return
      }
      const live = !!socket && socket.readyState === WebSocket.OPEN
      const mono = downmixToMono(e.inputBuffer)
      // Compute RMS amplitude to drive the listening-halo pulse animation
      // so the user can visually tell whether their voice is being captured.
      // Kept running while the socket is down: the capture graph and the
      // adaptive noise floor stay warm, so the first frames after a reconnect
      // are judged against a live baseline instead of a cold one.
      let sumSq = 0
      for (let i = 0; i < mono.length; i += 1) sumSq += mono[i] * mono[i]
      const rms = Math.sqrt(sumSq / Math.max(1, mono.length))
      // Normalize: typical speech RMS ~0.05-0.3; map to 0-1 with soft curve
      const level = Math.min(1, rms * 3.5)
      micLevel.value = level
      if (live) {
        // Near-field classification: adaptive noise floor vs current RMS.
        // A far-field background voice is a few dB below close-mic speech, so
        // a loud-close-vs-quiet-far threshold separates "the user talking into
        // the phone" from "TV/room speech picked up at a distance".
        _proxFrameCount += 1
        if (rms < _proxFloor) {
          _proxFloor = Math.max(0.0005, rms * 0.9 + _proxFloor * 0.1)
        } else {
          _proxFloor = Math.min(PROX_FLOOR_MAX, _proxFloor + _proxFloor * 0.002 + 1e-5)
        }
        if (_proxFrameCount >= PROX_WARMUP_FRAMES) {
          const nearThr = Math.max(_proxFloor * PROX_NEAR_RATIO, PROX_NEAR_MIN)
          if (rms >= nearThr) {
            _proxNearFrames += 1
            _proxFarFrames = 0
          } else {
            _proxNearFrames = 0
            _proxFarFrames += 1
          }
          const wantNear = _proxNearFrames >= PROX_NEAR_FRAMES
          const wantFar = _proxFarFrames >= PROX_FAR_FRAMES
          if (!_proxReported) {
            _proxReported = true
            sendProximity(wantNear)
          } else if (wantNear && !_proxNear) {
            sendProximity(true)
          } else if (wantFar && _proxNear) {
            sendProximity(false)
          } else if (wantNear && _proxNear && performance.now() - _proxLastSentAt >= PROX_HEARTBEAT_MS) {
            // Heartbeat while near persists: the backend's freshness window is
            // 1.0s and it classifies at EoT flush (1-3s after the last partial),
            // so a single near report would expire before the verdict matters.
            sendProximity(true)
          }
        }
      }
      const resampled = resample(mono, e.inputBuffer.sampleRate, 16000)
      if (resampled.length === 0) return
      const chunk = float32ToLEBuffer(resampled)
      if (live && socket!.bufferedAmount < BACKPRESSURE_THRESHOLD && pendingChunks.length === 0) {
        socket!.send(chunk)
      } else if (live || reconnecting || reconnectTimer !== null) {
        // Backpressured, or the socket died with an auto-reconnect in flight:
        // buffer so speech in the gap is replayed after reconnect instead of
        // being silently dropped. Ring-bound drop-oldest (≤~100s) keeps memory
        // bounded on a long outage.
        bufferChunk(chunk)
      }
      // Socket down with NO reconnect pending (idle/stopped session) → drop.
    }
    // Audio graph: mic → RNNoise(denoise) → ScriptProcessor(capture) → mute → destination
    if (rnnoiseNode) {
      sourceNode.connect(rnnoiseNode)
      rnnoiseNode.connect(processorNode)
    } else {
      sourceNode.connect(processorNode)
    }
    processorNode.connect(muteNode)
    muteNode.connect(captureCtx.destination)
    // Start RNNoise VAD keepalive (provides VAD status via onstatus callback)
    rnnoiseNode?.update(true)
    micOn.value = true
  }

  function stopCapture() {
    micOn.value = false
    micLevel.value = 0
    _proxFloor = 0.01
    _proxFrameCount = 0
    _proxNearFrames = 0
    _proxFarFrames = 0
    _proxNear = false
    _proxReported = false
    _proxLastSentAt = 0
    rnnoiseNode?.update(false)
    rnnoiseNode?.disconnect()
    processorNode?.disconnect()
    sourceNode?.disconnect()
    muteNode?.disconnect()
    rnnoiseNode = null
    processorNode = null
    sourceNode = null
    muteNode = null
    mediaStream?.getTracks().forEach(t => t.stop())
    mediaStream = null
    if (captureCtx) {
      void captureCtx.close()
      captureCtx = null
    }
    _rnnoiseReady = false
  }

  // ---- websocket ----
  function stopFlushTimer() {
    if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null }
  }

  function stopReconnectTimer() {
    if (reconnectTimer !== null) { clearTimeout(reconnectTimer); reconnectTimer = null }
  }

  async function flushPendingChunks(): Promise<void> {
    if (flushInFlight) return
    flushInFlight = true
    const epoch = spillEpoch
    try {
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        // Socket dead (reconnect pending): KEEP the buffered audio — it will be
        // flushed once the new session is ready. Never silently drop speech.
        stopFlushTimer()
        return
      }
      // Oldest first: spilled (IndexedDB, ~10 min bound) then the in-memory ring.
      if (spillLoaded === null && !spillDrainStarted) {
        spillDrainStarted = true
        const drained = await drainSpilledChunks()
        // B1: the socket may have died (or the whole generation been replaced)
        // while we awaited the cursor walk. On a dead socket `send()` is
        // silently DROPPED by the browser, and the rows are already deleted
        // from IndexedDB — so re-spill the remainder instead of flushing it
        // into the void.
        if (epoch !== spillEpoch) {
          // A newer generation owns the spill store now; these rows belong to
          // a dead session and must never leak into it (B2).
          spillLoaded = null
          spillDrainStarted = false
          return
        }
        if (!socket || socket.readyState !== WebSocket.OPEN) {
          for (const chunk of drained) void spillChunk(chunk)
          spillLoaded = null
          spillDrainStarted = false
          stopFlushTimer()
          return
        }
        spillLoaded = drained
      }
      while (
        spillLoaded && spillLoaded.length > 0
        && socket && socket.readyState === WebSocket.OPEN
        && socket.bufferedAmount < BACKPRESSURE_THRESHOLD
      ) {
        socket.send(spillLoaded.shift()!)
      }
      while (
        pendingChunks.length > 0
        && socket && socket.readyState === WebSocket.OPEN
        && socket.bufferedAmount < BACKPRESSURE_THRESHOLD
      ) {
        socket.send(pendingChunks.shift()!)
      }
      if ((spillLoaded && spillLoaded.length > 0) || pendingChunks.length > 0) {
        flushTimer = setTimeout(() => { void flushPendingChunks() }, FLUSH_INTERVAL_MS)
      } else {
        stopFlushTimer()
      }
    } finally {
      flushInFlight = false
    }
  }

  function bufferChunk(chunk: ArrayBuffer) {
    pendingChunks.push(chunk)
    // Ring bound: move the oldest overflow to IndexedDB (~10 min) instead of
    // dropping recorded speech; if spill is unavailable, fall back to the old
    // drop-oldest behavior so memory stays bounded either way.
    while (pendingChunks.length > MAX_PENDING_CHUNKS) {
      const oldest = pendingChunks.shift()!
      if (spillLoaded !== null) {
        // A replay is in flight: spilling now would give these chunks HIGHER
        // auto-increment keys than the re-spilled remainder and reorder the
        // stream (M1). The replay window is short — keep them in memory.
        pendingChunks.push(oldest)
        break
      }
      void spillChunk(oldest).then((stored) => {
        if (!stored) console.warn('[voice] pending audio buffer full, dropping oldest chunk')
      })
    }
    if (flushTimer === null && !flushInFlight) {
      flushTimer = setTimeout(() => { void flushPendingChunks() }, FLUSH_INTERVAL_MS)
    }
  }

  // Re-spill the undelivered remainder of a drain when the socket dies again
  // mid-replay — keeps order (auto-increment keys append after nothing else
  // exists in the store at that point) and loses no audio.
  function respillRemainder() {
    if (spillLoaded && spillLoaded.length > 0) {
      for (const chunk of spillLoaded) void spillChunk(chunk)
    }
    spillLoaded = null
    spillDrainStarted = false
  }

  function clearSpillState() {
    // Bump the generation FIRST: an in-flight drain that resumes after this
    // point must discard its result instead of injecting the previous
    // session's audio into the new one (B2). `flushInFlight` is intentionally
    // NOT reset here — the old flush still owns that flag until it returns.
    spillEpoch += 1
    spillLoaded = null
    spillDrainStarted = false
    void clearSpilledChunks()
  }

  function queueClientEvent(payload: { event: string; text?: string }) {
    if (pendingClientEvents.length >= MAX_PENDING_CLIENT_EVENTS) {
      // A text message is a semantic unit — dropping the OLDEST silently loses
      // speech the user already typed. Reject the newest and keep the queue
      // stable (M4); the caller keeps its local UI copy.
      console.warn('[voice] pending client-event queue full, dropping newest event')
      return
    }
    pendingClientEvents.push(JSON.stringify(payload))
  }

  function flushClientEvents() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return
    while (pendingClientEvents.length > 0) {
      socket.send(pendingClientEvents.shift()!)
    }
  }

  function finalizeStreamingMessages() {
    // The turn in flight died with the socket — clear streaming flags so no
    // message spins forever, but KEEP the text (completed turns are already
    // persisted server-side; the transcript stays visible).
    for (const m of messages.value) {
      if (m.streaming) m.streaming = false
    }
  }

  function scheduleReconnect() {
    stopReconnectTimer()
    if (reconnectAttempt >= RECONNECT_MAX_ATTEMPTS) {
      errorMsg.value = '语音连接已断开，自动重连失败'
      state.value = 'error'
      // Drop every buffered medium of the dead session — it must never be
      // replayed into a later session as ghost speech.
      pendingChunks = []
      pendingClientEvents = []
      stopFlushTimer()
      clearSpillState()
      return
    }
    state.value = 'connecting'
    const epoch = sessionEpoch
    const delay = Math.min(
      RECONNECT_BASE_DELAY_MS * (2 ** reconnectAttempt),
      RECONNECT_MAX_DELAY_MS,
    )
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      if (epoch !== sessionEpoch) return
      void doReconnect(epoch)
    }, delay)
  }

  function handleSocketClose(closeEvent: CloseEvent) {
    const epoch = sessionEpoch
    connected.value = false
    finalizeStreamingMessages()
    // Playback/turn cursors belong to the dead connection — reset them. The
    // audible-notification ACK state is server-owned and replayed on the next
    // session (voice_notification_service), so local teardown is safe here.
    clearPlayback()
    speaking.value = false
    partialText.value = ''
    pendingUserMsgId = null
    currentAssistantId = null
    respillRemainder()
    if (userClosed) return
    if (closeEvent.code === 1008) {
      // Auth failure delivered AFTER accept (api/voice.py:116-120 when the
      // session survives the handshake): a retry would fail identically until
      // the user signs in again — terminal, and end the generation's buffers.
      terminalClose = true
      pendingChunks = []
      pendingClientEvents = []
      stopFlushTimer()
      clearSpillState()
      errorMsg.value = closeEvent.reason || '语音服务鉴权失败，请重新登录'
      state.value = 'error'
      return
    }
    if (closeEvent.code === 1006 || closeEvent.code === 1005) {
      // A token rejected BEFORE accept surfaces as an opaque 1006/1005 (the
      // browser discards the backend's pre-accept 1008 → HTTP 403). One cheap
      // authenticated probe tells "network down" (retry) apart from
      // "credentials gone" (terminal), so an expired session cannot spin the
      // full backoff ladder (I1).
      void probeAuthLost().then((lost) => {
        if (!lost || userClosed || epoch !== sessionEpoch) return
        terminalClose = true
        stopReconnectTimer()
        pendingChunks = []
        pendingClientEvents = []
        stopFlushTimer()
        clearSpillState()
        errorMsg.value = '语音服务鉴权失败，请重新登录'
        state.value = 'error'
      })
    }
    if (sawErrorEvent) {
      // Backend reported an error on this connection and it closed — treat as
      // terminal (the error state is already shown; the manual retry button
      // stays available). Prevents retry storms on permanent failures.
      if (state.value !== 'error') {
        errorMsg.value = errorMsg.value || closeEvent.reason || '语音服务连接已断开'
        state.value = 'error'
      }
      return
    }
    // Initial connect failures, abnormal closes AND clean server-side closes
    // (restart/drain) all auto-reconnect with backoff — a weak network must
    // never strand the user on a "tap retry" screen.
    scheduleReconnect()
  }

  async function probeAuthLost(): Promise<boolean> {
    try {
      // Deliberately NOT the axios instance: a 401 there triggers the global
      // logout redirect (client.ts:30-36), which would unmount the voice page
      // before its terminal message can render. A bare fetch keeps the probe
      // side-effect free; every normal API call still runs the usual flow.
      const token = localStorage.getItem(TOKEN_KEY)
      const resp = await fetch('/api/voice/sessions', {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      return resp.status === 401 || resp.status === 403
    } catch {
      return false // network-level failure → keep retrying
    }
  }

  function shouldReconnect(): boolean {
    return !userClosed && !sawErrorEvent && !terminalClose
  }

  async function doReconnect(epoch: number) {
    if (reconnecting) return
    reconnecting = true
    reconnectAttempt += 1
    try {
      await connect(lastConversationId, true)
      if (epoch !== sessionEpoch) return // superseded generation: its handlers belong to the new one
      // Success bookkeeping happens on 'ready' (reconnecting=false, attempt reset).
    } catch {
      if (epoch !== sessionEpoch) return
      if (reconnecting) {
        reconnecting = false
        // The attempt socket's close event normally schedules the next try via
        // handleSocketClose; this covers non-close failures (connect timeout).
        // must NOT resurrect a terminal decision made there (I-3).
        if (reconnectTimer === null && shouldReconnect()) scheduleReconnect()
      }
    }
  }

  function handleEvent(ev: VoiceEvent) {
    if (ev.event !== 'error') {
      // Any successfully parsed backend event counts as "the connection lived
      // on after the last error": the terminal guard only applies when an
      // `error` was the LAST thing this connection produced (I-2). A per-turn
      // error (voice_generation.py:410) followed by normal traffic must not
      // permanently disarm auto-reconnect; nor may its stale text be reused
      // as a later disconnect's message (M-3).
      sawErrorEvent = false
      errorMsg.value = null
    }
    switch (ev.event) {
      case 'ready':
        connected.value = true
        ttsAvailable.value = ev.tts !== false
        state.value = 'listen'
        // Reconnect completed: the new session is live — drain the audio
        // buffered during the gap into it (backend queues it until its own
        // ASR upstream connects, so nothing is lost).
        if (reconnecting) {
          reconnecting = false
          reconnectAttempt = 0
          // Fresh acoustic baseline for the new backend session (M-4): the
          // noise floor may not match the room the reconnect lands in, and a
          // stale `_proxReported` would suppress the first near/far verdict.
          _proxFrameCount = 0
          _proxNearFrames = 0
          _proxFarFrames = 0
          _proxReported = false
          _proxNear = false
        }
        void flushPendingChunks()
        // Text typed / interrupt tapped during the outage: deliver in order.
        flushClientEvents()
        // Late-ready recovery: if capture isn't running (e.g. the 12s connect
        // timeout fired while the backend waited on a slow ASR upstream), start
        // the mic now so the session isn't left "listening" with the mic off.
        // NEVER reopen a mic the user explicitly turned off (2026-11-03:
        // reconnect 'ready' would otherwise resurrect capture against an
        // intentional mute).
        if (!micOn.value && !micIntentionallyOff) {
          void ensureCapture().catch(surfaceCaptureError)
        }
        break
      case 'session':
        if (ev.session_id) {
          currentSessionId.value = ev.session_id
          lastConversationId = ev.session_id
        }
        if (ev.assistant_id) voiceAssistantId.value = ev.assistant_id
        break
      case 'state':
        if (ev.state) state.value = ev.state
        // A new think phase means a fresh turn — allow audio again.
        if (ev.state === 'think') droppingAudio = false
        break
      case 'asr_partial':
        partialText.value = ev.text || ''
        break
      case 'asr_segment':
        partialText.value = ''
        break
      case 'user_turn':
        partialText.value = ''
        pendingUserMsgId = pushMessage({ role: 'user', text: ev.text || '' })
        currentAssistantId = null
        break
      case 'user_turn_cancelled':
        // Intent classifier rejected the turn as noise/fragment — remove the
        // optimistically-shown user message so the transcript stays clean.
        if (pendingUserMsgId !== null) {
          messages.value = messages.value.filter(m => m.id !== pendingUserMsgId)
          pendingUserMsgId = null
        }
        break
      case 'assistant_text': {
        const text = ev.text || ''
        if (currentAssistantId === null) {
          currentAssistantId = pushMessage({ role: 'assistant', text, streaming: !ev.done })
        } else {
          updateMessage(currentAssistantId, text, !ev.done)
        }
        break
      }
      case 'speaking_start': {
        droppingAudio = false
        speaking.value = true
        const nextNotificationId = ev.notification_id || null
        retainActiveNoticeDrain(nextNotificationId)
        activeNotificationId = nextNotificationId
        activeNoticeBurstSeq = typeof ev.notice_burst_seq === 'number' && Number.isInteger(ev.notice_burst_seq) ? ev.notice_burst_seq : 0
        resetBurstTiming()
        startProgressTimer()
        break
      }
      case 'speaking_end':
        speaking.value = false
        activeNotificationId = null
        activeNoticeBurstSeq = 0
        drainFinishedNotices()
        if (pendingNoticeDrains.size === 0) stopProgressTimer()
        playbackProgress.value = 0
        break
      case 'tool_notice':
        pushMessage({ role: 'system', text: ev.text || '', kind: 'notice' })
        break
      case 'tool_call':
        pushMessage({ role: 'system', text: `正在调用工具：${ev.name || ''}`, kind: 'tool' })
        break
      case 'tool_result':
        pushMessage({ role: 'system', text: `工具 ${ev.name || ''} 已返回结果`, kind: 'tool' })
        if (ev.name === 'notes') scheduleNotesRefresh()
        break
      case 'deferred':
        pushMessage({ role: 'system', text: `已收到：“${ev.text || ''}”，将在播报结束后回答`, kind: 'deferred' })
        break
      case 'turn_queued':
        // User spoke while a turn (e.g. a long tool loop) is still running —
        // the backend acknowledged immediately so the user is never ignored.
        pushMessage({ role: 'system', text: `已收到：“${ev.text || ''}”，当前任务还在执行，告一段落后立即回答`, kind: 'deferred' })
        break
      case 'backchannel':
        // subtle; ignore in transcript
        break
      case 'interjection':
        // Agent made a brief remark during the user's speech. Show it as a
        // subtle assistant message and set the interjection text for the
        // floating overlay on the voice page.
        interjectionText.value = ev.text || ''
        if (ev.text) {
          pushMessage({ role: 'assistant', text: ev.text, kind: 'interjection', emotion: ev.emotion })
        }
        // Clear the floating text after a few seconds
        setTimeout(() => { interjectionText.value = '' }, 4000)
        break
      case 'emotion':
        if (ev.emotion) emotion.value = ev.emotion
        break
      case 'ignored':
        // ASR noise / misrecognition the agent chose not to answer — show subtly.
        partialText.value = ''
        pushMessage({ role: 'system', text: ev.text || '', kind: 'ignored', reason: ev.reason })
        break
      case 'interrupted':
        droppingAudio = true
        clearPlayback()
        speaking.value = false
        break
      case 'playback_paused':
        // Acoustic onset barge-in: user speech stopped the assistant's audio
        // immediately; the backend decides resume-from-breakpoint or switch.
        droppingAudio = true
        clearPlayback()
        speaking.value = false
        break
      case 'notification_ack_pending':
        // A drain arrived before the full notice was ready. Rearm instead of
        // leaving the client unable to ACK the final segment of the same notice.
        if (activeNotificationId === ev.notification_id) {
          drainedSent = false
          if (speaking.value) startProgressTimer()
        }
        break
      case 'playback_resumed':
        // The backend emits this before any resumed PCM. Restore both parts of
        // the notice ACK identity so a pre-pause drain cannot settle this burst.
        activeNotificationId = ev.notification_id || null
        activeNoticeBurstSeq = typeof ev.notice_burst_seq === 'number' && Number.isInteger(ev.notice_burst_seq) ? ev.notice_burst_seq : 0
        resetBurstTiming()
        startProgressTimer()
        break
      case 'generation_cancelled':
        // Noise gate cancelled the in-flight generation — remove any partial
        // assistant message that was streamed before the cancellation.
        droppingAudio = true
        clearPlayback()
        speaking.value = false
        if (currentAssistantId !== null) {
          messages.value = messages.value.filter(m => m.id !== currentAssistantId)
          currentAssistantId = null
        }
        break
      case 'task_cancelled':
        droppingAudio = true
        clearPlayback()
        speaking.value = false
        pushMessage({ role: 'system', text: '任务已取消', kind: 'notice' })
        break
      case 'delegation_submitted': {
        pushMessage({
          role: 'system',
          text: ev.deduplicated
            ? `该任务已经提交过（${ev.assistant_name || '执行助手'}），无需重复创建`
            : `已交给 ${ev.assistant_name || '执行助手'}，独立执行会话已创建（${ev.status || 'pending'}）`,
          kind: 'notice',
        })
        break
      }
      case 'open_task_result': {
        const target = String(ev.task_id || ev.execution_conversation_id || '')
        if (target) void openTaskResult(target)
        break
      }
      case 'bg_task_notice_batch': {
        const items = Array.isArray(ev.items) ? ev.items : []
        for (const raw of items) {
          const item = raw as Record<string, unknown>
          const status = String(item.status || '')
          pushMessage({
            role: 'system',
            text: `委派任务《${String(item.title || '未命名任务')}》${status === 'completed' ? '已完成' : status === 'cancelled' ? '已取消' : status === 'timed_out' ? '已超时' : status.startsWith('waiting_') ? '需要你确认' : '执行失败'}`,
            kind: 'notice',
          })
        }
        break
      }
      case 'bg_task_notice': {
        // A background task submitted from voice reached a terminal/waiting
        // state — the assistant announces it aloud and offers follow-ups.
        const status = String(ev.status || '')
        const stateText = status === 'completed'
          ? '已完成'
          : status === 'cancelled'
            ? '已取消'
            : status === 'timed_out'
              ? '已超时'
              : status === 'waiting_input'
                ? '需要你补充信息'
                : status === 'waiting_auth'
                  ? '需要你授权'
                  : status === 'waiting_budget'
                    ? '需要你确认是否继续'
                    : '执行失败'
        pushMessage({
          role: 'system',
          text: `后台任务《${ev.title || '未命名任务'}》${stateText}`,
          kind: 'notice',
        })
        break
      }
      case 'error':
        errorMsg.value = ev.error || '语音服务出错'
        state.value = 'error'
        // Terminal guard: a backend `error` followed by a close on the SAME
        // connection must not spin the reconnect loop (permanent failures —
        // ASR 未配置 / server draining — would otherwise burn ~35s of retries).
        // Cleared by any later session activity (see 'state'/'ready').
        sawErrorEvent = true
        break
    }
  }

  function handleMessage(event: MessageEvent) {
    if (typeof event.data === 'string') {
      try {
        handleEvent(JSON.parse(event.data) as VoiceEvent)
      } catch { /* ignore */ }
    } else if (event.data instanceof ArrayBuffer) {
      schedulePcm16(event.data)
    }
  }

  async function connect(conversationId?: string | null, isReconnect = false): Promise<void> {
    if (!isReconnect) {
      errorMsg.value = null
      state.value = 'connecting'
    }
    // Tear down any previous live socket BEFORE adopting the new one, so two
    // live sockets can never coexist and a dying socket's handlers can never
    // touch the new session's state (I-4).
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      const dead = socket
      socket = null
      dead.onopen = null
      dead.onmessage = null
      dead.onerror = null
      dead.onclose = null
      try { dead.close() } catch { /* ignore */ }
    }
    const epoch = sessionEpoch
    return new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(getVoiceWebSocketUrl(undefined, conversationId))
      ws.binaryType = 'arraybuffer'
      socket = ws
      let settled = false
      const timer = setTimeout(() => {
        // Per-promise `settled` (not the shared `connected` ref): if another
        // socket flipped `connected` meanwhile, this stale attempt must still
        // time out instead of hanging forever with reconnecting stuck true (M-1).
        if (!settled) {
          settled = true
          if (!isReconnect) {
            errorMsg.value = '连接语音服务超时'
            state.value = 'error'
          }
          reject(new Error('连接超时'))
          // A handshake that never completes produces no close event; close it
          // ourselves so the retry chain takes over (initial connects included —
          // the old "late-ready rescue" is superseded by auto-retry).
          try { ws.close() } catch { /* ignore */ }
        }
      }, 12000)
      ws.onopen = () => {
        if (epoch !== sessionEpoch) return
        if (playCtx && playCtx.state === 'suspended') void playCtx.resume()
      }
      ws.onmessage = (e) => {
        if (epoch !== sessionEpoch) {
          // Orphan of a superseded generation: close it out without touching state.
          try { ws.close() } catch { /* ignore */ }
          return
        }
        handleMessage(e)
        if (connected.value && !settled) {
          settled = true
          clearTimeout(timer)
          resolve()
        }
      }
      ws.onerror = () => {
        if (epoch !== sessionEpoch) return
        if (!settled) {
          settled = true
          clearTimeout(timer)
          reject(new Error('连接失败'))
        }
        // State bookkeeping only for the initial connect; a reconnect's error
        // is followed by onclose, which drives the retry schedule.
        if (!isReconnect && !connected.value) {
          errorMsg.value = '语音服务连接失败'
          state.value = 'error'
        }
      }
      ws.onclose = (ev) => {
        if (epoch !== sessionEpoch) return // superseded generation owns the state now
        if (!settled) {
          settled = true
          clearTimeout(timer)
          reject(new Error('连接已关闭'))
        }
        handleSocketClose(ev)
      }
    })
  }

  async function start(conversationId?: string | null) {
    // Fresh generation: invalidate any in-flight reconnect of the previous one.
    sessionEpoch += 1
    stopReconnectTimer()
    stopFlushTimer()
    reconnecting = false
    reconnectAttempt = 0
    userClosed = false
    sawErrorEvent = false
    terminalClose = false
    micIntentionallyOff = false
    // A fresh session never carries audio/text buffered by a previous one.
    pendingChunks = []
    pendingClientEvents = []
    clearSpillState()
    lastConversationId = conversationId ?? null
    await connect(conversationId)
    try {
      await ensureCapture()
    } catch (err) {
      // Surface mic-capture failures (permission denied / no device / insecure
      // context) into the error state — otherwise the page silently shows a
      // "mic off" button while the orb looks like it is listening.
      surfaceCaptureError(err)
      throw err
    }
  }

  function sendText(text: string) {
    const trimmed = text.trim()
    if (!trimmed) return
    // `connected` (ready received) — not merely OPEN — is the send gate: the
    // handshake-to-ready window would otherwise let a fresh message overtake
    // text queued during the outage (M3).
    if (socket && socket.readyState === WebSocket.OPEN && connected.value) {
      socket.send(JSON.stringify({ event: 'text', text: trimmed }))
      return
    }
    if (socket || reconnecting || reconnectTimer !== null) {
      // Weak-network window: queue instead of silently dropping the message —
      // delivered (in order) as soon as the new session is ready.
      queueClientEvent({ event: 'text', text: trimmed })
    }
  }

  function interrupt() {
    // Local teardown is unconditional: the user's tap must stop audio/UI even
    // while the socket is down (previously a silent no-op in that window).
    droppingAudio = true
    clearPlayback()
    speaking.value = false
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ event: 'interrupt' }))
      return
    }
    if (reconnecting || reconnectTimer !== null) {
      // The fresh session may start replaying a queued notice — carry the tap over.
      queueClientEvent({ event: 'interrupt' })
    }
  }

  function toggleMic() {
    if (micOn.value) {
      micIntentionallyOff = true
      stopCapture()
      return
    }
    micIntentionallyOff = false
    void ensureCapture()
      .then(() => {
        if (state.value === 'error') {
          state.value = 'listen'
          errorMsg.value = null
        }
      })
      .catch(surfaceCaptureError)
  }

  function disconnect() {
    // User-initiated teardown: kill the reconnect machine (epoch bump
    // invalidates any in-flight attempt), its timer, and every buffer of this
    // generation (memory + spilled audio + queued text events).
    userClosed = true
    sessionEpoch += 1
    stopReconnectTimer()
    stopFlushTimer()
    reconnecting = false
    reconnectAttempt = 0
    sawErrorEvent = false
    terminalClose = false
    pendingChunks = []
    pendingClientEvents = []
    clearSpillState()
    if (socket) {
      try {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ event: 'stop' }))
        }
        socket.onmessage = null
        socket.onclose = null
        socket.onerror = null
        socket.close()
      } catch { /* ignore */ }
      socket = null
    }
    stopCapture()
    clearPlayback()
    if (playCtx) {
      void playCtx.close()
      playCtx = null
    }
    connected.value = false
    state.value = 'idle'
  }

  onUnmounted(() => {
    disconnect()
  })

  // ---- session management ----
  async function loadSessions() {
    try {
      sessions.value = await getVoiceSessions()
    } catch { /* ignore */ }
  }

  async function startNewSession() {
    disconnect()
    messages.value = []
    currentSessionId.value = null
    partialText.value = ''
    showSessionPanel.value = false
    try {
      const session = await createVoiceSession()
      currentSessionId.value = session.id
      await start(session.id)
      await loadSessions()
    } catch { /* error surfaced via state */ }
  }

  async function switchSession(sessionId: string) {
    if (sessionId === currentSessionId.value && connected.value) {
      showSessionPanel.value = false
      return
    }
    disconnect()
    messages.value = []
    partialText.value = ''
    showSessionPanel.value = false
    currentSessionId.value = sessionId
    try {
      await start(sessionId)
      await loadSessions()
    } catch { /* error surfaced via state */ }
  }

  // Test hooks: exposed unconditionally on window (same convention as the
  // pre-existing __voiceTestSend used by voice_mode_changes.spec.ts, which
  // feature-detects them). Read-only probe + text-send shim for WS-level e2e
  // (voice_duplex_reconnect.spec.ts); neither alters runtime behavior.
  if (typeof window !== 'undefined') {
    ;(window as any).__voiceTestSend = (text: string) => sendText(text)
    // Read-only resilience probe for e2e (voice_duplex_reconnect.spec.ts):
    // connection/mic/buffer state without touching behavior.
    ;(window as any).__voiceTestState = () => ({
      state: state.value,
      connected: connected.value,
      micOn: micOn.value,
      messages: messages.value.map(m => ({ role: m.role, text: m.text, streaming: !!m.streaming })),
      buffered: pendingChunks.length,
      queuedEvents: pendingClientEvents.length,
      spillCount: getSpilledCount(),
      reconnectAttempt,
      sessionId: currentSessionId.value,
    })
  }

  return {
    state: computed(() => state.value),
    connected: computed(() => connected.value),
    micOn: computed(() => micOn.value),
    speaking: computed(() => speaking.value),
    playbackProgress: computed(() => playbackProgress.value),
    errorMsg: computed(() => errorMsg.value),
    partialText: computed(() => partialText.value),
    messages: computed(() => messages.value),
    ttsAvailable: computed(() => ttsAvailable.value),
    micLevel: computed(() => micLevel.value),
    currentSessionId: computed(() => currentSessionId.value),
    voiceAssistantId: computed(() => voiceAssistantId.value),
    emotion: computed(() => emotion.value),
    interjectionText: computed(() => interjectionText.value),
    sessions: computed(() => sessions.value),
    showSessionPanel,
    start,
    disconnect,
    sendText,
    interrupt,
    toggleMic,
    loadSessions,
    startNewSession,
    switchSession,
  }
}
