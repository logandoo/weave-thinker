// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { computed, onUnmounted, ref } from 'vue'
import { getAsrWebSocketUrl, type ASRStreamEventPayload, type ASRStreamStartPayload, type HotwordItem } from '@/api/asr'

type PartialHandler = (payload: ASRStreamEventPayload) => void
type FinalHandler = (payload: ASRStreamEventPayload) => void
type ErrorHandler = (message: string) => void

interface UseAsrStreamingOptions {
  startPayload?: Omit<ASRStreamStartPayload, 'event' | 'custom_hotwords'>
  customHotwords?: HotwordItem[] | (() => HotwordItem[])
  onPartial?: PartialHandler
  onFinal?: FinalHandler
  onError?: ErrorHandler
}

interface PendingFinal {
  resolve: (payload: ASRStreamEventPayload | null) => void
  reject: (error: Error) => void
}

function getAudioContextCtor(): typeof AudioContext {
  const ctor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!ctor) {
    throw new Error('当前浏览器不支持音频录制')
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
  for (let index = 0; index < sampleCount; index += 1) {
    let sample = 0
    for (let channel = 0; channel < channelCount; channel += 1) {
      sample += buffer.getChannelData(channel)[index]
    }
    mono[index] = sample / channelCount
  }

  return mono
}

function resampleTo16kHz(input: Float32Array, sourceRate: number): Float32Array {
  if (sourceRate === 16000) {
    return input
  }

  const ratio = sourceRate / 16000
  const outputLength = Math.max(1, Math.round(input.length / ratio))
  const output = new Float32Array(outputLength)

  for (let index = 0; index < outputLength; index += 1) {
    const position = index * ratio
    const lower = Math.floor(position)
    const upper = Math.min(lower + 1, input.length - 1)
    const weight = position - lower
    output[index] = input[lower] * (1 - weight) + input[upper] * weight
  }

  return output
}

function float32ToLittleEndianBuffer(input: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(input.length * 4)
  const view = new DataView(buffer)

  for (let index = 0; index < input.length; index += 1) {
    view.setFloat32(index * 4, input[index], true)
  }

  return buffer
}

const BACKPRESSURE_THRESHOLD = 256 * 1024 // 256KB
const FLUSH_INTERVAL_MS = 100
const FINISH_TIMEOUT_MS = 60_000
const READY_TIMEOUT_MS = 10_000
const MAX_RECONNECT_ATTEMPTS = 10
const RECONNECT_BASE_DELAY_MS = 500
const RECONNECT_MAX_DELAY_MS = 5000

export function useAsrStreaming(options: UseAsrStreamingOptions = {}) {
  const isRecording = ref(false)
  const isFinishing = ref(false)
  const partialText = ref('')
  const finalText = ref('')
  const error = ref<string | null>(null)

  let mediaStream: MediaStream | null = null
  let audioContext: AudioContext | null = null
  let sourceNode: MediaStreamAudioSourceNode | null = null
  let processorNode: ScriptProcessorNode | null = null
  let muteNode: GainNode | null = null
  let socket: WebSocket | null = null
  let pendingFinal: PendingFinal | null = null
  let pendingReady: { resolve: () => void; reject: (error: Error) => void } | null = null
  let pendingChunks: ArrayBuffer[] = []
  let flushTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectAttempt = 0
  let reconnecting = false

  function stopFlushTimer() {
    if (flushTimer !== null) {
      clearTimeout(flushTimer)
      flushTimer = null
    }
  }

  function stopReconnectTimer() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  function flushPendingChunks() {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      pendingChunks = []
      stopFlushTimer()
      return
    }

    while (pendingChunks.length > 0 && socket.bufferedAmount < BACKPRESSURE_THRESHOLD) {
      socket.send(pendingChunks.shift()!)
    }

    if (pendingChunks.length > 0) {
      flushTimer = setTimeout(flushPendingChunks, FLUSH_INTERVAL_MS)
    } else {
      stopFlushTimer()
    }
  }

  function closeAudioGraph() {
    processorNode?.disconnect()
    sourceNode?.disconnect()
    muteNode?.disconnect()

    processorNode = null
    sourceNode = null
    muteNode = null

    mediaStream?.getTracks().forEach(track => track.stop())
    mediaStream = null

    if (audioContext) {
      void audioContext.close()
      audioContext = null
    }
  }

  function rejectPendingFinal(message: string) {
    if (pendingFinal) {
      pendingFinal.reject(new Error(message))
      pendingFinal = null
    }
  }

  function rejectPendingReady(message: string) {
    if (pendingReady) {
      pendingReady.reject(new Error(message))
      pendingReady = null
    }
  }

  function clearSocket() {
    if (!socket) {
      return
    }

    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null

    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close()
    }

    socket = null
  }

  function cleanupState() {
    isRecording.value = false
    isFinishing.value = false
  }

  function resetTranscriptState() {
    partialText.value = ''
    finalText.value = ''
    error.value = null
  }

  function handleSocketMessage(event: MessageEvent) {
    let payload: ASRStreamEventPayload

    let rawData: string
    if (typeof event.data === 'string') {
      rawData = event.data
    } else if (event.data instanceof ArrayBuffer) {
      rawData = new TextDecoder().decode(event.data)
    } else {
      return
    }

    try {
      payload = JSON.parse(rawData) as ASRStreamEventPayload
    } catch {
      return
    }

    if (payload.event === 'ready') {
      if (pendingReady) {
        pendingReady.resolve()
        pendingReady = null
      }
      return
    }

    if (payload.text) {
      partialText.value = payload.text
      finalText.value = payload.text
    }

    if (payload.event === 'partial' || payload.event === 'segment') {
      // Backend already accumulates text across VAD sentence boundaries,
      // so we can forward the payload directly without local accumulation.
      options.onPartial?.(payload)
      return
    }

    if (payload.event === 'final') {
      options.onFinal?.(payload)
      if (pendingFinal) {
        pendingFinal.resolve(payload)
        pendingFinal = null
      }
      reconnectAttempt = 0
      cleanupState()
      clearSocket()
      return
    }

    if (payload.event === 'error') {
      const message = payload.error || '语音识别失败'
      error.value = message
      options.onError?.(message)
      rejectPendingReady(message)
      rejectPendingFinal(message)
      cleanupState()
      clearSocket()
    }
  }

  function createSocket(): Promise<WebSocket> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(getAsrWebSocketUrl())
      ws.binaryType = 'arraybuffer'

      ws.onopen = () => {
        socket = ws
        ws.onmessage = handleSocketMessage
        ws.onerror = () => {
          const message = '语音识别连接失败'
          error.value = message
          options.onError?.(message)
          rejectPendingReady(message)
          rejectPendingFinal(message)
          cleanupState()
        }
        ws.onclose = handleSocketClose
        resolve(ws)
      }

      ws.onerror = () => {
        reject(new Error('语音识别连接失败'))
      }
    })
  }

  async function startSocket(startPayload?: Omit<ASRStreamStartPayload, 'event' | 'custom_hotwords'>, isReconnect = false): Promise<void> {
    const ws = await createSocket()
    const payload: ASRStreamStartPayload = {
      event: 'start',
      ...options.startPayload,
      ...startPayload,
    }
    const hotwords = typeof options.customHotwords === 'function'
      ? options.customHotwords()
      : options.customHotwords
    if (hotwords && hotwords.length > 0) {
      payload.custom_hotwords = hotwords
    }
    ws.send(JSON.stringify(payload))

    try {
      await new Promise<void>((resolve, reject) => {
        pendingReady = { resolve, reject }
        setTimeout(() => {
          if (pendingReady) {
            pendingReady = null
            reject(new Error('ASR服务就绪超时'))
          }
        }, READY_TIMEOUT_MS)
      })
    } catch (cause) {
      clearSocket()
      throw cause
    }

    if (isReconnect) {
      reconnecting = false
      reconnectAttempt = 0
    }
  }

  function scheduleReconnect() {
    stopReconnectTimer()
    if (reconnectAttempt >= MAX_RECONNECT_ATTEMPTS) {
      const message = '语音识别连接断开，重连失败'
      error.value = message
      options.onError?.(message)
      rejectPendingReady(message)
      rejectPendingFinal(message)
      cleanupState()
      closeAudioGraph()
      clearSocket()
      return
    }
    const delay = Math.min(
      RECONNECT_BASE_DELAY_MS * (2 ** reconnectAttempt),
      RECONNECT_MAX_DELAY_MS,
    )
    reconnectTimer = setTimeout(() => {
      void reconnect()
    }, delay)
  }

  async function reconnect() {
    if (reconnecting) return
    reconnecting = true
    reconnectAttempt += 1
    try {
      await startSocket(undefined, true)
      reconnecting = false
      if (audioContext && audioContext.state !== 'closed' && sourceNode) {
        isRecording.value = true
      } else {
        await stop()
      }
    } catch {
      reconnecting = false
      if (isRecording.value || isFinishing.value) {
        scheduleReconnect()
      } else {
        cleanupState()
        closeAudioGraph()
        clearSocket()
      }
    }
  }

  function handleSocketClose(closeEvent: CloseEvent) {
    const closedByClient = closeEvent.code === 1000 || closeEvent.code === 1005
    if (!closedByClient && isRecording.value) {
      if (!error.value) {
        const message = closeEvent.reason || '语音识别连接已断开'
        error.value = message
        options.onError?.(message)
        rejectPendingReady(message)
        rejectPendingFinal(message)
      } else if (pendingReady || pendingFinal) {
        rejectPendingReady(error.value)
        rejectPendingFinal(error.value)
      }
      scheduleReconnect()
      return
    }

    if (!closedByClient && isFinishing.value) {
      const message = closeEvent.reason || '语音识别连接已断开'
      error.value = message
      options.onError?.(message)
      rejectPendingReady(message)
      rejectPendingFinal(message)
    }

    cleanupState()
    closeAudioGraph()
    clearSocket()
  }

  async function connectAudioProcessing() {
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
    } catch (err) {
      const name = err instanceof Error ? err.name : ''
      const message = err instanceof Error ? err.message : ''
      if (name === 'NotAllowedError' || message.includes('Permission denied')) {
        throw new Error('麦克风权限被拒绝，请允许浏览器访问麦克风')
      }
      if (name === 'NotFoundError' || message.includes('Requested device not found')) {
        throw new Error('未找到麦克风设备')
      }
      if (name === 'NotSupportedError' || message.includes('Not supported')) {
        throw new Error('当前浏览器或环境不支持录音，请使用支持麦克风的浏览器')
      }
      throw new Error(`无法访问麦克风: ${message || '未知错误'}`)
    }

    try {
      const AudioContextCtor = getAudioContextCtor()
      audioContext = new AudioContextCtor({ latencyHint: 'interactive' })
    } catch (err) {
      const message = err instanceof Error ? err.message : '未知错误'
      throw new Error(`音频处理初始化失败: ${message}`)
    }

    sourceNode = audioContext.createMediaStreamSource(mediaStream)
    processorNode = audioContext.createScriptProcessor(4096, sourceNode.channelCount, 1)
    muteNode = audioContext.createGain()
    muteNode.gain.value = 0

    processorNode.onaudioprocess = audioEvent => {
      if (!socket || socket.readyState !== WebSocket.OPEN || !isRecording.value) {
        return
      }

      const mono = downmixToMono(audioEvent.inputBuffer)
      const resampled = resampleTo16kHz(mono, audioEvent.inputBuffer.sampleRate)
      if (resampled.length === 0) {
        return
      }

      const chunk = float32ToLittleEndianBuffer(resampled)

      if (socket.bufferedAmount < BACKPRESSURE_THRESHOLD && pendingChunks.length === 0) {
        socket.send(chunk)
      } else {
        pendingChunks.push(chunk)
        if (flushTimer === null) {
          flushTimer = setTimeout(flushPendingChunks, FLUSH_INTERVAL_MS)
        }
      }
    }

    sourceNode.connect(processorNode)
    processorNode.connect(muteNode)
    muteNode.connect(audioContext.destination)
  }

  async function start(startPayload: Omit<ASRStreamStartPayload, 'event' | 'custom_hotwords'> = {}) {
    if (isRecording.value || isFinishing.value || reconnecting) {
      return
    }

    resetTranscriptState()
    reconnectAttempt = 0

    try {
      await startSocket(startPayload)
    } catch (cause) {
      clearSocket()
      throw cause
    }

    try {
      await connectAudioProcessing()
      isRecording.value = true
    } catch (cause) {
      clearSocket()
      closeAudioGraph()
      throw cause
    }
  }

  async function stop(): Promise<ASRStreamEventPayload | null> {
    stopReconnectTimer()
    reconnectAttempt = 0
    reconnecting = false

    if (!socket || socket.readyState !== WebSocket.OPEN || !isRecording.value) {
      return null
    }

    isRecording.value = false
    isFinishing.value = true
    closeAudioGraph()

    // Flush any remaining buffered audio before sending finish
    while (pendingChunks.length > 0 && socket.readyState === WebSocket.OPEN) {
      socket.send(pendingChunks.shift()!)
    }
    stopFlushTimer()

    const finalResult = new Promise<ASRStreamEventPayload | null>((resolve, reject) => {
      pendingFinal = { resolve, reject }
    })

    const timeoutPromise = new Promise<never>((_resolve, reject) => {
      setTimeout(() => {
        reject(new Error('语音识别超时，请重试'))
      }, FINISH_TIMEOUT_MS)
    })

    socket.send(JSON.stringify({ event: 'finish' }))

    try {
      return await Promise.race([finalResult, timeoutPromise])
    } catch (err) {
      // On timeout, clean up properly
      if (err instanceof Error && err.message === '语音识别超时，请重试') {
        error.value = err.message
        options.onError?.(err.message)
        pendingFinal = null
        cleanupState()
        clearSocket()
      }
      throw err
    }
  }

  function cancel() {
    reconnectAttempt = 0
    reconnecting = false
    stopReconnectTimer()
    rejectPendingReady('录音已取消')
    rejectPendingFinal('录音已取消')
    pendingChunks = []
    stopFlushTimer()
    closeAudioGraph()
    cleanupState()
    clearSocket()
  }

  onUnmounted(() => {
    stopReconnectTimer()
    cancel()
  })

  return {
    isRecording: computed(() => isRecording.value),
    isFinishing: computed(() => isFinishing.value),
    partialText: computed(() => partialText.value),
    finalText: computed(() => finalText.value),
    error: computed(() => error.value),
    start,
    stop,
    cancel,
  }
}