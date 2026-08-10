<script setup lang="ts">
import { ref } from 'vue'
import { resolveConflict, type ConflictResolution } from '../api/knowledgeOperations'

const props = defineProps<{
  conflictId: number
  status: string
}>()

const emit = defineEmits<{
  resolved: [conflict: ConflictResolution]
}>()

const currentStatus = ref(props.status)
const resolving = ref(false)
const error = ref('')

async function resolve(): Promise<void> {
  if (resolving.value || currentStatus.value !== 'open') return
  resolving.value = true
  error.value = ''
  try {
    const conflict = await resolveConflict(props.conflictId, 'resolved', '')
    currentStatus.value = conflict.status
    emit('resolved', conflict)
  } catch {
    error.value = '处理冲突失败，请稍后重试'
  } finally {
    resolving.value = false
  }
}
</script>

<template>
  <section class="conflict-card">
    <p>证据冲突：{{ currentStatus === 'resolved' ? '已解决' : '待处理' }}</p>
    <button v-if="currentStatus === 'open'" type="button" :disabled="resolving" @click="resolve">
      {{ resolving ? '处理中…' : '标记为已解决' }}
    </button>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>
