<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import type { KBItem } from './helpers'

const props = defineProps<{
  knowledgeBases: KBItem[]
  loadingKbs?: boolean
  canEdit?: boolean
}>()

const selectedKbId = defineModel<string>('selectedKbId', { default: '' })

const emit = defineEmits<{
  (e: 'open-create'): void
}>()
</script>

<template>
  <header class="wiki-header">
    <div class="header-left">
      <h1 class="page-title">超文本 Wiki (Hypertext Wiki)</h1>
      <p class="page-subtitle">语义 Markdown 编辑、[[双向链接]] 关联图谱与增量修订秒级回滚</p>
    </div>

    <div class="header-right">
      <!-- KB Selector -->
      <div class="kb-selector-wrap">
        <span class="label">当前知识库:</span>
        <el-select
          v-model="selectedKbId"
          placeholder="选择知识库"
          size="default"
          class="kb-select"
          :loading="props.loadingKbs"
        >
          <el-option
            v-for="kb in props.knowledgeBases"
            :key="kb.kb_id"
            :label="kb.name"
            :value="kb.kb_id"
          />
        </el-select>
      </div>

      <el-button
        type="primary"
        :icon="Plus"
        data-test="create-wiki-btn"
        :disabled="!props.canEdit || !selectedKbId"
        @click="emit('open-create')"
      >
        新建 Wiki 词条
      </el-button>
    </div>
  </header>
</template>

<style scoped>
.wiki-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
}

.header-left {
  display: flex;
  flex-direction: column;
}

.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.page-subtitle {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--color-secondary);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.kb-selector-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
}

.kb-selector-wrap .label {
  font-size: 13px;
  color: var(--color-secondary);
}

.kb-select {
  width: 180px;
}
</style>
