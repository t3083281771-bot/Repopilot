<script setup lang="ts">
defineProps<{ name: string; size?: number }>()
const paths: Record<string, string> = {
  pilot: 'M5 18 10 4l9 8-14 6ZM10 4l1 10 8-2',
  grid: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  chart: 'M4 4v16h16M8 15v-4M12 15V7M16 15V9',
  box: 'm12 3 9 5-9 5-9-5 9-5Zm-9 5v9l9 5 9-5V8M12 13v9',
  plus: 'M12 5v14M5 12h14',
  chevron: 'm9 5 7 7-7 7',
  down: 'm6 9 6 6 6-6',
  git: 'M8 6a3 3 0 1 0 0 .1M8 9v6M8 15a3 3 0 1 0 0 .1M16 5a3 3 0 1 0 0 .1M8 12h3a5 5 0 0 0 5-5',
  code: 'm8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16',
  arrow: 'M5 12h14m-6-6 6 6-6 6',
  sparkle: 'm12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3Z',
  search: 'M20 20l-5-5M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z',
  file: 'M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8M8 16h5',
  clock: 'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  check: 'm5 12 4 4L19 6',
  close: 'm6 6 12 12M6 18 18 6',
  refresh: 'M20 7v5h-5M4 17v-5h5M5.4 8a7 7 0 0 1 11.5-3L20 8M4 16l3.1 3A7 7 0 0 0 18.6 16',
  info: 'M12 11v5M12 7v.1M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  warning: 'M12 9v4M12 17v.1M12 3l10 18H2L12 3Z',
  link: 'm10 13 4-4M8 16l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m0 12a4 4 0 0 0 6 0l5-5a4 4 0 0 0-6-6l-1 1',
  layers: 'm12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5',
  terminal: 'm5 7 5 5-5 5M13 17h6',
  shield: 'm12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Zm-4 9 3 3 5-6',
  copy: 'M8 8h12v12H8zM16 8V4H4v12h4',
  external: 'M14 3h7v7M21 3 11 13M10 3H3v18h18v-7',
}
</script>

<template>
  <svg :width="size ?? 18" :height="size ?? 18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path :d="paths[name] ?? paths.code" />
  </svg>
</template>
