<script setup lang="ts">
import type { WikiRevisionDiff } from '../../api/wiki'
import { formatTime } from './helpers'

const props = defineProps<{
  diff: WikiRevisionDiff | null
  loading?: boolean
}>()

const visible = defineModel<boolean>('visible', { default: false })
</script>

<template>
  <el-dialog
    v-model="visible"
    title="版本变更 Diff 详情"
    width="700px"
  >
    <div v-loading="props.loading" class="diff-dialog-body">
      <template v-if="props.diff">
        <div class="diff-meta">
          <span>版本号: #{{ props.diff.revision_num }}</span>
          <span>提交备注: {{ props.diff.comment }}</span>
          <span>生成时间: {{ formatTime(props.diff.created_at) }}</span>
        </div>
        <div class="diff-content-box">
          <pre class="diff-pre">{{ props.diff.delta || '此版本为初始版本或无增量差异' }}</pre>
        </div>
      </template>
    </div>
    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.diff-meta {
  display: flex;
  gap: 16px;
  font-size: 13px;
  margin-bottom: 12px;
  color: var(--color-secondary);
}

.diff-content-box {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px;
  border-radius: 6px;
  max-height: 400px;
  overflow: auto;
}

.diff-pre {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.5;
}
</style>
