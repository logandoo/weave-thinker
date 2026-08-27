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
import { RNNoiseNode, rnnoise_loadAssets } from 'simple-rnnoise-wasm'

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

  // Periodically report the real playback position to the backend and detect
  // when the burst fully drains. WebAudio's clock advances even in headless /
  // silent output, so this works regardless of actual sound rendering.
  function startProgressTimer() {
    stopProgressTimer()
    progressTimer = setInterval(() => {
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
          socket.send(JSON.stringify({ event: 'playback_drained' }))
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
    if (playCtx) {
      nextStartTime = playCtx.currentTime + 0.02
    } else {
      nextStartTime = 0
    }
    stopProgressTimer()
    resetBurstTiming()
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
  const PROX_NEAR_RATIO = 4.0 // rms >= floor * ratio → near candidate
  const PROX_NEAR_MIN = 0.02 // absolute floor of the near threshold
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
      if (!socket || socket.readyState !== WebSocket.OPEN || !micOn.value) {
        micLevel.value = 0
        return
      }
      const mono = downmixToMono(e.inputBuffer)
      // Compute RMS amplitude to drive the listening-halo pulse animation
      // so the user can visually tell whether their voice is being captured.
      let sumSq = 0
      for (let i = 0; i < mono.length; i += 1) sumSq += mono[i] * mono[i]
      const rms = Math.sqrt(sumSq / Math.max(1, mono.length))
      // Normalize: typical speech RMS ~0.05-0.3; map to 0-1 with soft curve
      const level = Math.min(1, rms * 3.5)
      micLevel.value = level
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
      const resampled = resample(mono, e.inputBuffer.sampleRate, 16000)
      if (resampled.length === 0) return
      if (socket.bufferedAmount < 256 * 1024) {
        socket.send(float32ToLEBuffer(resampled))
      }
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
  function handleEvent(ev: VoiceEvent) {
    switch (ev.event) {
      case 'ready':
        connected.value = true
        ttsAvailable.value = ev.tts !== false
        state.value = 'listen'
        // Late-ready recovery: if capture isn't running (e.g. the 12s connect
        // timeout fired while the backend waited on a slow ASR upstream), start
        // the mic now so the session isn't left "listening" with the mic off.
        if (!micOn.value) {
          void ensureCapture().catch(surfaceCaptureError)
        }
        break
      case 'session':
        if (ev.session_id) currentSessionId.value = ev.session_id
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
      case 'speaking_start':
        droppingAudio = false
        speaking.value = true
        resetBurstTiming()
        startProgressTimer()
        break
      case 'speaking_end':
        speaking.value = false
        stopProgressTimer()
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
      case 'playback_resumed':
        // Answer playback continues from the breakpoint (backchannel/defer).
        // The resumed audio is a NEW burst: restart the playback clock so
        // played_sec stays relative to the resume point. The backend anchors
        // its pause-breakpoint math to this (burst_base_chars + played_sec);
        // without the reset, played_sec would keep counting from the
        // pre-pause burst and the backend would double-count (and truncate).
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
      case 'bg_task_notice': {
        // A background task submitted from voice reached a terminal state —
        // the assistant is about to announce it aloud and offer follow-ups.
        const done = ev.status === 'completed'
        pushMessage({
          role: 'system',
          text: done
            ? `后台任务《${ev.title || '未命名任务'}》已完成`
            : `后台任务《${ev.title || '未命名任务'}》执行失败`,
          kind: 'notice',
        })
        break
      }
      case 'error':
        errorMsg.value = ev.error || '语音服务出错'
        state.value = 'error'
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

  async function connect(conversationId?: string | null) {
    errorMsg.value = null
    state.value = 'connecting'
    return new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(getVoiceWebSocketUrl(undefined, conversationId))
      ws.binaryType = 'arraybuffer'
      socket = ws
      const timer = setTimeout(() => {
        if (!connected.value) {
          errorMsg.value = '连接语音服务超时'
          state.value = 'error'
          reject(new Error('连接超时'))
        }
      }, 12000)
      ws.onopen = () => {
        if (playCtx && playCtx.state === 'suspended') void playCtx.resume()
      }
      ws.onmessage = (e) => {
        handleMessage(e)
        if (connected.value) {
          clearTimeout(timer)
          resolve()
        }
      }
      ws.onerror = () => {
        clearTimeout(timer)
        errorMsg.value = '语音服务连接失败'
        state.value = 'error'
        reject(new Error('连接失败'))
      }
      ws.onclose = () => {
        clearTimeout(timer)
        connected.value = false
      }
    })
  }

  async function start(conversationId?: string | null) {
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
    if (!socket || socket.readyState !== WebSocket.OPEN || !text.trim()) return
    socket.send(JSON.stringify({ event: 'text', text: text.trim() }))
  }

  function interrupt() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return
    socket.send(JSON.stringify({ event: 'interrupt' }))
    droppingAudio = true
    clearPlayback()
    speaking.value = false
  }

  function toggleMic() {
    if (micOn.value) {
      stopCapture()
      return
    }
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

  // Test hook: expose sendText on window for Playwright WS-level testing.
  // Non-invasive: only active when the page sets window.__VOICE_TEST_MODE.
  if (typeof window !== 'undefined') {
    ;(window as any).__voiceTestSend = (text: string) => sendText(text)
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
