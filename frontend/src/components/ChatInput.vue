<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useChatStore, type ChatMode } from '../stores/chat'

const props = defineProps<{
  mode?: ChatMode
  kbId?: string
}>()

const chatStore = useChatStore()
const inputText = ref('')

onMounted(() => {
  chatStore.loadAvailableModels()
})

async function handleSend() {
  const q = inputText.value.trim()
  if (!q || chatStore.isStreaming) return
  inputText.value = ''
  await chatStore.sendMessage(q, props.mode, props.kbId)
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function getModelIcon(modelId: string): string {
  const id = modelId.toLowerCase()
  if (id.includes('deepseek')) return '⚡'
  if (id.includes('qwen')) return '🧠'
  if (id.includes('gpt') || id.includes('openai')) return '✨'
  if (id.includes('claude')) return '🎯'
  return '🤖'
}
</script>

<template>
  <div class="input-area">
    <!-- Model & Scope Floating Capsule -->
    <div class="capsule-toolbar">
      <!-- Active file filter badge -->
      <div v-if="chatStore.selectedFile" class="scope-pill">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
        <span class="scope-name" :title="chatStore.selectedFile">{{ chatStore.selectedFile }}</span>
        <button class="scope-clear" @click="chatStore.selectFile(null)" title="清除筛选">✕</button>
      </div>

      <!-- Model Selector & Arena Switch -->
      <div v-if="chatStore.availableModels.length > 1" class="model-dock">
        <!-- Single Model Selector -->
        <div v-if="!chatStore.arenaEnabled" class="model-select-wrapper">
          <span class="model-icon-badge">{{ getModelIcon(chatStore.selectedModel || '') }}</span>
          <el-select
            v-model="chatStore.selectedModel"
            size="small"
            class="model-select-element"
            placeholder="选择推理模型"
          >
            <el-option
              v-for="m in chatStore.availableModels"
              :key="m.id"
              :label="m.name"
              :value="m.id"
            >
              <div class="option-item">
                <span class="option-icon">{{ getModelIcon(m.id) }}</span>
                <span class="option-name">{{ m.name }}</span>
                <span v-if="(m as any).weight" class="option-weight">权重 {{ (m as any).weight }}</span>
              </div>
            </el-option>
          </el-select>
        </div>

        <!-- Arena Multi-Model Selector -->
        <div v-else class="arena-select-wrapper">
          <span class="arena-icon-badge">⚔️</span>
          <el-select
            v-model="chatStore.arenaModels"
            multiple
            size="small"
            class="model-select-element arena-multi"
            placeholder="选择对齐模型"
          >
            <el-option
              v-for="m in chatStore.availableModels"
              :key="m.id"
              :label="m.name"
              :value="m.id"
            />
          </el-select>
        </div>

        <!-- Arena Toggle Pill -->
        <button
          :class="['arena-toggle-pill', { active: chatStore.arenaEnabled }]"
          @click="chatStore.arenaEnabled = !chatStore.arenaEnabled"
          title="开启多模型同屏竞技对比"
        >
          <span class="arena-toggle-icon">⚔️</span>
          <span class="arena-toggle-text">Arena 竞技</span>
          <span class="arena-toggle-dot" />
        </button>
      </div>
    </div>

    <!-- Main Floating Input Box -->
    <div :class="['input-box', { 'is-streaming': chatStore.isStreaming }]">
      <el-input
        v-model="inputText"
        type="textarea"
        :rows="1"
        :autosize="{ minRows: 1, maxRows: 6 }"
        placeholder="输入您的问题，按 Enter 键极速发送..."
        @keydown="handleKeydown"
        :disabled="chatStore.isStreaming"
      />

      <button
        class="send-btn"
        :disabled="!inputText.trim() || chatStore.isStreaming"
        @click="handleSend"
        aria-label="发送消息"
      >
        <svg v-if="!chatStore.isStreaming" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="19" x2="12" y2="5"/>
          <polyline points="5 12 12 5 19 12"/>
        </svg>
        <div v-else class="streaming-spinner" />
      </button>
    </div>

    <div class="input-hint">
      <span><strong>Enter</strong> 发送</span>
      <span class="hint-dot">·</span>
      <span><strong>Shift + Enter</strong> 换行</span>
    </div>
  </div>
</template>

<style scoped>
.input-area {
  padding: 12px 20px 18px;
  background: linear-gradient(to top, var(--color-background) 75%, transparent);
  position: relative;
  z-index: 10;
}

/* ── Capsule Toolbar ─────────────────────────────────── */
.capsule-toolbar {
  max-width: var(--content-max-width);
  margin: 0 auto 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.scope-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid rgba(99, 102, 241, 0.2);
  border-radius: var(--radius-full);
  font-size: 11.5px;
  font-weight: 500;
  box-shadow: var(--shadow-xs);
}

.scope-name {
  max-width: 180px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scope-clear {
  border: none;
  background: rgba(99, 102, 241, 0.15);
  color: var(--color-primary);
  width: 16px;
  height: 16px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  font-size: 10px;
  padding: 0;
  transition: all var(--duration-fast);
}

.scope-clear:hover {
  background: var(--color-primary);
  color: #ffffff;
}

.model-dock {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  background: var(--color-surface);
  padding: 3px 6px;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
  box-shadow: var(--shadow-xs);
}

.model-select-wrapper,
.arena-select-wrapper {
  display: flex;
  align-items: center;
  gap: 4px;
}

.model-icon-badge,
.arena-icon-badge {
  font-size: 13px;
  margin-left: 4px;
}

.model-select-element {
  width: 135px;
}

.model-select-element.arena-multi {
  min-width: 180px;
}

:deep(.model-select-element .el-input__wrapper) {
  background: transparent !important;
  box-shadow: none !important;
  border: none !important;
  padding: 0 4px !important;
}

:deep(.model-select-element .el-input__inner) {
  font-size: 12px !important;
  font-weight: 600 !important;
  color: var(--color-foreground) !important;
}

.option-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.option-icon {
  font-size: 14px;
}

.option-name {
  font-weight: 500;
}

.option-weight {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-muted);
}

.arena-toggle-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  background: var(--color-surface-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  font-size: 11.5px;
  font-weight: 600;
  color: var(--color-secondary);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.arena-toggle-pill:hover {
  background: var(--color-surface-3);
  color: var(--color-foreground);
}

.arena-toggle-pill.active {
  background: linear-gradient(135deg, #eef2ff 0%, #e0e7ff 100%);
  color: var(--color-primary);
  border-color: rgba(99, 102, 241, 0.4);
  box-shadow: 0 1px 4px rgba(79, 70, 229, 0.15);
}

.arena-toggle-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-muted);
  transition: background var(--duration-fast);
}

.arena-toggle-pill.active .arena-toggle-dot {
  background: var(--color-primary);
  box-shadow: 0 0 6px rgba(79, 70, 229, 0.6);
}

/* ── Main Input Box (Floating Studio Dock) ─────────────── */
.input-box {
  max-width: var(--content-max-width);
  margin: 0 auto;
  display: flex;
  gap: 10px;
  align-items: flex-end;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: 8px 10px 8px 16px;
  box-shadow: var(--shadow-card);
  transition: all var(--duration-normal) var(--ease-out);
}

.input-box:focus-within {
  border-color: var(--color-primary);
  box-shadow: 0 8px 24px -4px rgba(79, 70, 229, 0.16), 0 0 0 2px var(--color-primary-light);
  transform: translateY(-1px);
}

.input-box.is-streaming {
  animation: pulseGlow 2.5s infinite ease-in-out;
}

:deep(.el-textarea__inner) {
  border: none !important;
  box-shadow: none !important;
  padding: 6px 0 !important;
  font-family: var(--font-body) !important;
  font-size: 14.5px !important;
  line-height: 1.5 !important;
  resize: none !important;
  background: transparent !important;
  color: var(--color-foreground) !important;
}

.send-btn {
  width: 36px;
  height: 36px;
  background: linear-gradient(135deg, #4f46e5 0%, #6366f1 100%);
  border: none;
  border-radius: var(--radius-md);
  color: #ffffff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: all var(--duration-fast) var(--ease-out);
  box-shadow: 0 2px 6px rgba(79, 70, 229, 0.3);
}

.send-btn:hover:not(:disabled) {
  background: linear-gradient(135deg, #4338ca 0%, #4f46e5 100%);
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
}

.send-btn:active:not(:disabled) {
  transform: translateY(0);
}

.send-btn:disabled {
  background: var(--color-surface-3);
  color: var(--color-muted);
  box-shadow: none;
  cursor: not-allowed;
}

.streaming-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: #ffffff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ── Bottom Hint ──────────────────────────────────────── */
.input-hint {
  text-align: center;
  font-size: 11px;
  color: var(--color-muted);
  margin-top: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.input-hint strong {
  font-weight: 600;
  color: var(--color-secondary);
}

.hint-dot {
  opacity: 0.5;
}
</style>
