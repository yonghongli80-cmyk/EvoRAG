<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchGapTickets } from '../../api/evolution'
import type { GapTicket } from '../../api/evolution'
import { formatTime, isForbidden } from './helpers'

const emit = defineEmits<{
  (e: 'error', message: string): void
  (e: 'forbidden'): void
  (e: 'clearError'): void
}>()

const gapTickets = ref<GapTicket[]>([])
const gapTicketsLoaded = ref(false)

async function loadGapTickets() {
  try {
    const data = await fetchGapTickets({ pageSize: 100 })
    gapTickets.value = data.items
    gapTicketsLoaded.value = true
    emit('clearError')
  } catch (err) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', '无法加载内容缺口工单')
  }
}

onMounted(() => {
  void loadGapTickets()
})
</script>

<template>
  <div class="tab-panel">
    <h3 class="panel-title">
      内容缺口工单（由 undetermined 聚类自动起草；补充材料入库后自动复测）
    </h3>
    <p v-if="gapTickets.length === 0" class="muted" data-test="gap-tickets-empty">
      暂无工单：只有“库里确实没有”的失败才会成为工单，且它永远不进候选池。
    </p>
    <div v-else class="table-card">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>知识库</th><th>状态</th><th>出现次数</th><th>问法样本</th>
            <th>召回窗口里没有的词</th><th>最近复测</th><th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="ticket in gapTickets" :key="ticket.id">
            <tr data-test="gap-ticket-row">
              <td>{{ ticket.id }}</td>
              <td>{{ ticket.kb_id }}</td>
              <td>{{ ticket.status }}</td>
              <td>{{ ticket.occurrences }}</td>
              <td>
                <div v-for="q in ticket.question_samples" :key="q" class="cluster-meta">{{ q }}</div>
              </td>
              <td>
                <code v-for="term in ticket.missing_terms" :key="term" class="term-chip">{{ term }}</code>
                <span v-if="ticket.missing_terms.length === 0" class="muted">（已无缺失）</span>
              </td>
              <td>
                <span v-if="ticket.recheck" class="cluster-meta" data-test="gap-ticket-recheck">
                  {{ ticket.recheck.verdict }} · 覆盖 {{ ticket.recheck.covered_terms.length }} 词
                </span>
                <span v-else class="muted">未复测</span>
              </td>
              <td>{{ formatTime(ticket.created_at) }}</td>
            </tr>
            <tr class="ticket-detail">
              <td colspan="8" data-test="gap-ticket-rationale">
                <span class="cluster-meta">{{ ticket.rationale }}</span>
                <span v-if="ticket.recheck" class="cluster-meta">
                  · 最近复测：仍缺
                  <code v-for="term in ticket.recheck.still_missing" :key="term" class="term-chip">
                    {{ term }}
                  </code>
                  <span v-if="ticket.recheck.still_missing.length === 0">（无，内容已到位）</span>
                </span>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
    <p class="cluster-meta">
      怎么用：把缺失词当作“该写什么”的线索 → 到该知识库用材料入库上传补充资料 →
      下一个 worker 轮次自动复测，覆盖后工单转 content_added。
    </p>
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

.panel-title { margin: 0 0 var(--space-3); font-size: var(--text-md); font-weight: var(--font-semibold); color: var(--color-foreground); }
.muted { color: var(--color-secondary); font-size: var(--text-sm); }
.cluster-meta { font-size: var(--text-xs); color: var(--color-secondary); }

.term-chip {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 1px 6px;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 10px;
}

@media (max-width: 900px) {
  .table-card { overflow: auto; }
}
</style>
