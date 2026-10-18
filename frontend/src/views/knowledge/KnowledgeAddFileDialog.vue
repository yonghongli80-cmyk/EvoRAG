<script setup lang="ts">
import { ref, watch } from 'vue'
import { Check } from '@element-plus/icons-vue'

const props = defineProps<{
  availableFiles: any[]
  loadingFiles?: boolean
  addingFile?: boolean
}>()

const visible = defineModel<boolean>('visible', { default: false })

const emit = defineEmits<{
  (e: 'add', file: any): void
  (e: 'open'): void
}>()

const selectedFile = ref<any>(null)

watch(visible, (val) => {
  if (val) {
    selectedFile.value = null
    emit('open')
  }
})
</script>

<template>
  <el-dialog
    v-model="visible"
    title="添加文件到知识库"
    width="520px"
    :close-on-click-modal="false"
  >
    <div v-if="props.loadingFiles" class="kb-add-loading">
      <p>加载中...</p>
    </div>
    <div v-else-if="props.availableFiles.length === 0" class="kb-add-empty">
      <p>暂无可添加的文件</p>
      <p class="kb-add-hint">请先在「文件管理」页面上传文件</p>
    </div>
    <div v-else class="kb-add-list">
      <div
        v-for="file in props.availableFiles"
        :key="file.name"
        class="kb-add-item"
        :class="{ 'kb-add-item--selected': selectedFile?.name === file.name }"
        @click="selectedFile = file"
      >
        <div class="kb-add-item-info">
          <div class="kb-add-item-name">{{ file.name }}</div>
          <div class="kb-add-item-meta">{{ file.size_human }}</div>
        </div>
        <div v-if="selectedFile?.name === file.name" class="kb-add-item-check">
          <el-icon><Check /></el-icon>
        </div>
      </div>
    </div>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="props.addingFile"
        :disabled="!selectedFile"
        @click="emit('add', selectedFile)"
      >
        添加
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.kb-add-loading,
.kb-add-empty {
  text-align: center;
  padding: var(--space-8) var(--space-4);
  color: var(--color-secondary);
  font-size: var(--text-sm);
}

.kb-add-hint {
  font-size: var(--text-xs);
  color: var(--color-muted);
  margin-top: var(--space-2);
}

.kb-add-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 360px;
  overflow-y: auto;
}

.kb-add-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.kb-add-item:hover {
  border-color: var(--color-accent);
  background: var(--color-accent-light);
}

.kb-add-item--selected {
  border-color: var(--color-accent);
  background: var(--color-accent-light);
}

.kb-add-item-info {
  flex: 1;
  min-width: 0;
}

.kb-add-item-name {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kb-add-item-meta {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  margin-top: 2px;
}

.kb-add-item-check {
  color: var(--color-accent);
  font-size: 18px;
  flex-shrink: 0;
  margin-left: var(--space-3);
}
</style>
