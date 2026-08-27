<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div ref="containerRef" class="sortable-list">
    <slot />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import Sortable from 'sortablejs'

// Global store for dragged items (shared between all SortableList instances)
// itemById provides a robust fallback: even if `item` ref is lost during Vue
// re-renders (e.g. group expansion during drag), the Map persists.
// isDragging flag prevents reactive array mutations during drag — the parent
// (Sidebar) reconciles DOM state after drag ends via reconcileDragState().
const draggedItemStore: { item: any | null; sourceKey: string | null; itemById: Map<string, any>; isDragging: boolean } = {
  item: null,
  sourceKey: null,
  itemById: new Map(),
  isDragging: false,
}

const props = defineProps<{
  modelValue?: any[]
  list?: any[]
  group?: string | { name: string; pull?: boolean | string; put?: boolean | string }
  itemKey?: string
  handle?: string
  disabled?: boolean
  ghostClass?: string
  animation?: number
  delay?: number
  sortKey?: string
  visible?: boolean
  sort?: boolean
  forceReinit?: number
}>()

const emit = defineEmits<{
  'update:modelValue': [value: any[]]
  start: [evt: any]
  end: [evt: any]
  add: [evt: any]
  remove: [evt: any]
  update: [evt: any]
  sort: [evt: any]
}>()

const containerRef = ref<HTMLElement | null>(null)
let sortableInstance: Sortable | null = null
let lazyObserver: IntersectionObserver | null = null
let resizeObserver: ResizeObserver | null = null
let lastHeight: number = 0

function getList(): any[] {
  return props.modelValue ?? props.list ?? []
}

function getItemKey(): string {
  return props.itemKey || 'id'
}

function findItemByKey(key: string): any | null {
  const list = getList()
  const itemKey = getItemKey()
  return list.find((item: any) => String(item[itemKey]) === key) || null
}

function initSortable() {
  if (!containerRef.value) return

  // If element is hidden (display:none), defer initialization until visible
  // This is critical for SortableJS to properly register drop zones
  if (containerRef.value.offsetParent === null) {
    if (!lazyObserver) {
      lazyObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && entry.target === containerRef.value) {
            lazyObserver?.disconnect()
            lazyObserver = null
            nextTick(() => initSortable())
            break
          }
        }
      }, { threshold: 0.01 })
      lazyObserver.observe(containerRef.value)
    }
    return
  }

  // Set up ResizeObserver to detect when container becomes visible (height > 0)
  // This handles collapsed groups that use grid-template-rows: 0fr
  if (!resizeObserver && containerRef.value) {
    lastHeight = containerRef.value.offsetHeight
    resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const newHeight = entry.contentRect.height || containerRef.value?.offsetHeight || 0
        // If height changed from 0 to non-zero, re-initialize SortableJS
        if (lastHeight === 0 && newHeight > 0) {
          lastHeight = newHeight
          nextTick(() => initSortable())
          return
        }
        lastHeight = newHeight
      }
    })
    resizeObserver.observe(containerRef.value)
  }

  if (sortableInstance) {
    sortableInstance.destroy()
    sortableInstance = null
  }

  const options: any = {
    group: props.group || 'default',
    animation: props.animation ?? 200,
    easing: 'cubic-bezier(0.25, 1, 0.5, 1)',
    ghostClass: props.ghostClass || 'sortable-ghost',
    chosenClass: 'sortable-chosen',
    dragClass: 'sortable-drag',
    fallbackClass: 'sortable-fallback',
    fallbackOnBody: true,
    draggable: '[data-draggable="true"]',
    forceFallback: true,
    sort: props.sort !== false,
    scroll: true,
    scrollSensitivity: 40,
    scrollSpeed: 8,
    bubbleScroll: true,
    removeCloneOnHide: true,
    onStart(evt: any) {
      draggedItemStore.isDragging = true
      document.body.style.userSelect = 'none'
      document.body.style.cursor = 'grabbing'
      const key = evt.item.getAttribute('data-key')
      if (key) {
        const found = findItemByKey(key)
        draggedItemStore.item = found
        draggedItemStore.sourceKey = props.sortKey || null
        if (found) {
          draggedItemStore.itemById.set(String(key), found)
        }
      }
      emit('start', evt)
    },
    onEnd(evt: any) {
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      setTimeout(() => {
        draggedItemStore.isDragging = false
        draggedItemStore.item = null
        draggedItemStore.sourceKey = null
        draggedItemStore.itemById.clear()
      }, 100)
      emit('end', evt)
    },
    onAdd(evt: any) {
      // During cross-list drag, skip reactive array mutation to prevent Vue
      // re-renders from undoing SortableJS DOM changes. The parent (Sidebar)
      // will reconcile via reconcileDragState() after drag ends.
      if (draggedItemStore.isDragging && !props.modelValue) {
        emit('add', evt)
        return
      }
      // Try the direct ref first; fall back to the ID map
      let draggedItem = draggedItemStore.item
      if (!draggedItem) {
        const key = evt.item.getAttribute('data-key')
        if (key) {
          draggedItem = draggedItemStore.itemById.get(key) || null
        }
      }
      if (draggedItem) {
        const list = getList()
        const newIndex = evt.newIndex !== undefined ? evt.newIndex : list.length
        list.splice(newIndex, 0, draggedItem)
        if (props.modelValue) {
          emit('update:modelValue', [...list])
        }
      }
      emit('add', evt)
    },
    onRemove(evt: any) {
      if (evt.pullMode === 'clone') return
      // During cross-list drag, skip reactive array mutation to prevent Vue
      // re-renders from undoing SortableJS DOM changes.
      if (draggedItemStore.isDragging && !props.modelValue) {
        // Still store the removed item for onAdd to find
        const key = evt.item.getAttribute('data-key')
        if (key && !draggedItemStore.item) {
          const list = getList()
          const itemKey = getItemKey()
          const found = list.find((i: any) => String(i[itemKey]) === key)
          if (found) {
            draggedItemStore.item = found
            draggedItemStore.itemById.set(String(key), found)
          }
        }
        emit('remove', evt)
        return
      }
      const list = getList()
      const itemKey = getItemKey()
      const key = evt.item.getAttribute('data-key')
      if (key) {
        const index = list.findIndex((i: any) => String(i[itemKey]) === key)
        if (index !== -1) {
          const removed = list.splice(index, 1)[0]
          // Ensure the item is in the global store for onAdd to find
          if (!draggedItemStore.item && removed) {
            draggedItemStore.item = removed
          }
          if (removed) {
            draggedItemStore.itemById.set(String(key), removed)
          }
          if (props.modelValue) {
            emit('update:modelValue', [...list])
          }
        }
      }
      emit('remove', evt)
    },
    onUpdate(evt: any) {
      // During drag, skip reactive array mutation to prevent Vue re-renders
      // from undoing SortableJS's in-list reordering. The parent (Sidebar)
      // will reconcile via reconcileDragState() after drag ends.
      if (draggedItemStore.isDragging) {
        emit('update', evt)
        return
      }
      const list = getList()
      if (evt.oldIndex !== undefined && evt.newIndex !== undefined) {
        const item = list.splice(evt.oldIndex, 1)[0]
        list.splice(evt.newIndex, 0, item)
        if (props.modelValue) {
          emit('update:modelValue', [...list])
        }
      }
      emit('update', evt)
    },
    onSort(evt: any) {
      emit('sort', evt)
    },
  }

  if (props.handle) {
    options.handle = props.handle
  }

  if (props.delay !== undefined) {
    options.delay = props.delay
  }

  if (props.disabled) {
    options.disabled = true
  }

  sortableInstance = Sortable.create(containerRef.value, options)
}

onMounted(() => {
  nextTick(() => {
    initSortable()
  })
})

onUnmounted(() => {
  if (sortableInstance) {
    sortableInstance.destroy()
    sortableInstance = null
  }
  if (lazyObserver) {
    lazyObserver.disconnect()
    lazyObserver = null
  }
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
})

watch(() => props.disabled, (disabled) => {
  if (sortableInstance) {
    sortableInstance.option('disabled', !!disabled)
  }
})

watch(() => props.group, () => {
  nextTick(() => initSortable())
})

// Force re-initialization when prop changes (e.g. parent expands collapsed groups during drag).
// IMPORTANT: Skip reinitialization for the source list to avoid destroying the active drag.
watch(() => props.forceReinit, () => {
  if (draggedItemStore.isDragging && draggedItemStore.sourceKey === props.sortKey) {
    return
  }
  if (sortableInstance) {
    sortableInstance.destroy()
    sortableInstance = null
  }
  if (lazyObserver) {
    lazyObserver.disconnect()
    lazyObserver = null
  }
  nextTick(() => initSortable())
})

// Re-initialize SortableJS when list data changes (e.g. after reconcileDragState).
// Skip during active drag to avoid interfering with SortableJS DOM manipulation.
watch(
  () => {
    const list = props.modelValue ?? props.list ?? []
    return list.map((item: any) => String(item[props.itemKey || 'id'])).join(',')
  },
  () => {
    if (draggedItemStore.isDragging) return
    nextTick(() => initSortable())
  }
)
</script>
