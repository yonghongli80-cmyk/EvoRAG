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
  <div v-if="visible" class="compare-backdrop" @click.self="close">
    <section class="document-compare-dialog" role="dialog" aria-modal="true" aria-label="资料比较">
      <header class="compare-header">
        <div>
          <p class="eyebrow">智能资料分析</p>
          <h2>资料比较</h2>
          <p class="header-note">选择资料并提出比较问题，系统会按相同维度整理差异。</p>
        </div>
        <button class="close-button" type="button" aria-label="关闭比较" @click="close">×</button>
      </header>
      <div class="compare-body">
        <form class="compare-form" @submit.prevent="submit">
          <label class="question-field">
            <span>比较问题</span>
            <input v-model="question" name="question" required placeholder="例如：比较两份制度中的年假规则">
          </label>
          <fieldset>
            <legend>
              <span>选择资料</span>
              <small>已选择 {{ selectedCount }}/5</small>
            </legend>
            <div class="document-options">
              <label
                v-for="document in documents"
                :key="document.id"
                :class="['document-option', { selected: isSelected(document.id) }]"
              >
                <input
                  type="checkbox"
                  :checked="isSelected(document.id)"
                  :disabled="!canSelect(document.id)"
                  @change="toggleDocument(document.id, ($event.target as HTMLInputElement).checked)"
                >
                <span class="document-mark">DOC</span>
                <span class="document-name">{{ document.name }}</span>
              </label>
            </div>
          </fieldset>
          <div class="form-footer">
            <span>最多选择 5 份资料</span>
            <button class="compare-button" type="submit" :disabled="loading || !question.trim() || selectedCount === 0">
              {{ loading ? '比较中…' : '开始比较' }}
            </button>
          </div>
        </form>
        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
        <div v-if="result" class="comparison-result">
          <div class="result-heading">
            <div>
              <p class="eyebrow">COMPARISON RESULT</p>
              <h3>比较结果</h3>
            </div>
            <span v-if="result.partial" class="partial-badge">部分资料证据不足</span>
          </div>
          <section v-for="row in result.rows" :key="row.dimension" class="dimension-card">
            <h4>{{ row.dimension }}</h4>
            <dl>
              <template v-for="(cell, documentId) in row.values" :key="documentId">
                <div class="comparison-cell" :class="{ insufficient: cell.status === 'insufficient_evidence' }">
                  <dt>{{ documents.find((document) => document.id === Number(documentId))?.name || documentId }}</dt>
                  <dd>{{ cell.status === 'insufficient_evidence' ? '资料不足' : cell.text }}</dd>
                </div>
              </template>
            </dl>
          </section>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.compare-backdrop { position: fixed; z-index: 1000; inset: 0; display: grid; place-items: center; padding: var(--space-6); background: rgba(15, 23, 42, .38); backdrop-filter: blur(2px); }
.document-compare-dialog { width: min(920px, 100%); max-height: min(820px, 92vh); display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--color-border); border-radius: var(--radius-xl); background: var(--color-surface); box-shadow: var(--shadow-lg); animation: scaleIn var(--duration-normal) var(--ease-out); }
.compare-header { display: flex; justify-content: space-between; gap: var(--space-6); padding: var(--space-6); border-bottom: 1px solid var(--color-border); background: linear-gradient(135deg, var(--color-surface), var(--color-accent-subtle)); }
.eyebrow { margin: 0 0 var(--space-1); color: var(--color-primary); font-size: 10px; font-weight: var(--font-bold); letter-spacing: .1em; }
.compare-header h2 { margin: 0; color: var(--color-foreground); font-family: var(--font-heading); font-size: var(--text-2xl); }
.header-note { margin: var(--space-1) 0 0; color: var(--color-secondary); font-size: var(--text-sm); }
.close-button { width: 36px; height: 36px; border: 1px solid var(--color-border); border-radius: var(--radius); color: var(--color-secondary); background: var(--color-surface); font-size: 22px; cursor: pointer; }
.close-button:hover { color: var(--color-foreground); background: var(--color-surface-2); }
.compare-body { padding: var(--space-6); overflow: auto; }
.compare-form { display: grid; gap: var(--space-5); }
.question-field { display: grid; gap: var(--space-2); color: var(--color-foreground); font-size: var(--text-sm); font-weight: var(--font-semibold); }
.question-field input { min-height: 44px; padding: 0 var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius); color: var(--color-foreground); background: var(--color-surface); font: inherit; font-weight: var(--font-regular); }
.question-field input:focus { border-color: var(--color-primary); box-shadow: 0 0 0 3px var(--color-accent-light); outline: none; }
fieldset { min-width: 0; padding: 0; border: 0; }
legend { width: 100%; display: flex; justify-content: space-between; margin-bottom: var(--space-3); color: var(--color-foreground); font-size: var(--text-sm); font-weight: var(--font-semibold); }
legend small { color: var(--color-secondary); font-weight: var(--font-regular); }
.document-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
.document-option { min-width: 0; display: flex; align-items: center; gap: var(--space-3); padding: var(--space-3) var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius); color: var(--color-foreground); background: var(--color-surface); cursor: pointer; transition: all var(--duration-fast) var(--ease-out); }
.document-option:hover { border-color: var(--color-border-hover); background: var(--color-surface-2); }
.document-option.selected { border-color: var(--color-primary); background: var(--color-accent-light); box-shadow: 0 0 0 1px var(--color-primary); }
.document-option input { accent-color: var(--color-primary); }
.document-mark { flex-shrink: 0; padding: 3px 5px; border-radius: var(--radius-xs); color: var(--color-primary); background: var(--color-accent-light); font-family: var(--font-mono); font-size: 9px; font-weight: var(--font-bold); }
.document-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--text-sm); }
.form-footer { display: flex; justify-content: space-between; align-items: center; gap: var(--space-4); padding-top: var(--space-4); border-top: 1px solid var(--color-border); }
.form-footer > span { color: var(--color-secondary); font-size: var(--text-xs); }
.compare-button { min-height: 40px; padding: 0 var(--space-6); border: 1px solid var(--color-primary); border-radius: var(--radius); color: var(--color-on-primary); background: var(--color-primary); font: inherit; font-size: var(--text-sm); font-weight: var(--font-semibold); cursor: pointer; }
.compare-button:hover { background: var(--color-primary-hover); }
.compare-button:disabled { opacity: .5; cursor: not-allowed; }
.error-banner { margin: var(--space-4) 0 0; padding: var(--space-3) var(--space-4); border: 1px solid #fecaca; border-radius: var(--radius); color: #b91c1c; background: var(--color-destructive-light); font-size: var(--text-sm); }
.comparison-result { margin-top: var(--space-6); padding-top: var(--space-6); border-top: 1px solid var(--color-border); }
.result-heading { display: flex; justify-content: space-between; align-items: center; gap: var(--space-4); margin-bottom: var(--space-4); }
.result-heading h3 { margin: 0; color: var(--color-foreground); font-size: var(--text-xl); }
.partial-badge { padding: 4px 10px; border-radius: var(--radius-full); color: #92400e; background: var(--color-warning-light); font-size: var(--text-xs); font-weight: var(--font-medium); }
.dimension-card { margin-bottom: var(--space-4); padding: var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: var(--color-background); }
.dimension-card h4 { margin: 0 0 var(--space-3); color: var(--color-foreground); font-size: var(--text-base); }
.dimension-card dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); margin: 0; }
.comparison-cell { padding: var(--space-3); border: 1px solid var(--color-border); border-radius: var(--radius); background: var(--color-surface); }
.comparison-cell.insufficient { border-style: dashed; background: var(--color-warning-light); }
.comparison-cell dt { margin-bottom: var(--space-1); color: var(--color-secondary); font-size: var(--text-xs); font-weight: var(--font-semibold); }
.comparison-cell dd { margin: 0; color: var(--color-foreground); font-size: var(--text-sm); line-height: var(--leading-relaxed); }

@media (max-width: 720px) {
  .compare-backdrop { padding: var(--space-2); }
  .compare-header, .compare-body { padding: var(--space-4); }
  .document-options, .dimension-card dl { grid-template-columns: 1fr; }
}
</style>
