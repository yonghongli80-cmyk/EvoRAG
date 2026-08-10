<script setup lang="ts">
import { useChatStore } from '../stores/chat'
import DOMPurify from 'dompurify'
import ReliabilityBadge from './ReliabilityBadge.vue'

interface Message {
  role: string
  content: string
  sources?: Array<{ doc_name: string }>
  feedback?: string
  confidence_level?: 'high' | 'medium' | 'low' | 'unavailable'
  confidence_reasons?: string[]
}

const props = defineProps<{
  message: Message
  index: number
  question?: string
}>()

const emit = defineEmits<{
  (e: 'add-to-analysis', question: string, answer: string): void
}>()

const chatStore = useChatStore()

function formatContent(text: string): string {
  if (!text) return ''
  const escaped = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#x27;')
    .replace(/\n/g, '<br>')
    .replace(/\[([^\]]+)\]/g, '<span class="ref">$1</span>')
  return DOMPurify.sanitize(escaped, { ALLOWED_TAGS: ['br', 'span'], ALLOWED_ATTR: ['class'] })
}

function handleAddToAnalysis() {
  emit('add-to-analysis', props.question || '', props.message.content)
}
</script>

<template>
  <div :class="['msg', message.role]">
    <div v-if="message.role === 'user'" class="msg-avatar user-avatar">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
        <path d="M8 8a3 3 0 100-6 3 3 0 000 6zm5 6a5 5 0 00-10 0h10z"/>
      </svg>
    </div>
    <div v-else class="msg-avatar ai-avatar">R</div>

    <div class="msg-body">
      <div class="bubble" v-html="formatContent(message.content)" />

      <ReliabilityBadge
        v-if="message.role === 'assistant' && message.confidence_level"
        :level="message.confidence_level"
        :reasons="message.confidence_reasons"
      />

      <!-- Sources -->
      <div v-if="message.sources?.length" class="sources">
        <span v-for="(src, j) in message.sources" :key="j" class="source-chip">
          {{ j + 1 }}. {{ src.doc_name }}
        </span>
      </div>

      <!-- Actions -->
      <div v-if="message.role === 'assistant' && message.content" class="actions">
        <button
          :class="['action-btn', { active: message.feedback === 'positive' }]"
          @click="chatStore.sendFeedback(index, 'positive')"
          title="有用"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M4 13V6L7 2l4 4v7H4z"/>
          </svg>
        </button>
        <button
          :class="['action-btn', { active: message.feedback === 'negative' }]"
          @click="chatStore.sendFeedback(index, 'negative')"
          title="没用"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M10 1V8L7 12l-4-4V1h7z" transform="rotate(180 7 7)"/>
          </svg>
        </button>
        <button class="action-btn" @click="chatStore.regenerate(index)" :disabled="chatStore.isStreaming" title="重新生成">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M1 7a6 6 0 0111.5-2.3M13 7a6 6 0 01-11.5 2.3"/>
            <path d="M13 1v4h-4M1 13v-4h4"/>
          </svg>
        </button>
        <button class="action-btn action-btn--accent" @click="handleAddToAnalysis" title="添加到分析">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <rect x="1" y="1" width="12" height="12" rx="2"/>
            <path d="M4 7h6M7 4v6"/>
          </svg>
          <span class="action-label">添加到分析</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.msg {
  display: flex;
  gap: var(--space-4);
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: var(--space-3) var(--space-6);
  animation: slideUp 0.3s var(--ease-out);
}

.msg.user {
  flex-direction: row-reverse;
}

.msg-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 14px;
  font-weight: var(--font-bold);
}

.user-avatar {
  background: var(--color-accent-light);
  color: var(--color-accent);
}

.ai-avatar {
  background: var(--color-foreground);
  color: white;
  font-family: var(--font-mono);
  font-size: 13px;
}

.msg-body {
  flex: 1;
  min-width: 0;
  max-width: 85%;
}

.msg.user .msg-body {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

/* ── Bubble ───────────────────────────────────────────── */
.bubble {
  padding: var(--space-4) var(--space-5);
  border-radius: var(--radius-lg);
  font-size: var(--text-base);
  line-height: var(--leading-relaxed);
  word-break: break-word;
}

.msg.user .bubble {
  background: var(--color-foreground);
  color: white;
  border-bottom-right-radius: var(--radius-sm);
}

.msg.assistant .bubble {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-bottom-left-radius: var(--radius-sm);
}

:deep(.ref) {
  display: inline;
  padding: 1px 6px;
  background: var(--color-accent-light);
  color: var(--color-accent);
  border-radius: 4px;
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
}

/* ── Sources ──────────────────────────────────────────── */
.sources {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-3);
}

.source-chip {
  padding: var(--space-1) var(--space-3);
  background: var(--color-muted);
  color: var(--color-secondary);
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.source-chip:hover {
  background: var(--color-accent-light);
  color: var(--color-accent);
}

/* ── Actions ──────────────────────────────────────────── */
.actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-3);
  opacity: 0;
  transition: opacity var(--duration-fast);
}

.msg:hover .actions {
  opacity: 1;
}

.action-btn {
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  border-radius: var(--radius-sm);
  color: var(--color-secondary);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  padding: 0 var(--space-2);
  font-family: var(--font-body);
}

.action-btn:hover {
  border-color: var(--color-accent);
  color: var(--color-accent);
}

.action-btn.active {
  background: var(--color-accent);
  border-color: var(--color-accent);
  color: white;
}

.action-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.action-btn--accent {
  border-color: var(--color-accent-subtle);
  color: var(--color-accent);
  background: var(--color-accent-light);
}

.action-btn--accent:hover {
  background: var(--color-accent);
  color: white;
}

.action-label {
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  white-space: nowrap;
}
</style>
