<script setup lang="ts">
import type { WikiRevision } from '../../api/wiki'
import { formatTime } from './helpers'

const props = defineProps<{
  historyList: WikiRevision[]
  loading?: boolean
  rollingBack?: boolean
  canEdit?: boolean
}>()

const visible = defineModel<boolean>('visible', { default: false })

const emit = defineEmits<{
  (e: 'view-diff', revisionNum: number): void
  (e: 'rollback', revisionNum: number): void
}>()
</script>

<template>
  <el-drawer
    v-model="visible"
    title="版本修订历史 (Revision History)"
    size="480px"
  >
    <div v-loading="props.loading" class="history-container">
      <div v-if="props.historyList.length === 0" class="empty-history">
        暂无历史修订记录
      </div>
      <div v-else class="history-timeline">
        <div
          v-for="rev in props.historyList"
          :key="rev.id"
          class="history-card"
        >
          <div class="rev-header">
            <span class="rev-badge">#{{ rev.revision_num }}</span>
            <span class="rev-author">{{ rev.author_id || 'system' }}</span>
            <span class="rev-time">{{ formatTime(rev.created_at) }}</span>
          </div>
          <div class="rev-comment">{{ rev.comment || '无提交备注' }}</div>
          <div class="rev-actions">
            <el-button
              size="small"
              link
              type="primary"
              @click="emit('view-diff', rev.revision_num)"
            >
              查看增量 Diff
            </el-button>
            <el-button
              v-if="props.canEdit"
              size="small"
              type="warning"
              plain
              :loading="props.rollingBack"
              @click="emit('rollback', rev.revision_num)"
            >
              回滚至此版本
            </el-button>
          </div>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.history-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.empty-history {
  text-align: center;
  color: var(--color-muted);
  padding: 32px 0;
  font-size: 13px;
}

.history-card {
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface-2);
  margin-bottom: 10px;
}

.rev-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rev-badge {
  background: var(--color-primary);
  color: #fff;
  font-size: 12px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
}

.rev-author {
  font-size: 13px;
  font-weight: 500;
}

.rev-time {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-secondary);
}

.rev-comment {
  font-size: 13px;
  margin: 8px 0;
  color: var(--color-foreground);
}

.rev-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
