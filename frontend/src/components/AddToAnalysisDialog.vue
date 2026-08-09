<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElDialog, ElRadio, ElRadioGroup, ElInput, ElButton } from 'element-plus'
import api from '../utils/api'

interface CardInfo {
  id: number
  name: string
}

const props = defineProps<{
  visible: boolean
  question: string
  answer: string
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'confirm', cardId: number | null, newCardName?: string): void
}>()

const cards = ref<CardInfo[]>([])
const selectedCardId = ref<number | undefined>(undefined)
const createNew = ref(false)
const newCardName = ref('')
const suggestLoading = ref(false)
const suggestedCardId = ref<number | null>(null)
const suggestedCardName = ref('')
const confidence = ref(0)
const loading = ref(false)

// Load cards and suggest when dialog opens
watch(() => props.visible, async (val) => {
  if (val) {
    selectedCardId.value = undefined
    createNew.value = false
    newCardName.value = ''
    suggestedCardId.value = null
    confidence.value = 0
    await loadCards()
    suggestCard()
  }
})

async function loadCards() {
  try {
    const res = await api.get('/analysis/cards')
    cards.value = res.data || []
  } catch {
    cards.value = []
  }
}

async function suggestCard() {
  if (!props.question && !props.answer) return
  suggestLoading.value = true
  try {
    const res = await api.post('/analysis/suggest-card', {
      question: props.question,
      answer: props.answer,
    })
    const data = res.data
    suggestedCardId.value = data.suggested_card_id
    suggestedCardName.value = data.suggested_card_name || ''
    confidence.value = data.confidence || 0
    // Auto-select suggested card if confidence is high enough
    if (suggestedCardId.value && confidence.value >= 0.6) {
      selectedCardId.value = suggestedCardId.value as number
    }
  } catch {
    // LLM suggestion is best-effort, ignore errors
  } finally {
    suggestLoading.value = false
  }
}

function handleConfirm() {
  if (createNew.value) {
    const name = newCardName.value.trim()
    if (!name) return
    emit('confirm', null, name)
  } else if (selectedCardId.value !== undefined) {
    emit('confirm', selectedCardId.value)
  }
}

function handleClose() {
  emit('update:visible', false)
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    @update:model-value="handleClose"
    title="添加到分析卡片"
    width="480px"
    :close-on-click-modal="false"
  >
    <!-- LLM Suggestion -->
    <div v-if="suggestLoading" class="suggest-loading">
      <span class="loading-dot"></span>
      <span>正在智能推荐卡片...</span>
    </div>
    <div v-else-if="suggestedCardId && confidence >= 0.6" class="suggest-result">
      <span class="suggest-label">AI 推荐</span>
      <span class="suggest-card">{{ suggestedCardName }}</span>
      <span class="suggest-confidence">置信度 {{ Math.round(confidence * 100) }}%</span>
    </div>

    <!-- Existing cards -->
    <div v-if="cards.length" class="card-list">
      <div class="card-list-label">选择已有卡片</div>
      <el-radio-group v-model="selectedCardId" :disabled="createNew" class="card-radio-group">
        <el-radio
          v-for="card in cards"
          :key="card.id"
          :value="card.id"
          class="card-radio"
        >
          {{ card.name }}
          <span v-if="card.id === suggestedCardId && confidence >= 0.6" class="suggest-tag">推荐</span>
        </el-radio>
      </el-radio-group>
    </div>

    <!-- Create new card -->
    <div class="new-card-section">
      <el-radio v-model="createNew" :value="true" @change="selectedCardId = undefined">
        新建卡片组
      </el-radio>
      <el-input
        v-if="createNew"
        v-model="newCardName"
        placeholder="输入卡片组名称"
        size="small"
        style="margin-top: 8px"
        @keyup.enter="handleConfirm"
      />
    </div>

    <template #footer>
      <el-button @click="handleClose">取消</el-button>
      <el-button
        type="primary"
        :disabled="!createNew && selectedCardId === undefined"
        :loading="loading"
        @click="handleConfirm"
      >
        确认
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.suggest-loading {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
  margin-bottom: var(--space-4);
  color: var(--color-secondary);
  font-size: var(--text-sm);
  background: var(--color-muted);
  border-radius: var(--radius);
}

.loading-dot {
  width: 8px;
  height: 8px;
  background: var(--color-accent);
  border-radius: 50%;
  animation: pulse 1s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

.suggest-result {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
  margin-bottom: var(--space-4);
  background: var(--color-accent-light);
  border: 1px solid var(--color-accent-subtle);
  border-radius: var(--radius);
  font-size: var(--text-sm);
}

.suggest-label {
  font-weight: var(--font-semibold);
  color: var(--color-accent);
}

.suggest-card {
  font-weight: var(--font-medium);
  color: var(--color-foreground);
}

.suggest-confidence {
  color: var(--color-secondary);
  font-size: var(--text-xs);
  margin-left: auto;
}

.card-list {
  margin-bottom: var(--space-4);
}

.card-list-label {
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--color-secondary);
  margin-bottom: var(--space-2);
}

.card-radio-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.card-radio {
  margin: 0;
  padding: var(--space-2);
  border-radius: var(--radius);
  transition: background var(--duration-fast);
}

.card-radio:hover {
  background: var(--color-muted);
}

.suggest-tag {
  font-size: 10px;
  padding: 1px 6px;
  background: var(--color-accent);
  color: white;
  border-radius: var(--radius-full);
  margin-left: var(--space-2);
  font-weight: var(--font-medium);
}

.new-card-section {
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}
</style>
