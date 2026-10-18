<script setup lang="ts">
import { computed, ref } from 'vue'
import { Search } from '@element-plus/icons-vue'
import type { WikiPage } from '../../api/wiki'
import { formatTime } from './helpers'

const props = defineProps<{
  pages: WikiPage[]
  activePageId: number | null
  loading?: boolean
  canEdit?: boolean
}>()

const collapsed = defineModel<boolean>('collapsed', { default: false })

const emit = defineEmits<{
  (e: 'select-page', pageId: number): void
  (e: 'open-create', defaultTitle?: string): void
}>()

const searchQuery = ref('')
const selectedTag = ref('')

const filteredPages = computed(() => {
  let list = props.pages || []
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.trim().toLowerCase()
    list = list.filter(p => p.title.toLowerCase().includes(q) || (p.slug && p.slug.toLowerCase().includes(q)))
  }
  if (selectedTag.value) {
    list = list.filter(p => (p.tags || []).includes(selectedTag.value))
  }
  return list
})

const allTags = computed(() => {
  const set = new Set<string>()
  for (const p of props.pages || []) {
    if (p.tags) {
      for (const t of p.tags) set.add(t)
    }
  }
  return Array.from(set)
})
</script>

<template>
  <aside :class="['wiki-sidebar', { collapsed }]">
    <!-- Collapsed dock -->
    <div v-if="collapsed" class="wiki-sidebar-collapsed-dock">
      <button class="expand-rail-btn" @click="collapsed = false" title="展开词条列表">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
      </button>
      <span class="collapsed-count">{{ filteredPages.length }}</span>
    </div>

    <!-- Expanded content -->
    <div v-else class="wiki-sidebar-content">
      <div class="sidebar-search">
        <el-input
          v-model="searchQuery"
          placeholder="检索 Wiki 词条..."
          :prefix-icon="Search"
          clearable
          size="small"
        />
        <button class="collapse-icon-btn" @click="collapsed = true" title="折叠词条列表">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="15 18 9 12 15 6"/>
          </svg>
        </button>
      </div>

      <!-- Tag filter pills -->
      <div v-if="allTags.length > 0" class="tags-filter-bar">
        <span
          :class="['tag-pill', { active: selectedTag === '' }]"
          @click="selectedTag = ''"
        >
          全部
        </span>
        <span
          v-for="tag in allTags"
          :key="tag"
          :class="['tag-pill', { active: selectedTag === tag }]"
          @click="selectedTag = selectedTag === tag ? '' : tag"
        >
          #{{ tag }}
        </span>
      </div>

      <!-- Pages List -->
      <div v-loading="props.loading" class="pages-list">
        <div
          v-for="p in filteredPages"
          :key="p.id"
          :class="['page-item', { active: props.activePageId === p.id }]"
          @click="emit('select-page', p.id)"
        >
          <div class="item-title-row">
            <span class="doc-icon">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
            </span>
            <span class="item-title" :title="p.title">{{ p.title }}</span>
          </div>
          <div class="item-meta-row">
            <span class="item-time">{{ formatTime(p.updated_at) }}</span>
            <div v-if="p.tags && p.tags.length > 0" class="item-tags">
              <span v-for="t in p.tags.slice(0, 2)" :key="t" class="mini-tag">#{{ t }}</span>
            </div>
          </div>
        </div>

        <div v-if="!props.loading && filteredPages.length === 0" class="empty-list">
          <span class="empty-icon">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            </svg>
          </span>
          <p>{{ searchQuery ? '未匹配到相关词条' : '暂无 Wiki 词条' }}</p>
          <el-button
            v-if="!searchQuery && props.canEdit"
            size="small"
            type="primary"
            link
            @click="emit('open-create')"
          >
            点击创建第一个词条
          </el-button>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.wiki-sidebar {
  width: 300px;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: width var(--duration-normal) var(--ease-out);
  position: relative;
  overflow: hidden;
}

.wiki-sidebar.collapsed {
  width: 52px;
}

.wiki-sidebar-collapsed-dock {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 14px 6px;
  gap: 12px;
  height: 100%;
}

.expand-rail-btn {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border-subtle);
  background: var(--color-surface);
  color: var(--color-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.expand-rail-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-primary);
  border-color: var(--color-primary-light);
}

.collapsed-count {
  font-size: 11px;
  font-weight: 700;
  color: var(--color-muted);
  background: var(--color-surface-2);
  padding: 2px 6px;
  border-radius: var(--radius-full);
}

.wiki-sidebar-content {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 300px;
}

.sidebar-search {
  padding: 12px;
  border-bottom: 1px solid var(--color-border);
  display: flex;
  align-items: center;
  gap: 8px;
}

.collapse-icon-btn {
  width: 24px;
  height: 24px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--color-muted);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all var(--duration-fast);
  padding: 0;
  flex-shrink: 0;
}

.collapse-icon-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
  border-color: var(--color-border-subtle);
}

.tags-filter-bar {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  padding: 8px 12px;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface-2);
}

.tag-pill {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  cursor: pointer;
  white-space: nowrap;
}

.tag-pill.active {
  background: var(--color-primary);
  color: #fff;
  border-color: var(--color-primary);
}

.pages-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.page-item {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  transition: all 0.15s ease;
  border: 1px solid transparent;
}

.page-item:hover {
  background: var(--color-surface-2);
}

.page-item.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.item-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.doc-icon {
  font-size: 14px;
}

.item-title {
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item-meta-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-secondary);
}

.item-tags {
  display: flex;
  gap: 4px;
}

.mini-tag {
  background: var(--color-surface-3);
  padding: 1px 4px;
  border-radius: 4px;
}

.empty-list {
  text-align: center;
  padding: 40px 16px;
  color: var(--color-secondary);
}

.empty-icon {
  font-size: 32px;
  display: block;
  margin-bottom: 8px;
}
</style>
