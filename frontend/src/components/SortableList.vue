<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div ref="containerRef" class="sortable-list">
    <slot />
  </div>
</template>

<script lang="ts">
// W26 拖拽全局态必须是**模块级**单例 —— <script setup> 顶层=每实例一份，
// 跨实例 destroy 守卫全失效（目标列表被毁时查的是它自己的 false 标志），且
// failsafe 无从复位。模块级才与 SortableJS 的模块级 _nulling 全局态对齐。
// itemById provides a robust fallback: even if `item` ref is lost during Vue
// re-renders (e.g. group expansion during drag), the Map persists.
// isDragging flag prevents reactive array mutations during drag — the parent
// (Sidebar) reconciles DOM state after drag ends via reconcileDragState().
const draggedItemStore: { item: any | null; sourceKey: string | null; itemById: Map<string, any>; isDragging: boolean; dragToken: number; sourceInstance: any | null } = {
  item: null,
  sourceKey: null,
  itemById: new Map(),
  isDragging: false,
  // W26（评审 B-M1）：拖拽世代令牌 —— onEnd 的 100ms 延迟清理不得清掉
  // 100ms 内新开始的拖拽状态（旧计时器闭包对活值点火）。
  dragToken: 0,
  // W26（评审 B-I2）：源实例身份 —— 源列表在拖拽中卸载（drawer v-if 关闭 /
  // 路由切换）时 end 永不派发，必须由卸载路径复位模块级全局态。
  sourceInstance: null,
}

/** W26 failsafe 复位出口：SortableJS 的 end 事件未到达时（destroy-during-drag
 *  等）onEnd 的清理也不会跑 —— Sidebar 的 forceEndDrag 经此复位全局态，
 *  否则 isDragging 永真会永久锁死全部实例的 re-init 与变更持久化。 */
export function resetGlobalDragState() {
  draggedItemStore.isDragging = false
  draggedItemStore.item = null
  draggedItemStore.sourceKey = null
  draggedItemStore.itemById.clear()
  draggedItemStore.sourceInstance = null
}
</script>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import Sortable from 'sortablejs'

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

  // W26 拖拽卡死根治（扼流守卫）：拖拽进行中禁止重建已有实例 ——
  // SortableJS 1.14.0 destroy() → _onDrop(undefined) → _nulling() 清空
  // **模块级全局**拖拽态（dragEl/tapEvt/Sortable.active），onEnd 永不派发，
  // isDragging 永真（上游 Vue.Draggable#1199 同源）。destroy 的唯一合法
  // 时机 = 非拖拽期（此处守卫之后）与组件卸载。尚无实例的懒初始化列表
  // （折叠组首次展开）允许纯创建 —— 无 destroy 即无 _nulling。
  if (draggedItemStore.isDragging && sortableInstance) return

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
      draggedItemStore.dragToken += 1
      draggedItemStore.sourceInstance = sortableInstance
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
      // W26（评审 B-M1）：世代令牌守卫 —— 延迟清理只对「自己这一代」拖拽生效，
      // 否则 100ms 内开始的下一次拖拽会被旧计时器清掉状态。
      const token = draggedItemStore.dragToken
      setTimeout(() => {
        if (draggedItemStore.dragToken !== token) return
        draggedItemStore.isDragging = false
        draggedItemStore.item = null
        draggedItemStore.sourceKey = null
        draggedItemStore.itemById.clear()
        draggedItemStore.sourceInstance = null
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
  // W26（评审 B-I2）：源列表在拖拽中卸载 → end 永不派发。只有源实例的卸载
  // 才允许复位模块级拖拽态（目标列表卸载不影响在途拖拽），否则全局守卫
  // 永真会永久锁死所有列表的 mutation 与重建。
  if (draggedItemStore.isDragging && draggedItemStore.sourceInstance === sortableInstance) {
    resetGlobalDragState()
  }
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
// W26：destroy 收敛进 initSortable 扼流守卫（拖拽中禁 destroy 任何实例，
// 不止源列表 —— _nulling() 清的是全局态，毁目标列表同样杀死在途拖拽）。
watch(() => props.forceReinit, () => {
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
