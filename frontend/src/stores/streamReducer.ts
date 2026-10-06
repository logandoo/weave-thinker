// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure stream-state reducer helpers (erasable-syntax TS only, import-free —
 * safe to unit-test in Node via type stripping).
 *
 * These encapsulate the ordering/consistency semantics of the live SSE stream
 * vs. the reconnect replay snapshot:
 *
 * - `mergeReplayIntoSequence`  — the resume snapshot must never REGRESS the
 *   live timeline. The live `displaySequence` and the snapshot can each hold
 *   chunks the other lacks (the buffer records events after the client
 *   receives them, so a disconnect leaves a broadcast-into-the-gap event in
 *   the live timeline but not the snapshot; deltas arriving while the client
 *   was down are in the snapshot but not the live timeline). Replacing the
 *   whole timeline with the snapshot makes already-answered content vanish
 *   mid-answer (observed: content disappears from the UI, refresh shows it
 *   saved in the DB). Merge instead: keep every live item, append snapshot
 *   items whose part_id is missing, and for a part present in BOTH take the
 *   longer content (superset semantics) per field.
 * - `pickStreamText`           — accumulators (content / reasoning) only ever
 *   grow to the longer of {live, snapshot}; never shrink.
 * - `shouldApplyRefresh`       — a server message-list snapshot is only
 *   applied when it is not strictly OLDER than the local list (guards the
 *   same-flow concurrent-refresh race: a pre-commit GET resolving after the
 *   post-commit one wipes the just-completed answer).
 */
import type { DisplaySequenceItem } from '../types'

/** Display-sequence item shape with the streaming-only fields the reducer
 *  merges (reasoning_content/result) and a permissive index signature. */
export interface TimelineItem extends DisplaySequenceItem {
  reasoning_content?: string
  result?: string
  [key: string]: unknown
}

export interface StreamTextAccumulator {
  /** Monotonic text accumulator. Pass the CURRENT live value. */
  live: string
  /** Reconnect snapshot value for the same accumulator. */
  snapshot: string
}

/** Keep the LONGER of live vs snapshot. Equal length: keep live (newer
 *  in-place mutations may have happened). */
export function pickStreamText({ live, snapshot }: StreamTextAccumulator): string {
  if (!snapshot) return live
  if (!live) return snapshot
  return live.length >= snapshot.length ? live : snapshot
}

/**
 * Refresh shrink-guard (conv 4e159a79, 2026-10-01): when a server snapshot
 * for the just-finalized message is a strict SUFFIX of what the client
 * actually displayed (i.e. the persisted row lost its HEAD), keep the longer
 * displayed content for rendering — the user watched those characters stream
 * in and a refresh must not delete them ("1-6 块被替换删除").
 *
 * Deliberately NARROW, so legitimate server-side trims still apply:
 *  - citation sanitize strips `[N]` markers MID-text  → not a suffix → server wins
 *  - orphan colon/punct strip removes ≤2 LEADING chars → below
 *    MIN_LOST_HEAD_CHARS → server wins
 *  - audit_reset / compression surgery: the client already dropped the same
 *    draft server-side-mirrored → contents equal or divergent → server wins
 */
export const MIN_LOST_HEAD_CHARS = 3

export function keepLongerDisplayed(
  local: string | null | undefined,
  server: string | null | undefined,
): string | null | undefined {
  if (
    local
    && server
    && local.length > server.length
    && local.length - server.length >= MIN_LOST_HEAD_CHARS
    && local.endsWith(server)
  ) {
    return local
  }
  return server
}

/** Live reasoning tail-window (conv 827a6f78 turn-B, 2026-09-03).
 *
 *  A 113k-char reasoning stream made StreamMarkdown's 80ms whole-tree v-html
 *  re-render explode in cost — the live view froze on a reasoning frame and
 *  the user saw "answer = only the thinking text" (DB stayed clean; refresh
 *  recovered). While a reasoning block is the ACTIVELY-streaming last item,
 *  render only its tail: bounded re-render cost + the streaming tail is what
 *  the user wants to watch anyway. Completed blocks and the persisted message
 *  render full content (non-string defense per the ingest contract). */
export const LIVE_REASONING_TAIL_CHARS = 6000

export function liveReasoningTail(
  content: unknown,
  isStreamingLast: boolean,
  cap: number = LIVE_REASONING_TAIL_CHARS,
): string {
  const c = typeof content === 'string' ? content : ''
  if (!isStreamingLast || c.length <= cap) return c
  return `…（思考已生成 ${c.length} 字，实时显示尾部，完成后展开全文）\n\n` + c.slice(-cap)
}

/** audit_reset client-side timeline surgery (conv 827a6f78 turn-B, 2026-09-03).
 *
 *  The backend's silent QC rejects the streamed draft and resets ITS
 *  accumulators; until now the client never heard about it, so s.content
 *  kept every rejected draft and the done-time answer bubble contained the
 *  whole mess (the clean DB row only appeared after a refresh). Contract:
 *  drop the draft TEXT items after the LAST tool card — tool cards and
 *  reasoning items are preserved (A4 parity: the backend keeps reasoning and
 *  tools on reset, only the rejected draft text goes). With no tool card at
 *  all, every text item is draft. */
export function dropDraftTextAfterLastTool(seq: Array<{ type?: string }> | null | undefined): {
  kept: Array<{ type?: string }>
  changed: boolean
} {
  const items = Array.isArray(seq) ? seq : []
  let lastTool = -1
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i] && items[i].type === 'tool_call') { lastTool = i; break }
  }
  const kept = lastTool >= 0
    ? items.filter((it, i) => i <= lastTool || it?.type !== 'text')
    : items.filter(it => it?.type !== 'text')
  return { kept, changed: kept.length !== items.length }
}

/** Field-level superset merge for a text-ish part present in both the live
 *  timeline and the snapshot: take the LONGER content. For delta-accumulated
 *  parts both values are prefixes of the same underlying text, so the longer
 *  one is the superset. */
export function mergePartContent(live: string | undefined, snapshot: string | undefined): string {
  const l = live || ''
  const s = snapshot || ''
  if (!s) return l
  if (!l) return s
  return l.length >= s.length ? l : s
}

/** Merge the resume replay's display_sequence into the live one.
 *
 *  Rules:
 *  - Every LIVE item is kept as-is (it is at least as new as the snapshot).
 *  - A snapshot item whose part_id is already present live is MERGED into the
 *    live item field-by-field: content / reasoning_content take the LONGER
 *    value (the snapshot may hold deltas that arrived while the client was
 *    disconnected — dropping them would leave a display hole), other fields
 *    fill in when the live item lacks them.
 *  - A snapshot item with a part_id the live timeline lacks is APPENDED.
 *  - Snapshot items WITHOUT a part_id (legacy shapes) are appended unless an
 *    identical (type, content) item already exists anywhere in the result.
 */
export function mergeReplayIntoSequence(
  currentItems: TimelineItem[],
  replayItems: TimelineItem[] | null | undefined,
): TimelineItem[] {
  const out: TimelineItem[] = [...currentItems]
  if (!Array.isArray(replayItems)) return out
  const byId = new Map<string, TimelineItem>()
  for (const it of currentItems) {
    if (it.part_id) byId.set(it.part_id, it)
  }
  for (const rp of replayItems) {
    if (!rp) continue
    if (rp.part_id) {
      const liveItem = byId.get(rp.part_id)
      if (liveItem) {
        // Same part in both: superset-merge the streaming fields, fill gaps
        // in the rest. Mutating the LIVE item keeps its reactive proxy valid.
        liveItem.content = mergePartContent(
          typeof liveItem.content === 'string' ? liveItem.content : '',
          typeof rp.content === 'string' ? rp.content : '',
        )
        for (const key of ['reasoning_content', 'result']) {
          const lv = liveItem[key]
          const sv = rp[key]
          if (typeof lv === 'string' && typeof sv === 'string') {
            liveItem[key] = mergePartContent(lv, sv)
          } else if (lv === undefined || lv === null || lv === '') {
            if (sv !== undefined && sv !== null) liveItem[key] = sv
          }
        }
        for (const key of ['status', 'title', 'error']) {
          if (liveItem[key] === undefined || liveItem[key] === null) {
            if (rp[key] !== undefined && rp[key] !== null) liveItem[key] = rp[key]
          }
        }
        continue
      }
      out.push({ ...rp })
      byId.set(rp.part_id, out[out.length - 1])
    } else {
      // Legacy (no part_id) items cannot be keyed. Dedup by identity
      // anywhere in the result (tail-only dedup double-renders [A,B]+[A,B]).
      const dup = out.some(it => !it.part_id && it.type === rp.type && it.content === rp.content)
      if (dup) continue
      out.push({ ...rp })
    }
  }
  return out
}

/** Is this local message id a store-synthesized placeholder (optimistic
 *  bubble / local fallback) rather than a server row? The server legitimately
 *  lacks these ids until the next refresh reconciles them. */
export function isSyntheticMessageId(id: string): boolean {
  return /^(temp-|local-abort-|resume-|resume-complete-|bg-)/.test(id)
}

/**
 * May a fetched server message-list snapshot replace the local list?
 *
 * The snapshot is strictly older than the local view when the newest local
 * message (a REAL id, not a synthetic placeholder) is absent from it.
 * Applying such a snapshot wipes messages the user can already see (the
 * completed answer pushed at `done` but not yet visible to a pre-commit GET).
 * Synthetic placeholders are exempt: the server legitimately lacks them until
 * the next refresh reconciles ids.
 */
export function shouldApplyRefresh(local: Array<{ id: string }>, server: Array<{ id: string }>): boolean {
  if (!Array.isArray(local) || local.length === 0) return true
  const newest = local[local.length - 1]
  if (!newest || typeof newest.id !== 'string') return true
  if (!Array.isArray(server)) return false
  if (server.some(m => m.id === newest.id)) return true
  if (isSyntheticMessageId(newest.id)) return true
  return false
}

// ─── 正文截断 UX 根治（2026-10-03，conv 8c03ff8e 事故修复）────────────────
//
// 红线（用户裁定）：回答正文永不静默截断。conv-open 波的 slim 桩把
// display_sequence[type=text].content 切成 500 字预览，前端曾直接渲染预览。
// A1（后端）已豁免正文段；本函数是前端兜底（A2）——存量桩（写入期已烘焙的
// 旧数据）靠这里换回全文：content_segments（桩内全量）→ message.content
// （任何读取模式都全量）逐级兜底，零网络请求。工具/思考预览保持原样
// （折叠卡后有展开取全文链路，且预览=性能收益，不回吐）。

/** B2 正文折叠已移除（2026-10-05 用户指令：正式回答绝对不可截断、折叠）。
 *  正文保真由 resolveDisplaySequence（换文愈合）+ forceFullBody（末条恒全量）
 *  承担；折叠/懒加载只作用于工具调用与思考过程块。 */

/** 非持久化（客户端合成）消息 id 族——追平判定时一律不计入。
 *  与 isSyntheticMessageId 的差集：interject-pending-/interject-committed-
 *  （插话排队/已提交物化行，同样不是服务器行 id）。 */
const NON_PERSISTED_ID = /^(temp-|local-abort-|resume-|resume-complete-|bg-|interject-pending-|interject-committed-)/

export interface TurnAnchor {
  /** 本轮发起前最后一条服务器（持久化）消息 id；空会话为 null。 */
  id: string | null
  /** 本轮期望新增的持久化消息数。发送=2（用户行+助手行）；停止/重新生成/
   *  编辑重发/恢复路径=1——尾部 assistant 判定是真正的闸门，need 只防
   *  「锚后零新增」的早退。 */
  need: number
}

/**
 * 分页感知的追平判定（2026-10-05 上游TODO项3）。
 *
 * 旧计数启发式 expectedMessageCount=内存长度+1 与分页瘦身刷新不相容：
 * 窗口化（MESSAGE_PAGE_SIZE=50）后列表长度被窗口夹住，length>=expected 在
 * ≥49 条历史会话上永不满足 → abort 快路径失灵、叠 local-abort-* 重复气泡。
 *
 * 锚定语义：锚之后出现 ≥need 条持久化消息且最新一条持久化消息为 assistant
 * → 追平。锚被挤出窗口（一轮涌入 ≥PAGE 条新消息的退化形态）→ 返回 null
 * （不可判定）——调用方须降级（全量拉取后再判），不得把 null 当追平。
 */
export function caughtUpAfterAnchor(
  list: Array<{ id: string; role?: string }>,
  anchor: TurnAnchor,
): boolean | null {
  const anchorIdx = anchor.id ? list.findIndex((m) => m.id === anchor.id) : -1
  if (anchor.id && anchorIdx < 0) return null
  const after = anchorIdx >= 0 ? list.slice(anchorIdx + 1) : list
  const persisted = after.filter((m) => !NON_PERSISTED_ID.test(m.id))
  if (persisted.length < anchor.need) return false
  return persisted[persisted.length - 1]?.role === 'assistant'
}

/**
 * 恢复路径（看门狗/启动恢复/切回前台）的追平判定（W3 项1 r2，2026-10-06）。
 *
 * 两版锚方案的教训：①「最新持久化 user」锚——当前轮 user 行未落库时会回退到
 * 上一轮 user 行，上一轮 assistant 满足尾部判定（假阳性）；②「任意角色最新
 * 持久化」锚——答案已落库且已入本地列表时锚=答案本身，after=[] → 硬 false
 * （假阴性：stuck streaming + 重复 local-abort 气泡）。两难的本质是锚把
 * 「上一轮尾部」与「本轮答案」混为一谈。
 *
 * 尾部形态判定（窗口无关，不需锚）：最新持久化行是 assistant，且其后再无
 * 合成（未落库）行——即 assistant 就是列表尾巴。
 *  - [u1,a1,temp-u2]（本轮未落库）→ 合成行在尾 → false ✓（原假阳性消除）
 *  - [u1,a1,u2',a2']（答案已落库）→ 尾部 assistant → true ✓（假阴性消除）
 *  - [u1,a1,u2']（U'已落库，答案未到）→ 尾部 user → false ✓
 *  - [u1,a1]（无合成行，本轮 user 行未提交窗口）→ true——与 W3 前旧锚行为一致
 *    （接受的历史语义；refresh 成功门在调用侧降低该窗口频率）。
 */
export function recoveryCaughtUp(list: Array<{ id: string; role?: string }>): boolean {
  let lastPersisted = -1
  let lastSynthetic = -1
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const id = list[i]?.id
    if (!id) continue
    if (NON_PERSISTED_ID.test(id)) {
      if (lastSynthetic < 0) lastSynthetic = i
    } else if (lastPersisted < 0) {
      lastPersisted = i
    }
    if (lastPersisted >= 0 && lastSynthetic >= 0) break
  }
  if (lastPersisted < 0 || lastSynthetic > lastPersisted) return false
  return list[lastPersisted]?.role === 'assistant'
}

/**
 * fullWindow 全量拉取后的收窗判定（W3 项3，2026-10-06）。
 *
 * 仅在**判为已追平**（verdict=true）时把整史收回分页窗——未追平时锚可能还在
 * 列表中，收窗会把它逐出窗口，令后续轮询永远无法判定（caught-up=null），
 * Phase-2 陈旧轮破后照旧叠 local-abort-* 重复气泡。未追平保持全量（锚保真），
 * 下次常规 refresh 自然重新窗口化。
 */
export function trimWindowAfterFullFetch<T>(list: T[], verdict: boolean, pageSize: number): T[] {
  if (!verdict || list.length <= pageSize) return list
  return list.slice(-pageSize)
}


/**
 * A2：把 `__truncated__` 的正文段（type==='text'）换回全文。
 *
 * 映射规则：text 段按出现序对应 content_segments[i]；无 content_segments
 * 时仅当全序列只有一个 text 段才回落 message.content（多段时 message.content
 * 是拼接体，无法可靠切分——保持预览+标记，诚实降级）。换回后清除
 * __truncated__/__size_bytes__（正文已全量，不再需要徽标数据）。
 * 工具/思考项原样返回（含截断标记——A3 徽标数据源）。
 */
export function resolveDisplaySequence(
  items: TimelineItem[] | null | undefined,
  contentSegments: string[] | null | undefined,
  messageContent: string | null | undefined,
): TimelineItem[] {
  const seq: TimelineItem[] = Array.isArray(items) ? items : []
  const segs: string[] = Array.isArray(contentSegments) ? contentSegments : []
  const body = typeof messageContent === 'string' ? messageContent : ''
  const textCount = seq.reduce((n, it) => n + (it && it.type === 'text' ? 1 : 0), 0)
  let textIdx = -1
  let out: TimelineItem[] | null = null
  for (let i = 0; i < seq.length; i++) {
    const it = seq[i]
    if (!it || it.type !== 'text') continue
    textIdx += 1
    if (!it.__truncated__) continue
    const seg = segs[textIdx]
    const full = typeof seg === 'string' && seg.length > 0
      ? seg
      : (textCount === 1 && body ? body : '')
    const curLen = typeof it.content === 'string' ? it.content.length : 0
    if (!full || full.length <= curLen) continue
    // 校验和守卫（评审 R1 I2 / R2 I4）：__size_bytes__ 是原文精确长度（字），
    // 位置映射错位（压缩路径曾整体清空 content_segments）时宁可保持预览+标记，
    // 也绝不把「错段」当全文渲染——错内容比截断更糟。无标记（legacy）退化为
    // 长度对比。
    const want = it.__size_bytes__
    // 码位对齐（复审 N1）：__size_bytes__ 记的是 Python len(v)（Unicode 码位），
    // JS string.length 是 UTF-16 码元——含 emoji 等非 BMP 字符时二者不等，
    // 必须用 Array.from 折算码位，否则正文永不愈合
    if (typeof want === 'number' && want > 0 && Array.from(full).length !== want) continue
    if (!out) out = seq.slice()
    const healed: TimelineItem = { ...it, content: full }
    delete healed.__truncated__
    delete healed.__size_bytes__
    out[i] = healed
  }
  return out || seq
}

/**
 * N2/criterion #8 终极兜底：末条回答的正文必须绝对完整。
 * 当仍有 text 段带 __truncated__（多段无 segments 的诚实降级态）而
 * message.content 可用时，把全部未愈合 text 段合并为一条全量正文
 * （保留工具/思考项原位）——message.content 永不外置、恒为全文。
 */
export function forceFullBody(
  items: TimelineItem[] | null | undefined,
  messageContent: string | null | undefined,
): TimelineItem[] {
  const seq: TimelineItem[] = Array.isArray(items) ? items : []
  const body = typeof messageContent === 'string' ? messageContent : ''
  if (!body) return seq
  const hasUnhealed = seq.some((it) => it && it.type === 'text' && it.__truncated__)
  if (!hasUnhealed) return seq
  const out: TimelineItem[] = []
  let mergedDone = false
  for (const it of seq) {
    if (it && it.type === 'text' && it.__truncated__) {
      if (!mergedDone) {
        out.push({ type: 'text', content: body })
        mergedDone = true
      }
      continue
    }
    out.push(it)
  }
  return out
}
