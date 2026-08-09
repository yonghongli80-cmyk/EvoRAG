<script setup lang="ts">
import { ref, watch } from 'vue'
import api from '../utils/api'
import { Search } from '@element-plus/icons-vue'

interface SearchResult {
  id: number
  title: string
  matched_snippet: string | null
  created_at: string
}

const emit = defineEmits<{
  (e: 'select', id: number): void
}>()

const query = ref('')
const results = ref<SearchResult[]>([])
const searching = ref(false)
const showResults = ref(false)

let searchTimer: ReturnType<typeof setTimeout> | null = null

watch(query, (val) => {
  if (searchTimer) clearTimeout(searchTimer)
  if (!val || val.length < 1) {
    results.value = []
    showResults.value = false
    return
  }
  searchTimer = setTimeout(async () => {
    searching.value = true
    try {
      const res = await api.get('/conversations/search', {
        params: { q: val, page: 1, size: 20 }
      })
      results.value = res.data?.results || []
      showResults.value = true
    } catch (err) {
      console.error('搜索失败:', err)
    } finally {
      searching.value = false
    }
  }, 300)
})

function highlightSnippet(snippet: string | null, q: string): string {
  if (!snippet) return ''
  // Escape HTML
  const escaped = snippet.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  // Highlight keywords
  const regex = new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi')
  return escaped.replace(regex, '<mark>$1</mark>')
}

function selectResult(id: number) {
  emit('select', id)
  query.value = ''
  results.value = []
  showResults.value = false
}

function onBlur() {
  setTimeout(() => { showResults.value = false }, 200)
}

function formatTime(ts: string) {
  if (!ts) return ''
  const d = new Date(ts)
  const now = new Date()
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}
</script>

<template>
  <div class="conversation-search">
    <div class="search-input-wrapper">
      <el-icon class="search-icon"><Search /></el-icon>
      <input
        v-model="query"
        class="search-input"
        placeholder="搜索对话..."
        @focus="showResults = results.length > 0"
        @blur="onBlur"
      />
    </div>

    <div v-if="showResults && results.length > 0" class="search-results">
      <div
        v-for="result in results"
        :key="result.id"
        class="search-result-item"
        @mousedown.prevent="selectResult(result.id)"
      >
        <div class="result-title">{{ result.title || '新对话' }}</div>
        <div v-if="result.matched_snippet" class="result-snippet" v-html="highlightSnippet(result.matched_snippet, query)"></div>
        <div class="result-time">{{ formatTime(result.created_at) }}</div>
      </div>
    </div>

    <div v-else-if="showResults && query && !searching" class="search-empty">
      未找到匹配的对话
    </div>
  </div>
</template>

<style scoped>
.conversation-search {
  position: relative;
  padding: 0 var(--space-3);
  margin-bottom: var(--space-2);
}

.search-input-wrapper {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: var(--color-muted);
  border-radius: var(--radius);
  border: 1px solid transparent;
  transition: all var(--duration-fast);
}

.search-input-wrapper:focus-within {
  border-color: var(--color-accent);
  background: var(--color-surface);
}

.search-icon {
  color: var(--color-secondary);
  font-size: 14px;
  flex-shrink: 0;
}

.search-input {
  flex: 1;
  border: none;
  background: none;
  outline: none;
  font-size: var(--text-sm);
  color: var(--color-foreground);
  font-family: var(--font-body);
}

.search-input::placeholder {
  color: var(--color-secondary);
}

.search-results {
  position: absolute;
  top: 100%;
  left: var(--space-3);
  right: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  max-height: 300px;
  overflow-y: auto;
  z-index: 100;
}

.search-result-item {
  padding: var(--space-3);
  cursor: pointer;
  border-bottom: 1px solid var(--color-border);
  transition: background var(--duration-fast);
}

.search-result-item:last-child {
  border-bottom: none;
}

.search-result-item:hover {
  background: var(--color-muted);
}

.result-title {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.result-snippet {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.result-snippet :deep(mark) {
  background: #fef9c3;
  color: #854d0e;
  border-radius: 2px;
  padding: 0 2px;
}

.result-time {
  font-size: 11px;
  color: var(--color-secondary);
  margin-top: 2px;
}

.search-empty {
  position: absolute;
  top: 100%;
  left: var(--space-3);
  right: var(--space-3);
  padding: var(--space-4);
  text-align: center;
  font-size: var(--text-sm);
  color: var(--color-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  z-index: 100;
}
</style>
