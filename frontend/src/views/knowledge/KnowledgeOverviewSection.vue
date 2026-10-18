<script setup lang="ts">
import { ref, watch } from 'vue'
import { Edit } from '@element-plus/icons-vue'

const props = defineProps<{
  overview: string
  loading?: boolean
  canManage?: boolean
  saving?: boolean
}>()

const emit = defineEmits<{
  (e: 'save', newOverview: string): void
}>()

const editing = ref(false)
const draft = ref('')

watch(() => props.overview, (newVal) => {
  if (!editing.value) {
    draft.value = newVal || ''
  }
})

function startEdit() {
  draft.value = props.overview || ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  draft.value = ''
}

function handleSave() {
  emit('save', draft.value)
}

function finishEdit() {
  editing.value = false
}

defineExpose({
  finishEdit
})
</script>

<template>
  <div class="kb-section">
    <div class="kb-section-header">
      <h3 class="kb-section-title">概述</h3>
      <el-button v-if="props.canManage && !editing" text size="small" @click="startEdit">
        <el-icon style="margin-right: 4px;"><Edit /></el-icon>
        编辑
      </el-button>
    </div>
    <div v-if="props.loading" class="skeleton-line" style="width: 100%; height: 40px;"></div>
    <div v-else-if="editing" class="kb-overview-edit">
      <el-input v-model="draft" type="textarea" :rows="3" placeholder="输入概述..." />
      <div class="kb-overview-edit-actions">
        <el-button size="small" @click="cancelEdit">取消</el-button>
        <el-button type="primary" size="small" :loading="props.saving" @click="handleSave">保存</el-button>
      </div>
    </div>
    <div v-else>
      <p v-if="props.overview" class="kb-overview-text">{{ props.overview }}</p>
      <p v-else class="kb-overview-empty">暂无概述</p>
    </div>
  </div>
</template>

<style scoped>
.kb-section {
  margin-bottom: var(--space-6);
}

.kb-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-3);
}

.kb-section-title {
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
  text-transform: uppercase;
  letter-spacing: var(--tracking-wider);
  color: var(--color-secondary);
  margin: 0;
}

.kb-overview-text {
  font-size: var(--text-sm);
  color: var(--color-foreground);
  line-height: var(--leading-relaxed);
}

.kb-overview-empty {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  font-style: italic;
}

.kb-overview-edit {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.kb-overview-edit-actions {
  display: flex;
  gap: var(--space-2);
  justify-content: flex-end;
}

.skeleton-line {
  border-radius: var(--radius-sm);
  background: var(--color-muted);
  animation: shimmer 1.5s infinite;
  background-size: 200% 100%;
  background-image: linear-gradient(90deg, var(--color-muted) 25%, var(--color-border) 50%, var(--color-muted) 75%);
}

@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}
</style>
