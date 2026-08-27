<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <!-- 形象令牌 --logo-* 由 main.css :root 提供默认（青野罗盘）；
       墨韵/黑白在各自 themes/*.css 覆写为 mockup login.html 的形象（固定色，深浅同值）。
       皮肤条件渲染一律走 CSS 令牌，禁止 JS 按 skin-id 分支（解耦审计红线）。 -->
  <svg class="logo-svg" :width="size" :height="size" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="logo-bg" x1="0" y1="0" x2="100" y2="100">
        <stop offset="0%" stop-color="var(--logo-tile-1, #ffffff)"/>
        <stop offset="100%" stop-color="var(--logo-tile-2, #eaf2e6)"/>
      </linearGradient>
    </defs>
    <rect width="100" height="100" rx="20" fill="url(#logo-bg)"/>
    <circle cx="50" cy="50" r="42" stroke="var(--logo-ring, #7AA35A)" stroke-width="var(--logo-ring-sw, 3)" fill="none"/>
    <circle cx="50" cy="50" r="34" stroke="var(--logo-dash, #cddbc8)" stroke-width="var(--logo-dash-sw, 1.5)" stroke-dasharray="3 3" fill="none"/>
    <line x1="50" y1="6" x2="50" y2="14" stroke="var(--logo-tick-n, #7AA35A)" stroke-width="var(--logo-tick-n-sw, 3)" stroke-linecap="round"/>
    <line x1="50" y1="86" x2="50" y2="94" stroke="var(--logo-tick-se, #9ABE7A)" stroke-width="var(--logo-tick-se-sw, 2.5)" stroke-linecap="round"/>
    <line x1="6" y1="50" x2="14" y2="50" stroke="var(--logo-tick-se, #9ABE7A)" stroke-width="var(--logo-tick-se-sw, 2.5)" stroke-linecap="round"/>
    <line x1="94" y1="50" x2="86" y2="50" stroke="var(--logo-tick-se, #9ABE7A)" stroke-width="var(--logo-tick-se-sw, 2.5)" stroke-linecap="round"/>
    <text x="50" y="22" text-anchor="middle" fill="var(--logo-letter, #7AA35A)" font-size="9" font-weight="var(--logo-letter-weight, 700)" font-family="var(--logo-letter-font, system-ui, sans-serif)">N</text>
    <polygon points="50,16 44,50 50,47 56,50" fill="var(--logo-needle-n, #d85c5c)"/>
    <polygon points="50,84 44,50 50,53 56,50" fill="var(--logo-needle-s, #2d3b24)"/>
    <circle cx="50" cy="50" r="4.5" fill="var(--logo-dot, #7AA35A)"/>
  </svg>
</template>

<script setup lang="ts">
defineProps<{
  size?: number
}>()
</script>
