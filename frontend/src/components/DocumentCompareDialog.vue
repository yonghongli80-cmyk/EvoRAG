<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { compareDocuments, type ComparisonResult } from '../api/reliability'

export interface ComparableDocument {
  id: number
  name: string
}

const props = defineProps<{
  visible: boolean
  documents: ComparableDocument[]
}>()

const emit = defineEmits<{
  'update:visible': [visible: boolean]
}>()

const question = ref('')
const selectedIds = ref<number[]>([])
const result = ref<ComparisonResult | null>(null)
const loading = ref(false)
const error = ref('')

const selectedCount = computed(() => selectedIds.value.length)

watch(() => props.visible, (visible) => {
  if (!visible) return
  result.value = null
  error.value = ''
})

function isSelected(documentId: number): boolean {
  return selectedIds.value.includes(documentId)
}

function canSelect(documentId: number): boolean {
  return isSelected(documentId) || selectedCount.value < 5
}

function toggleDocument(documentId: number, checked: boolean): void {
  if (checked) {
    if (canSelect(documentId)) selectedIds.value = [...selectedIds.value, documentId]
    return
  }
  selectedIds.value = selectedIds.value.filter((id) => id !== documentId)
}

async function submit(): Promise<void> {
  if (!question.value.trim() || selectedIds.value.length === 0 || loading.value) return
  loading.value = true
  error.value = ''
  try {
    result.value = await compareDocuments({
      question: question.value.trim(),
      document_ids: selectedIds.value,
      dimensions: []
    })
  } catch {
    error.value = '比较请求失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

function close(): void {
  emit('update:visible', false)
}
</script>

<template>
  <section v-if="visible" class="document-compare-dialog" role="dialog" aria-label="资料比较">
    <header>
      <h2>资料比较</h2>
      <button type="button" aria-label="关闭比较" @click="close">×</button>
    </header>
    <form @submit.prevent="submit">
      <label>
        比较问题
        <input v-model="question" name="question" required>
      </label>
      <fieldset>
        <legend>选择资料（最多 5 份）</legend>
        <label v-for="document in documents" :key="document.id">
          <input
            type="checkbox"
            :checked="isSelected(document.id)"
            :disabled="!canSelect(document.id)"
            @change="toggleDocument(document.id, ($event.target as HTMLInputElement).checked)"
          >
          {{ document.name }}
        </label>
      </fieldset>
      <button type="submit" :disabled="loading || !question.trim() || selectedCount === 0">
        {{ loading ? '比较中…' : '开始比较' }}
      </button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <div v-if="result" class="comparison-result">
      <p v-if="result.partial">部分资料未能提供充分证据</p>
      <section v-for="row in result.rows" :key="row.dimension">
        <h3>{{ row.dimension }}</h3>
        <dl>
          <template v-for="(cell, documentId) in row.values" :key="documentId">
            <dt>{{ documents.find((document) => document.id === Number(documentId))?.name || documentId }}</dt>
            <dd>{{ cell.status === 'insufficient_evidence' ? '资料不足' : cell.text }}</dd>
          </template>
        </dl>
      </section>
    </div>
  </section>
</template>
