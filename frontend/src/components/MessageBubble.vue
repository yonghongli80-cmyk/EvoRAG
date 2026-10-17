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
  model?: string
  arenaResponses?: Record<string, string>
  structuredResult?: {
    kind: string
    facts: Array<{ label: string; value: string }>
    sources: Array<{ dataset_id: number; sheet_key: string; cell_range: string }>
    rowsScanned: number
    clarification?: { question: string }
  }
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

function getModelBadgeIcon(modelId: string): string {
  const m = modelId.toLowerCase()
  if (m.includes('deepseek')) return '⚡'
  if (m.includes('qwen')) return '🧠'
  if (m.includes('gpt') || m.includes('openai')) return '✨'
  return '🤖'
}
</script>

<template>
  <div :class="['msg', message.role]">
    <div v-if="message.role === 'user'" class="msg-avatar user-avatar">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
        <circle cx="12" cy="7" r="4"/>
      </svg>
    </div>
    <div v-else class="msg-avatar ai-avatar">R</div>

    <div class="msg-body">
      <!-- Arena Multi-Model Side-by-Side Grid -->
      <div v-if="message.arenaResponses && Object.keys(message.arenaResponses).length > 1" class="arena-container" data-test="arena-grid">
        <div v-for="(text, modelName) in message.arenaResponses" :key="modelName" class="arena-card">
          <div class="arena-card-header">
            <span class="arena-tag">
              <span class="arena-tag-icon">{{ getModelBadgeIcon(String(modelName)) }}</span>
              {{ modelName }}
            </span>
            <span class="arena-status-dot" title="实时回答已就绪" />
          </div>
          <div class="bubble arena-bubble" v-html="formatContent(text)" />
        </div>
      </div>

      <!-- Standard Single Model Bubble -->
      <div v-else class="bubble" v-html="formatContent(message.content)" />

      <!-- Structured Facts Table -->
      <div v-if="message.structuredResult?.facts.length" class="structured-result" data-test="structured-facts">
        <div class="structured-header">
          <span class="structured-title">结构化知识事实</span>
          <span class="structured-scanned">扫描 {{ message.structuredResult.rowsScanned }} 行</span>
        </div>
        <div class="structured-fact-grid">
          <div v-for="fact in message.structuredResult.facts" :key="fact.label" class="structured-fact">
            <span class="fact-label">{{ fact.label }}</span>
            <strong class="fact-value">{{ fact.value }}</strong>
          </div>
        </div>
        <div v-if="message.structuredResult.sources.length" class="structured-sources">
          <span v-for="source in message.structuredResult.sources" :key="`${source.dataset_id}-${source.cell_range}`">
            {{ source.sheet_key }} · {{ source.cell_range }}
          </span>
        </div>
      </div>
      <div v-else-if="message.structuredResult?.clarification" class="structured-clarification" data-test="structured-clarification">
        {{ message.structuredResult.clarification.question }}
      </div>

      <!-- Reliability Badge -->
      <ReliabilityBadge
        v-if="message.role === 'assistant' && message.confidence_level"
        :level="message.confidence_level"
        :reasons="message.confidence_reasons"
      />

      <!-- Source References -->
      <div v-if="message.sources?.length" class="sources">
        <span v-for="(src, j) in message.sources" :key="j" class="source-chip">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          </svg>
          {{ j + 1 }}. {{ src.doc_name }}
        </span>
      </div>

      <!-- Action Toolbar -->
      <div v-if="message.role === 'assistant' && message.content" class="actions">
        <button
          :class="['action-btn', { active: message.feedback === 'positive' }]"
          @click="chatStore.sendFeedback(index, 'positive')"
          title="回答有用"
          aria-label="赞同"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>
          </svg>
        </button>

        <button
          :class="['action-btn', { active: message.feedback === 'negative' }]"
          @click="chatStore.sendFeedback(index, 'negative')"
          title="回答不准确"
          aria-label="反对"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/>
          </svg>
        </button>

        <button
          class="action-btn"
          @click="chatStore.regenerate(index)"
          :disabled="chatStore.isStreaming"
          title="重新生成"
          aria-label="重新生成"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10"/>
            <polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
          </svg>
        </button>

        <button
          class="action-btn action-btn--accent"
          @click="handleAddToAnalysis"
          title="添加到深度分析工作台"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
            <line x1="12" y1="8" x2="12" y2="16"/>
            <line x1="8" y1="12" x2="16" y2="12"/>
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
  gap: 14px;
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: 12px 24px;
  animation: slideUp 0.25s var(--ease-out);
}

.msg.user {
  flex-direction: row-reverse;
}

.msg-avatar {
  width: 34px;
  height: 34px;
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 13.5px;
  font-weight: 700;
  box-shadow: var(--shadow-xs);
}

.user-avatar {
  background: var(--color-surface-3);
  color: var(--color-secondary);
}

.ai-avatar {
  background: linear-gradient(135deg, #4f46e5 0%, #6366f1 100%);
  color: #ffffff;
  box-shadow: 0 2px 8px rgba(79, 70, 229, 0.28);
}

.msg-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.msg.user .msg-body {
  align-items: flex-end;
}

/* ── Standard Bubble ──────────────────────────────────── */
.bubble {
  padding: 12px 18px;
  border-radius: var(--radius-xl);
  font-size: 14.5px;
  line-height: 1.65;
  word-break: break-word;
  color: var(--color-foreground);
}

.msg.user .bubble {
  background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%);
  color: #ffffff;
  border-radius: 18px 18px 4px 18px;
  box-shadow: 0 2px 8px rgba(79, 70, 229, 0.25);
  max-width: 80%;
}

.msg.assistant .bubble {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px 18px 18px 18px;
  box-shadow: var(--shadow-card);
}

:deep(.ref) {
  display: inline-block;
  padding: 1px 6px;
  margin: 0 2px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  border-radius: var(--radius-xs);
  font-size: 12px;
  font-weight: 600;
}

/* ── Arena Dual/Multi Column View ─────────────────────── */
.arena-container {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
  gap: 14px;
  width: 100%;
  margin-bottom: 6px;
}

.arena-card {
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 14px;
  box-shadow: var(--shadow-card);
  transition: all var(--duration-fast) var(--ease-out);
}

.arena-card:hover {
  border-color: rgba(99, 102, 241, 0.35);
  box-shadow: var(--shadow-md);
}

.arena-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.arena-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  font-size: 12px;
  font-weight: 600;
  border-radius: var(--radius-full);
  background: var(--color-primary-light);
  color: var(--color-primary);
  letter-spacing: 0.3px;
}

.arena-tag-icon {
  font-size: 13px;
}

.arena-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-success);
  box-shadow: 0 0 6px rgba(16, 185, 129, 0.5);
}

.arena-bubble {
  background: transparent !important;
  padding: 0 !important;
  color: inherit;
  border: none !important;
  box-shadow: none !important;
  font-size: 14px;
}

/* ── Structured Results ───────────────────────────────── */
.structured-result {
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
}

.structured-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
  color: var(--color-secondary);
  font-size: 12px;
}

.structured-fact-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px;
}

.structured-fact {
  display: flex;
  justify-content: space-between;
  padding: 4px 8px;
  background: var(--color-surface);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border-subtle);
}

.fact-label {
  color: var(--color-secondary);
}

.structured-sources {
  margin-top: 8px;
  font-size: 11px;
  color: var(--color-muted);
}

.structured-clarification {
  padding: 10px 14px;
  background: var(--color-warning-light);
  border: 1px solid rgba(245, 158, 11, 0.3);
  border-radius: var(--radius-md);
  font-size: 13.5px;
  color: #92400e;
}

/* ── Source Chips ─────────────────────────────────────── */
.sources {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
}

.source-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--color-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-full);
  padding: 3px 10px;
  transition: all var(--duration-fast);
}

.source-chip:hover {
  border-color: var(--color-border);
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

/* ── Action Toolbar ───────────────────────────────────── */
.actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-full);
  padding: 2px 6px;
  align-self: flex-start;
  box-shadow: var(--shadow-xs);
  margin-top: 2px;
}

.action-btn {
  width: 28px;
  height: 28px;
  background: none;
  border: none;
  border-radius: var(--radius-full);
  color: var(--color-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--duration-fast) var(--ease-out);
}

.action-btn:hover:not(:disabled) {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

.action-btn.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.action-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.action-btn--accent {
  width: auto;
  padding: 0 10px;
  gap: 4px;
  border-radius: var(--radius-full);
  color: var(--color-primary);
  font-size: 11.5px;
  font-weight: 600;
}

.action-btn--accent:hover {
  background: var(--color-primary-light);
}

.action-label {
  font-size: 11.5px;
  font-weight: 600;
}
</style>
