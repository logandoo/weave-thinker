<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
/**
 * Shared icon set for the notes toolbar (primary buttons + second-level menus).
 *
 * Icons are declarative shape lists rendered as inline SVG so both note hosts
 * (NoteEditor / ZenNotePanel) stay visually identical without duplicating path
 * markup. Stroke/fill follow `currentColor`, so skins keep working.
 */
import { defineComponent, h, type PropType } from 'vue'

type Shape = [tag: string, attrs: Record<string, string | number>]

const ICONS: Record<string, Shape[]> = {
  heading: [
    ['path', { d: 'M4 12h8' }],
    ['path', { d: 'M4 18V6' }],
    ['path', { d: 'M12 18V6' }],
  ],
  bold: [
    ['path', { d: 'M6 4h8a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z' }],
    ['path', { d: 'M6 12h9a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z' }],
  ],
  italic: [
    ['line', { x1: 19, y1: 4, x2: 10, y2: 4 }],
    ['line', { x1: 14, y1: 20, x2: 5, y2: 20 }],
    ['line', { x1: 15, y1: 4, x2: 9, y2: 20 }],
  ],
  character: [
    ['path', { d: 'M4 7V4h16v3' }],
    ['path', { d: 'M9 20h6' }],
    ['path', { d: 'M12 4v16' }],
  ],
  paragraph: [
    ['path', { d: 'M13 4v16' }],
    ['path', { d: 'M17 4v16' }],
    ['path', { d: 'M19 4H9.5a3.5 3.5 0 0 0 0 7H15' }],
  ],
  tools: [
    ['path', { d: 'M4 21v-6' }],
    ['path', { d: 'M4 11V3' }],
    ['path', { d: 'M12 21v-9' }],
    ['path', { d: 'M12 8V3' }],
    ['path', { d: 'M20 21v-4' }],
    ['path', { d: 'M20 13V3' }],
    ['path', { d: 'M2 15h4' }],
    ['path', { d: 'M10 8h4' }],
    ['path', { d: 'M18 13h4' }],
  ],
  list: [
    ['path', { d: 'M9 6h12' }],
    ['path', { d: 'M9 12h12' }],
    ['path', { d: 'M9 18h12' }],
    ['circle', { cx: 4, cy: 6, r: 1, fill: 'currentColor', stroke: 'none' }],
    ['circle', { cx: 4, cy: 12, r: 1, fill: 'currentColor', stroke: 'none' }],
    ['circle', { cx: 4, cy: 18, r: 1, fill: 'currentColor', stroke: 'none' }],
  ],
  insert: [
    ['path', { d: 'M12 5v14' }],
    ['path', { d: 'M5 12h14' }],
  ],
  voice: [
    ['path', { d: 'M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z' }],
    ['path', { d: 'M19 11v1a7 7 0 0 1-14 0v-1' }],
    ['line', { x1: 12, y1: 19, x2: 12, y2: 23 }],
    ['line', { x1: 8, y1: 23, x2: 16, y2: 23 }],
  ],
  highlight: [
    ['path', { d: 'M9 11l-6 6v3h4l6-6' }],
    ['path', { d: 'M22 13l-5-5-4 4 5 5z' }],
    ['path', { d: 'M13 20h9' }],
  ],
  fontColor: [
    ['path', { d: 'M4 20h16' }],
    ['path', { d: 'M6 16l6-12 6 12' }],
    ['path', { d: 'M9 11h6' }],
  ],
  superscript: [
    ['path', { d: 'M4 18L9 6l5 12' }],
    ['path', { d: 'M17 4h4v4' }],
    ['path', { d: 'M17 8h4' }],
  ],
  subscript: [
    ['path', { d: 'M4 16L9 4l5 12' }],
    ['path', { d: 'M17 16h4v4' }],
    ['path', { d: 'M17 20h4' }],
  ],
  clearFormat: [
    ['path', { d: 'M20 20H8l-5-5a2 2 0 0 1 0-2.8l7-7a2 2 0 0 1 2.8 0l5 5a2 2 0 0 1 0 2.8L12 20' }],
    ['path', { d: 'M7 12l7 7' }],
  ],
  quote: [
    ['path', { d: 'M6 17h3l2-4V7H5v6h3z' }],
    ['path', { d: 'M14 17h3l2-4V7h-6v6h3z' }],
  ],
  alignLeft: [
    ['line', { x1: 3, y1: 6, x2: 21, y2: 6 }],
    ['line', { x1: 3, y1: 12, x2: 13, y2: 12 }],
    ['line', { x1: 3, y1: 18, x2: 17, y2: 18 }],
  ],
  alignCenter: [
    ['line', { x1: 3, y1: 6, x2: 21, y2: 6 }],
    ['line', { x1: 6, y1: 12, x2: 18, y2: 12 }],
    ['line', { x1: 4, y1: 18, x2: 20, y2: 18 }],
  ],
  alignRight: [
    ['line', { x1: 3, y1: 6, x2: 21, y2: 6 }],
    ['line', { x1: 11, y1: 12, x2: 21, y2: 12 }],
    ['line', { x1: 7, y1: 18, x2: 21, y2: 18 }],
  ],
  search: [
    ['circle', { cx: 11, cy: 11, r: 7 }],
    ['line', { x1: 16.5, y1: 16.5, x2: 21, y2: 21 }],
  ],
  replace: [
    ['path', { d: 'M17 1l4 4-4 4' }],
    ['path', { d: 'M3 11V9a4 4 0 0 1 4-4h14' }],
    ['path', { d: 'M7 23l-4-4 4-4' }],
    ['path', { d: 'M21 13v2a4 4 0 0 1-4 4H3' }],
  ],
  toc: [
    ['path', { d: 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20' }],
    ['path', { d: 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z' }],
    ['path', { d: 'M9 7h7' }],
    ['path', { d: 'M9 11h5' }],
  ],
  orderedList: [
    ['path', { d: 'M10 6h11' }],
    ['path', { d: 'M10 12h11' }],
    ['path', { d: 'M10 18h11' }],
    ['path', { d: 'M4 6h1v4' }],
    ['path', { d: 'M4 10h2' }],
    ['path', { d: 'M6 18H4c0-1 2-2 2-3s-1-1.5-2-1' }],
  ],
  restartNumbering: [
    ['path', { d: 'M3 12a9 9 0 1 0 3-6.7' }],
    ['path', { d: 'M3 3v6h6' }],
    ['path', { d: 'M11 12h6' }],
  ],
  link: [
    ['path', { d: 'M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1 1' }],
    ['path', { d: 'M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1-1' }],
  ],
  math: [
    ['path', { d: 'M18 4H6l6 8-6 8h12' }],
  ],
  code: [
    ['path', { d: 'M16 18l6-6-6-6' }],
    ['path', { d: 'M8 6l-6 6 6 6' }],
  ],
  image: [
    ['rect', { x: 3, y: 3, width: 18, height: 18, rx: 2, ry: 2 }],
    ['circle', { cx: 8.5, cy: 8.5, r: 1.5 }],
    ['path', { d: 'M21 15l-5-5L5 21' }],
  ],
  audio: [
    ['path', { d: 'M9 18V5l12-2v13' }],
    ['circle', { cx: 6, cy: 18, r: 3 }],
    ['circle', { cx: 18, cy: 16, r: 3 }],
  ],
  video: [
    ['path', { d: 'M22.5 6.4a2.8 2.8 0 0 0-1.9-2C18.9 4 12 4 12 4s-6.9 0-8.6.4a2.8 2.8 0 0 0-1.9 2A29 29 0 0 0 1 11.8a29 29 0 0 0 .5 5.3 2.8 2.8 0 0 0 1.9 2C5.1 19.5 12 19.5 12 19.5s6.9 0 8.6-.4a2.8 2.8 0 0 0 1.9-2 29 29 0 0 0 .5-5.3 29 29 0 0 0-.5-5.4z' }],
    ['polygon', { points: '9.8 15 15.5 11.8 9.8 8.5 9.8 15' }],
  ],
  endnote: [
    ['path', { d: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z' }],
    ['polyline', { points: '14 2 14 8 20 8' }],
    ['path', { d: 'M8 12h1.5v5' }],
    ['path', { d: 'M8 17h3' }],
    ['path', { d: 'M14 12h3v3h-3' }],
  ],
  stop: [
    ['rect', { x: 6, y: 6, width: 12, height: 12, rx: 2, fill: 'currentColor' }],
  ],
  close: [
    ['path', { d: 'M18 6L6 18' }],
    ['path', { d: 'M6 6l12 12' }],
  ],
}

export default defineComponent({
  name: 'NoteToolbarIcon',
  props: {
    name: { type: String as PropType<string>, required: true },
    size: { type: Number, default: 16 },
  },
  setup(props) {
    return () => h(
      'svg',
      {
        width: props.size,
        height: props.size,
        viewBox: '0 0 24 24',
        fill: 'none',
        stroke: 'currentColor',
        'stroke-width': 2,
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
        'aria-hidden': 'true',
        focusable: 'false',
      },
      (ICONS[props.name] || []).map(([tag, attrs]) => h(tag, attrs)),
    )
  },
})
</script>
