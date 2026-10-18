<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchEvolutionMetrics } from '../../api/evolution'
import type { EvolutionMetrics } from '../../api/evolution'
import { formatLatency, formatRate, isForbidden } from './helpers'

defineProps<{
  forbidden?: boolean
}>()

const emit = defineEmits<{
  (e: 'error', message: string): void
  (e: 'forbidden'): void
  (e: 'clearError'): void
}>()

const metrics = ref<EvolutionMetrics | null>(null)

async function loadMetrics() {
  try {
    if (typeof fetchEvolutionMetrics === 'function') {
      metrics.value = await fetchEvolutionMetrics()
      emit('clearError')
    }
  } catch (err) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', '无法加载观测指标')
  }
}

onMounted(() => {
  void loadMetrics()
})
</script>

<template>
  <div class="tab-panel">
    <div v-if="metrics" class="metrics" data-test="metrics-panel">
      <div class="metrics-cards">
        <div class="metric-card">
          <span class="metric-label">观测窗口</span>
          <strong data-test="metrics-window">{{ metrics.window_days }} 天</strong>
        </div>
        <div class="metric-card">
          <span class="metric-label">失败观测</span>
          <strong data-test="metrics-observations">{{ metrics.counts.observations }}</strong>
        </div>
        <div class="metric-card">
          <span class="metric-label">聚类（候选池）</span>
          <strong data-test="metrics-clusters">{{ metrics.counts.clusters }}（{{ metrics.counts.candidate_pool }}）</strong>
        </div>
        <div class="metric-card">
          <span class="metric-label">问题解决率</span>
          <strong data-test="metrics-solve-rate">{{ formatRate(metrics.rates.problem_solve_rate) }}</strong>
        </div>
        <div class="metric-card">
          <span class="metric-label">同类问题泛化率</span>
          <strong data-test="metrics-generalization-rate">{{ formatRate(metrics.rates.generalization_rate) }}</strong>
        </div>
        <div class="metric-card">
          <span class="metric-label">错误答案率</span>
          <strong data-test="metrics-wrong-answer-rate">{{ formatRate(metrics.rates.wrong_answer_rate) }}</strong>
        </div>
        <div class="metric-card">
          <span class="metric-label">拒答准确率</span>
          <strong data-test="metrics-refusal-rate">{{ formatRate(metrics.rates.refusal_accuracy_rate) }}</strong>
        </div>
        <div class="metric-card">
          <span class="metric-label">平均耗时</span>
          <strong data-test="metrics-latency">{{ formatLatency(metrics.rates.avg_latency_ms) }}</strong>
        </div>
      </div>
      <table class="metrics-table" data-test="metrics-definitions">
        <thead>
          <tr><th>指标</th><th>口径</th><th>可用阶段</th></tr>
        </thead>
        <tbody>
          <tr v-for="(definition, name) in metrics.definitions" :key="name">
            <td><code>{{ name }}</code></td>
            <td>{{ definition.definition }}</td>
            <td>第 {{ definition.available_from_phase }} 阶段</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else-if="!forbidden" class="empty-state" data-test="metrics-placeholder">
      <strong>指标加载中或无观测数据</strong>
      <span>第 2 阶段起持续累积观测；指标由存储信号确定性计算，样本不足时显示“样本不足”。</span>
    </div>
  </div>
</template>

<style scoped>
.tab-panel { display: grid; gap: var(--space-5); }

.metrics {
  display: grid;
  gap: var(--space-4);
}

.metrics-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
}

.metric-card {
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: var(--shadow-xs);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.metric-label {
  font-size: var(--text-xs);
  color: var(--color-secondary);
}

.metric-card strong {
  font-size: var(--text-lg);
  color: var(--color-foreground);
}

.metrics-table {
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

.muted, .empty-state { color: var(--color-secondary); font-size: var(--text-sm); }
.empty-state { min-height: 200px; display: grid; place-content: center; gap: var(--space-2); text-align: center; border: 1px dashed var(--color-border); border-radius: var(--radius-lg); background: var(--color-surface); }
.empty-state strong { color: var(--color-foreground); font-size: var(--text-base); }
</style>
