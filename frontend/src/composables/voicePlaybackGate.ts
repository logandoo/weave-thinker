// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Owns which server turn may produce audible audio on this WebSocket.
 * Epochs cancel forward-only; sequences deduplicate audio without relying on
 * callback timing. A null epoch is the legacy anonymous-binary compatibility
 * path and never relaxes the rules of an identified turn.
 */
export class VoicePlaybackGate {
  private activeEpoch: number | null = null
  private cancelledThrough = 0
  private pausedEpoch: number | null = null
  private lastSequence = 0
  private legacyDropped = false

  reset(): void {
    this.activeEpoch = null
    this.cancelledThrough = 0
    this.pausedEpoch = null
    this.lastSequence = 0
    this.legacyDropped = false
  }

  get currentEpoch(): number | null {
    return this.activeEpoch && this.activeEpoch > 0 ? this.activeEpoch : null
  }

  start(epoch: number | null): boolean {
    if (epoch === null) {
      this.activeEpoch = 0
      this.pausedEpoch = null
      this.legacyDropped = false
      return true
    }
    if (!Number.isInteger(epoch) || epoch <= 0) return false
    if (epoch <= this.cancelledThrough) return false
    if (this.activeEpoch !== null && epoch < this.activeEpoch) return false
    if (this.pausedEpoch === epoch) return false
    this.activeEpoch = epoch
    this.pausedEpoch = null
    this.legacyDropped = false
    return true
  }

  pause(epoch: number | null): boolean {
    if (epoch === null) {
      if (this.legacyDropped) return false
      this.pausedEpoch = this.activeEpoch ?? 0
      return true
    }
    if (!Number.isInteger(epoch) || epoch <= 0) return false
    if (epoch <= this.cancelledThrough || this.activeEpoch !== epoch) return false
    this.pausedEpoch = epoch
    return true
  }

  resume(epoch: number | null): boolean {
    if (epoch === null) {
      const active = this.activeEpoch ?? 0
      if (this.legacyDropped || this.pausedEpoch !== active) return false
      this.pausedEpoch = null
      return true
    }
    if (!Number.isInteger(epoch) || epoch <= 0) return false
    if (epoch <= this.cancelledThrough) return false
    if (this.activeEpoch !== epoch || this.pausedEpoch !== epoch) return false
    this.pausedEpoch = null
    return true
  }

  cancel(epoch: number | null): boolean {
    if (epoch === null) {
      if (this.activeEpoch !== null && this.activeEpoch > 0) {
        this.cancelledThrough = Math.max(this.cancelledThrough, this.activeEpoch)
      }
      this.activeEpoch = 0
      this.pausedEpoch = null
      this.legacyDropped = true
      return true
    }
    if (!Number.isInteger(epoch) || epoch <= 0) return false
    if (epoch < this.cancelledThrough) return false
    if (this.activeEpoch !== null && epoch < this.activeEpoch) return false
    this.cancelledThrough = Math.max(this.cancelledThrough, epoch)
    if (this.pausedEpoch !== null && this.pausedEpoch <= epoch) this.pausedEpoch = null
    if (this.activeEpoch === 0) this.legacyDropped = true
    return true
  }

  acceptAudio(epoch: number | null, sequence: number | null): boolean {
    if (epoch === null || sequence === null) return false
    if (!Number.isInteger(epoch) || !Number.isInteger(sequence) || epoch <= 0 || sequence <= 0) return false
    if (epoch !== this.activeEpoch || epoch <= this.cancelledThrough) return false
    if (sequence <= this.lastSequence) return false
    // A sequence rejected during pause is still consumed. Resume uses newly
    // synthesized PCM with fresh server sequences, never a rejected replay.
    this.lastSequence = sequence
    if (this.pausedEpoch === epoch) return false
    return true
  }

  acceptLegacyAudio(): boolean {
    return this.activeEpoch === 0 && !this.legacyDropped && this.pausedEpoch === null
  }

  isStale(epoch: number | null): boolean {
    return epoch !== null && this.activeEpoch !== null && epoch < this.activeEpoch
  }
}
