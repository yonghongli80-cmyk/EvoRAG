<script setup lang="ts">
import type { EvolutionObservation } from '../../api/evolution'
import { formatTime, poolLabel, shortHash, stageLabel } from './helpers'

defineProps<{
  observations: EvolutionObservation[]
  loading: boolean
  page: number
  totalPages: number
  observationsTotal: number
}>()

defineEmits<{
  (e: 'changePage', page: number): void
}>()
</script>

<template>
  <div class="tab-panel">
    <p v-if="!loading && observations.length === 0" class="empty-state" data-test="observations-empty">
      <strong>暂无观察</strong>
      <span>权限过滤后的可诊断失败会异步落入观测队列，同类根因累计三次后进入候选池。</span>
    </p>
    <div v-else class="table-card">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>知识库</th><th>失败阶段</th><th>根因特征</th><th>状态</th>
            <th>池</th><th>优先级</th><th>最佳分数</th><th>可答性</th><th>路由</th>
            <th>Jev 问题集</th><th>候选块哈希</th><th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in observations" :key="item.id" data-test="observation-row">
            <td>{{ item.id }}</td>
            <td>{{ item.kb_id }}</td>
            <td>{{ stageLabel(item.failure_stage) }}</td>
            <td>{{ item.root_cause_feature || '未判定' }}</td>
            <td>{{ item.status }}</td>
            <td>{{ poolLabel(item.pool) }}</td>
            <td>{{ item.priority }}</td>
            <td>{{ item.best_score ?? '—' }}</td>
            <td>{{ item.answerability_kind || '—' }}</td>
            <td>{{ item.route }}</td>
            <td>{{ item.jev_question_set_version || '—' }}</td>
            <td>
              <span v-for="hash in item.chunk_hashes.slice(0, 2)" :key="hash" class="hash">{{ shortHash(hash) }}</span>
              <span v-if="item.chunk_hashes.length > 2">+{{ item.chunk_hashes.length - 2 }}</span>
              <span v-if="item.chunk_hashes.length === 0">无候选块</span>
            </td>
            <td>{{ formatTime(item.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div class="pager" data-test="observations-pager">
        <button type="button" :disabled="page <= 1" @click="$emit('changePage', page - 1)">上一页</button>
        <span>第 {{ page }} / {{ totalPages }} 页 · 共 {{ observationsTotal }} 条</span>
        <button type="button" :disabled="page >= totalPages" @click="$emit('changePage', page + 1)">下一页</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tab-panel { display: grid; gap: var(--space-5); }

.table-card {
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: var(--shadow-xs);
}

table { width: 100%; border-collapse: collapse; }
th, td { padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--color-border); text-align: left; font-size: var(--text-sm); }
th { color: var(--color-secondary); background: var(--color-surface-2); font-weight: var(--font-semibold); }
tbody tr:hover { background: #f8fbff; }

.pager { display: flex; align-items: center; gap: var(--space-3); padding-top: var(--space-3); color: var(--color-secondary); font-size: var(--text-sm); }
.pager button {
  min-height: 30px;
  padding: 0 var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface);
  font: inherit;
  font-size: var(--text-xs);
  cursor: pointer;
}
.pager button:disabled { color: var(--color-secondary); cursor: not-allowed; opacity: .5; }

.hash { display: inline-block; padding: 2px 6px; margin-right: var(--space-1); border-radius: var(--radius); background: var(--color-surface-2); font-family: ui-monospace, monospace; font-size: var(--text-xs); }

.muted, .empty-state { color: var(--color-secondary); font-size: var(--text-sm); }
.empty-state { min-height: 200px; display: grid; place-content: center; gap: var(--space-2); text-align: center; border: 1px dashed var(--color-border); border-radius: var(--radius-lg); background: var(--color-surface); }
.empty-state strong { color: var(--color-foreground); font-size: var(--text-base); }

@media (max-width: 900px) {
  .table-card { overflow: auto; }
}
</style>
