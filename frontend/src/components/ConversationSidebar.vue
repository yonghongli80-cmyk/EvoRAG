<script setup lang="ts">
import { ref, watch } from 'vue'
import { useChatStore, type ChatMode } from '../stores/chat'
import { ElMessageBox } from 'element-plus'
import ConversationSearch from './ConversationSearch.vue'

const props = withDefaults(defineProps<{
  mode: ChatMode
  collapsed?: boolean
}>(), {
  collapsed: false
})

const emit = defineEmits<{
  (e: 'update:collapsed', value: boolean): void
  (e: 'select', convId: number): void
}>()

const chatStore = useChatStore()
const isCollapsed = ref(props.collapsed)

watch(() => props.collapsed, (val) => {
  isCollapsed.value = val
})

function toggleCollapse() {
  isCollapsed.value = !isCollapsed.value
  emit('update:collapsed', isCollapsed.value)
}

async function handleNewConversation() {
  await chatStore.createConversation(props.mode)
}

async function handleSelect(id: number) {
  await chatStore.selectConversation(id)
  emit('select', id)
}

async function handleDelete(id: number) {
  try {
    await ElMessageBox.confirm('确定删除此对话？删除后记录不可恢复。', '确认删除', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消'
    })
    await chatStore.deleteConversation(id)
  } catch {
    // User cancelled
  }
}

function formatTime(ts: string | number) {
  if (!ts) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  const now = new Date()
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}
</script>

<template>
  <aside :class="['conv-sidebar', { collapsed: isCollapsed }]">
    <!-- Collapsed mini rail -->
    <div v-if="isCollapsed" class="collapsed-rail">
      <button class="rail-btn" @click="toggleCollapse" title="展开对话历史 (Ctrl+B)">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
          <line x1="9" y1="3" x2="9" y2="21"/>
          <polyline points="14 9 17 12 14 15"/>
        </svg>
      </button>

      <button class="rail-btn rail-btn--primary" @click="handleNewConversation" title="新建对话">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="5" x2="12" y2="19"/>
          <line x1="5" y1="12" x2="19" y2="12"/>
        </svg>
      </button>

      <div class="rail-count" :title="`${chatStore.conversations.length} 个对话`">
        {{ chatStore.conversations.length }}
      </div>
    </div>

    <!-- Expanded full sidebar -->
    <div v-else class="expanded-panel">
      <div class="conv-sidebar-header">
        <div class="header-left">
          <span class="conv-sidebar-title">对话历史</span>
          <span class="conv-badge">{{ chatStore.conversations.length }}</span>
        </div>
        <div class="header-actions">
          <button class="new-chat-btn" @click="handleNewConversation" aria-label="新建对话">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            新建
          </button>
          <button class="collapse-btn" @click="toggleCollapse" title="收起侧栏">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
              <line x1="9" y1="3" x2="9" y2="21"/>
              <polyline points="15 15 12 12 15 9"/>
            </svg>
          </button>
        </div>
      </div>

      <div class="search-wrap">
        <ConversationSearch @select="handleSelect" />
      </div>

      <div class="conv-list">
        <div
          v-for="conv in chatStore.conversations"
          :key="conv.id"
          :class="['conv-item', { active: conv.id === chatStore.currentConvId }]"
          @click="handleSelect(conv.id)"
        >
          <div class="conv-icon">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
          </div>
          <div class="conv-info">
            <div class="conv-title" :title="conv.title || '新对话'">{{ conv.title || '新对话' }}</div>
            <div class="conv-time">{{ formatTime(conv.created_at) }}</div>
          </div>
          <button class="conv-delete" @click.stop="handleDelete(conv.id)" title="删除对话" aria-label="删除对话">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        <div v-if="!chatStore.conversations.length" class="empty-conv">
          <div class="empty-icon-box">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
          </div>
          <span>暂无对话历史</span>
          <small>点击上方“新建”开始</small>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.conv-sidebar {
  width: 240px;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: width var(--duration-normal) var(--ease-out);
  position: relative;
  overflow: hidden;
}

.conv-sidebar.collapsed {
  width: 52px;
}

/* ── Collapsed Mini Rail ───────────────────────────────── */
.collapsed-rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 12px 6px;
  gap: 10px;
  height: 100%;
}

.rail-btn {
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

.rail-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
  border-color: var(--color-border);
}

.rail-btn--primary {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border-color: rgba(99, 102, 241, 0.2);
}

.rail-btn--primary:hover {
  background: var(--color-primary);
  color: #ffffff;
}

.rail-count {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-muted);
  margin-top: auto;
  padding-bottom: 8px;
}

/* ── Expanded Full Panel ───────────────────────────────── */
.expanded-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 240px;
}

.conv-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 14px 10px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 6px;
}

.conv-sidebar-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--color-secondary);
  letter-spacing: 0.2px;
}

.conv-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--color-surface-2);
  color: var(--color-secondary);
  font-size: 10.5px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: var(--radius-full);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.new-chat-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  background: var(--color-primary);
  color: #ffffff;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.new-chat-btn:hover {
  background: var(--color-primary-hover);
  box-shadow: 0 2px 6px rgba(79, 70, 229, 0.25);
}

.collapse-btn {
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--color-muted);
  border-radius: var(--radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.collapse-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

.search-wrap {
  padding: 0 10px 8px;
}

/* ── Conversation List ────────────────────────────────── */
.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.conv-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  position: relative;
  background: transparent;
}

.conv-item:hover {
  background: var(--color-surface-2);
}

.conv-item.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.conv-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-muted);
  flex-shrink: 0;
}

.conv-item.active .conv-icon {
  color: var(--color-primary);
}

.conv-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.conv-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conv-item.active .conv-title {
  color: var(--color-primary-focus);
  font-weight: 600;
}

.conv-time {
  font-size: 11px;
  color: var(--color-muted);
}

.conv-delete {
  opacity: 0;
  width: 22px;
  height: 22px;
  border-radius: var(--radius-xs);
  border: none;
  background: transparent;
  color: var(--color-muted);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.conv-item:hover .conv-delete {
  opacity: 1;
}

.conv-delete:hover {
  background: var(--color-destructive-light);
  color: var(--color-destructive);
}

.empty-conv {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 16px;
  color: var(--color-muted);
  text-align: center;
  gap: 6px;
  font-size: 13px;
}

.empty-icon-box {
  width: 44px;
  height: 44px;
  border-radius: var(--radius-full);
  background: var(--color-surface-2);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 4px;
}

.empty-conv small {
  font-size: 11px;
  color: var(--color-muted);
}
</style>
