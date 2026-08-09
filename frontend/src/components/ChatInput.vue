<script setup lang="ts">
import { ref } from 'vue'
import { useChatStore, type ChatMode } from '../stores/chat'

const props = defineProps<{
  mode?: ChatMode
  kbId?: string
}>()

const chatStore = useChatStore()
const inputText = ref('')

async function handleSend() {
  const q = inputText.value.trim()
  if (!q || chatStore.isStreaming) return
  inputText.value = ''
  await chatStore.sendMessage(q, props.mode, props.kbId)
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
}
</script>

<template>
  <div class="input-area">
    <div v-if="chatStore.selectedFile" class="scope-indicator">
      <span class="scope-icon">🔍</span>
      <span class="scope-name">{{ chatStore.selectedFile }}</span>
      <button class="scope-clear" @click="chatStore.selectFile(null)" title="清除筛选">✕</button>
    </div>
    <div class="input-box">
      <el-input
        v-model="inputText"
        type="textarea"
        :rows="1"
        :autosize="{ minRows: 1, maxRows: 5 }"
        placeholder="输入你的问题..."
        @keydown="handleKeydown"
        :disabled="chatStore.isStreaming"
      />
      <button class="send-btn" :disabled="!inputText.trim() || chatStore.isStreaming" @click="handleSend">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M16 2L8 10M16 2l-5 14-3-7-7-3 14-4z"/>
        </svg>
      </button>
    </div>
    <div class="input-hint">Enter 发送 · Shift + Enter 换行</div>
  </div>
</template>

<style scoped>
.input-area {
  padding: var(--space-4) var(--space-6) var(--space-6);
  background: linear-gradient(to top, var(--color-background) 60%, transparent);
}

/* ── Scope Indicator ─────────────────────────────────── */
.scope-indicator {
  max-width: var(--content-max-width);
  margin: 0 auto var(--space-2);
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-3);
  background: var(--color-accent-light);
  color: var(--color-accent);
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
}

.scope-icon {
  font-size: 12px;
}

.scope-name {
  max-width: 200px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scope-clear {
  width: 18px;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  color: var(--color-accent);
  border-radius: 50%;
  cursor: pointer;
  font-size: 11px;
  padding: 0;
  transition: all var(--duration-fast);
}

.scope-clear:hover {
  background: var(--color-accent);
  color: white;
}

.input-box {
  max-width: var(--content-max-width);
  margin: 0 auto;
  display: flex;
  gap: var(--space-3);
  align-items: flex-end;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-3) var(--space-3) var(--space-3) var(--space-4);
  box-shadow: var(--shadow);
  transition: all var(--duration-normal) var(--ease-out);
}

.input-box:focus-within {
  border-color: var(--color-accent);
  box-shadow: var(--shadow), 0 0 0 3px var(--color-accent-light);
}

:deep(.el-textarea__inner) {
  border: none !important;
  box-shadow: none !important;
  padding: var(--space-2) 0 !important;
  font-family: var(--font-body) !important;
  font-size: var(--text-base) !important;
  resize: none !important;
  background: transparent !important;
}

.send-btn {
  width: 40px;
  height: 40px;
  background: var(--color-accent);
  border: none;
  border-radius: var(--radius);
  color: white;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: all var(--duration-normal) var(--ease-out);
}

.send-btn:hover:not(:disabled) {
  background: var(--color-accent-hover);
  transform: translateY(-1px);
  box-shadow: var(--shadow-accent);
}

.send-btn:disabled {
  background: var(--color-border);
  cursor: not-allowed;
}

.input-hint {
  text-align: center;
  font-size: var(--text-xs);
  color: var(--color-secondary);
  margin-top: var(--space-2);
  opacity: 0.7;
}
</style>
