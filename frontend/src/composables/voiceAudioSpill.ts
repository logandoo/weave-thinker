// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * IndexedDB spill queue for voice mic frames produced while the duplex
 * WebSocket is down (weak-network window).
 *
 * Why: the in-memory ring in useVoiceDuplex deliberately bounds itself
 * (~100s of audio) so a dead network cannot grow the JS heap without limit.
 * Without a second medium that bound also bounds how much recorded speech a
 * long outage can preserve. This module spills the chunks that overflow the
 * ring onto disk (IndexedDB), capped at ~10 minutes, and hands them back in
 * recording order when the session reconnects.
 *
 * Guarantees / non-goals:
 * - Best effort: if IndexedDB is unavailable (private mode, quota, blocked),
 *   `spillChunk` resolves false and the caller keeps the old drop-oldest
 *   behavior. No exception ever escapes.
 * - Ordering: the store has out-of-line auto-increment keys; readwrite
 *   transactions on one store are serialized in creation order, so a cursor
 *   walk returns chunks oldest-first.
 * - Lifecycle: callers must clear the store when a recording generation ends
 *   (start / disconnect / retries exhausted) so spilled audio can never be
 *   replayed into a later session as ghost speech.
 */
const DB_NAME = 'weave-voice-spill'
const STORE = 'chunks'
const DB_VERSION = 1

/** ~10 minutes at ~85ms per chunk (4096 samples @ 48k). */
export const SPILL_MAX_CHUNKS = 7200

let dbPromise: Promise<IDBDatabase | null> | null = null
let spillBroken = false
let spillCount = 0

function openDb(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === 'undefined') {
    spillBroken = true
    return Promise.resolve(null)
  }
  if (!dbPromise) {
    dbPromise = new Promise((resolve) => {
      try {
        const req = indexedDB.open(DB_NAME, DB_VERSION)
        req.onupgradeneeded = () => {
          const db = req.result
          if (!db.objectStoreNames.contains(STORE)) {
            // Out-of-line auto-increment key: insertion order == cursor order.
            db.createObjectStore(STORE, { autoIncrement: true })
          }
        }
        req.onsuccess = () => resolve(req.result)
        req.onerror = () => { spillBroken = true; resolve(null) }
        req.onblocked = () => { spillBroken = true; resolve(null) }
      } catch {
        spillBroken = true
        resolve(null)
      }
    })
  }
  return dbPromise
}

export function isSpillBroken(): boolean {
  return spillBroken
}

export function getSpilledCount(): number {
  return spillCount
}

/** Reconcile the in-memory counter with the store (concurrency-safe bound). */
async function recountSpill(db: IDBDatabase): Promise<number> {
  try {
    const count = await new Promise<number>((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly')
      const req = tx.objectStore(STORE).count()
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(req.error)
    })
    spillCount = count
  } catch {
    // keep the previous estimate on failure
  }
  return spillCount
}

async function deleteOldest(db: IDBDatabase, count: number): Promise<void> {
  if (count <= 0) return
  await new Promise<void>((resolve) => {
    const tx = db.transaction(STORE, 'readwrite')
    const store = tx.objectStore(STORE)
    let deleted = 0
    const req = store.openCursor()
    req.onsuccess = () => {
      const cur = req.result
      if (!cur || deleted >= count) return
      cur.delete()
      deleted += 1
      cur.continue()
    }
    tx.oncomplete = () => resolve()
    tx.onerror = () => resolve()
    tx.onabort = () => resolve()
  })
  await recountSpill(db)
}

/**
 * Append one audio chunk to the spill store.
 * @returns true when the chunk is durable; false when spill is unavailable
 *          (the caller should then drop it, as before).
 */
export async function spillChunk(chunk: ArrayBuffer): Promise<boolean> {
  if (spillBroken) return false
  const db = await openDb()
  if (!db) return false
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite')
      tx.objectStore(STORE).add(chunk)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
      tx.onabort = () => reject(tx.error)
    })
  } catch {
    spillBroken = true
    console.warn('[voice] audio spill unavailable, falling back to drop-oldest')
    return false
  }
  // Recount instead of incrementing: puts/drains may interleave across
  // generations, and a drifted counter would either break the ~10 min bound
  // or make the store appear empty (M2).
  const total = await recountSpill(db)
  if (total > SPILL_MAX_CHUNKS) {
    // Hard bound (~10 min): evict the oldest spilled audio, never the newest.
    void deleteOldest(db, total - SPILL_MAX_CHUNKS)
  }
  return true
}

/** Read every spilled chunk in recording order and delete it atomically. */
export async function drainSpilledChunks(): Promise<ArrayBuffer[]> {
  const db = await openDb()
  if (!db) return []
  const out: ArrayBuffer[] = []
  try {
    await new Promise<void>((resolve) => {
      const tx = db.transaction(STORE, 'readwrite')
      const store = tx.objectStore(STORE)
      const req = store.openCursor()
      req.onsuccess = () => {
        const cur = req.result
        if (!cur) return
        out.push(cur.value as ArrayBuffer)
        cur.delete()
        cur.continue()
      }
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
      tx.onabort = () => resolve()
    })
    // Puts committed after the cursor snapshot may still be in the store;
    // recount instead of assuming zero (M2).
    await recountSpill(db)
  } catch {
    spillBroken = true
  }
  return out
}

/** Drop all spilled audio (new generation / teardown / retries exhausted). */
export async function clearSpilledChunks(): Promise<void> {
  const db = await openDb()
  if (!db) {
    spillCount = 0
    return
  }
  try {
    await new Promise<void>((resolve) => {
      const tx = db.transaction(STORE, 'readwrite')
      tx.objectStore(STORE).clear()
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
      tx.onabort = () => resolve()
    })
  } catch {
    spillBroken = true
  }
  await recountSpill(db)
}
