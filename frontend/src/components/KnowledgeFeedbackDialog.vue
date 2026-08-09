<template>
  <el-dialog
    :model-value="visible"
    :title="feedbackTitle"
    width="480px"
    :close-on-click-modal="false"
    @update:model-value="$emit('update:visible', $event)"
  >
    <el-form label-position="top">
      <el-form-item
        v-if="kind === 'outdated' && citations.length > 1"
        label="选择内容过期的来源"
        :error="citationError"
      >
        <select
          v-model="selectedCitationId"
          class="citation-select"
          data-test="citation-select"
          style="width: 100%"
        >
          <option :value="undefined" disabled>请选择</option>
          <option
            v-for="c in citations"
            :key="c.citation_id"
            :value="c.citation_id"
          >
            {{ c.doc_name }}{{ c.section_title ? ' · ' + c.section_title : '' }}
          </option>
        </select>
        <div v-if="citationError" class="el-form-item__error" data-test="citation-error">
          请选择内容过期的来源
        </div>
      </el-form-item>
      <el-form-item label="补充说明">
        <el-input
          v-model="comment"
          type="textarea"
          :rows="2"
          placeholder="可选的补充说明"
        />
      </el-form-item>
      <el-form-item v-if="kind === 'outdated'" label="建议的更新内容">
        <el-input
          v-model="suggestedAnswer"
          type="textarea"
          :rows="2"
          placeholder="可选的建议答案"
        />
      </el-form-item>
    </el-form>
    <div class="feedback-actions">
      <el-button @click="$emit('update:visible', false)">取消</el-button>
      <el-button type="primary" data-test="submit-feedback" @click="submit">
        提交反馈
      </el-button>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Citation } from '../api/documents'
import type { KnowledgeFeedbackRequest } from '../api/knowledgeOperations'

const props = defineProps<{
  visible: boolean
  assistantMessageId: number
  kind: 'useful' | 'not_useful' | 'outdated'
  citations: Citation[]
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'submit', payload: KnowledgeFeedbackRequest): void
}>()

const selectedCitationId = ref<number | undefined>(undefined)
const comment = ref('')
const suggestedAnswer = ref('')

const feedbackTitle = computed(() => {
  if (props.kind === 'outdated') return '反馈内容过期'
  if (props.kind === 'not_useful') return '反馈内容无帮助'
  return '反馈内容有用'
})

const citationError = computed(() => {
  if (props.kind === 'outdated' && props.citations.length > 1 && !selectedCitationId.value) {
    return true
  }
  return false
})

function submit() {
  if (citationError.value) return
  emit('submit', {
    assistant_message_id: props.assistantMessageId,
    kind: props.kind,
    citation_id: selectedCitationId.value,
    comment: comment.value,
    suggested_answer: suggestedAnswer.value
  })
}
</script>
