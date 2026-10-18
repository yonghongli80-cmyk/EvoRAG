<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  fetchCandidate,
  fetchCandidates,
  rollbackCandidate,
  submitCanary
} from '../../api/evolution'
import type {
  EvolutionCandidate,
  EvolutionCandidateDetail
} from '../../api/evolution'
import {
  formatTime,
  gateLabel,
  gateResultLabel,
  isForbidden,
  payloadFields,
  targetLabel
} from './helpers'

const emit = defineEmits<{
  (e: 'error', message: string): void
  (e: 'forbidden'): void
  (e: 'clearError'): void
}>()

const candidates = ref<EvolutionCandidate[]>([])
const candidatesLoaded = ref(false)
const selectedCandidate = ref<EvolutionCandidateDetail | null>(null)
const selectedCandidateId = ref<number | null>(null)
const candidateLoading = ref(false)
const canaryPercent = ref('5')
const candidateActing = ref(false)

async function loadCandidates() {
  candidateLoading.value = true
  try {
    const data = await fetchCandidates({ pageSize: 100 })
    candidates.value = data.items
    candidatesLoaded.value = true
    emit('clearError')
  } catch (err) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', '无法加载候选列表')
  } finally {
    candidateLoading.value = false
  }
}

async function openCandidate(candidate: EvolutionCandidate) {
  selectedCandidateId.value = candidate.id
  candidateLoading.value = true
  try {
    selectedCandidate.value = await fetchCandidate(candidate.id)
    canaryPercent.value = String(selectedCandidate.value.canary_percent || 5)
    emit('clearError')
  } catch (err) {
    selectedCandidate.value = null
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', '无法加载候选详情')
  } finally {
    candidateLoading.value = false
  }
}

/** Re-read the open candidate after an action: the trail and the percentage moved. */
async function reloadCandidate() {
  if (selectedCandidateId.value === null) return
  await openCandidate({ id: selectedCandidateId.value } as EvolutionCandidate)
}

/** Parse the percentage box; returns null when it is not an integer 0..100. */
function parsePercent(): number | null {
  const percent = Number(canaryPercent.value)
  if (!Number.isInteger(percent) || percent < 0 || percent > 100) return null
  return percent
}

async function canary() {
  if (!selectedCandidate.value || candidateActing.value) return
  const percent = parsePercent()
  if (percent === null) {
    emit('error', '灰度百分比必须是 0..100 的整数')
    return
  }
  candidateActing.value = true
  try {
    await submitCanary(selectedCandidate.value.id, percent)
    emit('clearError')
    await reloadCandidate()
    await loadCandidates()
  } catch (err: any) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', err?.response?.data?.detail || '灰度提交失败')
  } finally {
    candidateActing.value = false
  }
}

async function rollback() {
  if (!selectedCandidate.value || candidateActing.value) return
  candidateActing.value = true
  try {
    await rollbackCandidate(selectedCandidate.value.id)
    emit('clearError')
    await reloadCandidate()
    await loadCandidates()
  } catch (err: any) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', err?.response?.data?.detail || '回滚失败')
  } finally {
    candidateActing.value = false
  }
}

onMounted(() => {
  void loadCandidates()
})
</script>

<template>
  <div class="tab-panel">
    <div class="table-card">
      <h3 class="panel-title">候选问题集（层 1-3 通过后由管理员按百分比灰度）</h3>
      <p v-if="candidateLoading && candidates.length === 0" class="muted" data-test="candidates-loading">
        加载候选中…
      </p>
      <p v-else-if="candidates.length === 0" class="muted" data-test="candidates-empty">
        暂无候选：只有经管理员确认根因的失败聚类才会生成问题集候选，五层门禁逐层放行。
      </p>
      <table v-else data-test="candidate-list">
        <thead>
          <tr>
            <th>ID</th><th>目标</th><th>状态</th><th>置信度</th><th>灰度百分比</th>
            <th>候选来源</th><th>提示词 / 模型</th><th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="candidate in candidates"
            :key="candidate.id"
            data-test="candidate-row"
            :class="{ 'candidate-row-active': selectedCandidateId === candidate.id }"
            @click="openCandidate(candidate)"
          >
            <td>{{ candidate.id }}</td>
            <td>{{ targetLabel(candidate.target) }}</td>
            <td>
              <span class="badge" :class="{ 'badge-warn': candidate.status === 'rolled_back' }">
                {{ candidate.status }}
              </span>
            </td>
            <td>{{ candidate.confidence }}</td>
            <td data-test="candidate-row-percent">{{ candidate.canary_percent }}%</td>
            <td>聚类 #{{ candidate.cluster_id }}{{ candidate.observation_id ? ` · 观测 #${candidate.observation_id}` : '' }}</td>
            <td>{{ candidate.prompt_version || '—' }} / {{ candidate.model || '—' }}</td>
            <td>{{ formatTime(candidate.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="selectedCandidate" class="candidate-detail" data-test="candidate-detail">
      <h3 class="panel-title">候选 #{{ selectedCandidate.id }}（{{ targetLabel(selectedCandidate.target) }}）</h3>
      <p v-if="candidateLoading" class="muted">加载候选项…</p>
      <template v-else>
        <p class="cluster-meta">
          {{ selectedCandidate.status }} · 置信度 {{ selectedCandidate.confidence }} · 灰度
          {{ selectedCandidate.canary_percent }}%
        </p>
        <dl class="draft-grid">
          <dt>生成理由</dt>
          <dd data-test="candidate-rationale">{{ selectedCandidate.rationale }}</dd>
          <dt>预期效果</dt>
          <dd data-test="candidate-expected-effect">{{ selectedCandidate.expected_effect }}</dd>
          <dt>回滚引用</dt>
          <dd data-test="candidate-rollback-ref">
            question_set_version={{ selectedCandidate.rollback_ref.question_set_version || '—' }}
            · payload_sha256={{ selectedCandidate.rollback_ref.payload_sha256 || '—' }}
          </dd>
        </dl>

        <h4 class="panel-subtitle">五层门禁轨迹</h4>
        <ul v-if="selectedCandidate.gate_log.length > 0" class="audit-list" data-test="candidate-gate-log">
          <li v-for="(entry, index) in selectedCandidate.gate_log" :key="index" data-test="candidate-gate-entry">
            <span class="audit-action">{{ gateLabel(entry.gate) }}</span>
            <span class="badge" :class="{ 'badge-warn': entry.result !== 'passed' && entry.result !== 'accepted' }">
              {{ gateResultLabel(entry.result) }}
            </span>
            <span class="cluster-meta">{{ entry.detail || '—' }} · {{ formatTime(entry.created_at) }}</span>
          </li>
        </ul>
        <p v-else class="muted">暂无门禁记录</p>

        <h4 class="panel-subtitle">载荷摘要（文本留在 evolution.db，不在界面外泄）</h4>
        <p v-if="selectedCandidate.payload_summary" class="cluster-meta" data-test="candidate-payload-summary">
          共 {{ selectedCandidate.payload_summary.question_count }} 个问题，改动
          {{ selectedCandidate.payload_summary.overridden }} 个：
          <span
            v-for="question in selectedCandidate.payload_summary.questions"
            :key="question.question_id"
            class="hash"
            data-test="candidate-payload-question"
          >
            {{ question.question_id }}({{ payloadFields(question.fields) || '未改动' }})
          </span>
        </p>

        <h4 class="panel-subtitle">生命周期审计</h4>
        <ul v-if="selectedCandidate.audit.length > 0" class="audit-list" data-test="candidate-audit">
          <li v-for="row in selectedCandidate.audit" :key="row.id">
            <span class="audit-action">{{ row.action }}</span>
            <span class="cluster-meta">{{ row.actor_id }} · {{ formatTime(row.created_at) }} · {{ row.note || '无备注' }}</span>
          </li>
        </ul>
        <p v-else class="muted">暂无审计记录</p>

        <div class="decision-bar">
          <div class="candidate-actions">
            <label for="candidate-percent">灰度百分比（0..100，按请求确定性分流）</label>
            <input
              id="candidate-percent"
              v-model="canaryPercent"
              class="decision-note"
              type="number"
              min="0"
              max="100"
              step="1"
              data-test="candidate-percent-input"
              :disabled="candidateActing"
            >
            <div class="decision-buttons">
              <button
                type="button"
                class="query-button"
                data-test="candidate-canary"
                :disabled="candidateActing"
                @click="canary()"
              >
                按百分比灰度
              </button>
              <button
                type="button"
                class="decision-button"
                data-test="candidate-rollback"
                :disabled="candidateActing"
                @click="rollback()"
              >
                一键回滚（置 0 + 审计）
              </button>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.tab-panel { display: grid; gap: var(--space-5); }

.table-card, .candidate-detail {
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
.panel-subtitle { margin: var(--space-4) 0 var(--space-2); font-size: var(--text-sm); font-weight: var(--font-semibold); color: var(--color-secondary); }

.muted { color: var(--color-secondary); font-size: var(--text-sm); }

.candidate-row-active { background: #f8fbff; }

.badge {
  padding: 2px var(--space-3);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius);
  color: var(--color-primary);
  background: var(--color-surface-2);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
}
.badge-warn { color: var(--color-destructive); border-color: #fecaca; background: var(--color-destructive-light); }

.cluster-meta { font-size: var(--text-xs); color: var(--color-secondary); }

.hash { display: inline-block; padding: 2px 6px; margin-right: var(--space-1); border-radius: var(--radius); background: var(--color-surface-2); font-family: ui-monospace, monospace; font-size: var(--text-xs); }

.draft-grid { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--space-2) var(--space-4); margin: 0 0 var(--space-4); }
.draft-grid dt { color: var(--color-secondary); font-size: var(--text-xs); }
.draft-grid dd { margin: 0; color: var(--color-foreground); font-size: var(--text-sm); white-space: pre-wrap; word-break: break-word; }

.audit-list { list-style: none; margin: 0; padding-left: 0; }
.audit-list li { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-2); padding: var(--space-2) 0; border-bottom: 1px solid var(--color-border); }
.audit-action { color: var(--color-primary); font-family: ui-monospace, monospace; font-size: var(--text-xs); font-weight: var(--font-semibold); }

.decision-bar { display: grid; gap: var(--space-3); margin-top: var(--space-4); padding-top: var(--space-4); border-top: 1px solid var(--color-border); }
.candidate-actions { display: grid; gap: var(--space-2); }
.candidate-actions label { color: var(--color-secondary); font-size: var(--text-xs); }
.candidate-actions .decision-note { max-width: 160px; }

.decision-note {
  width: 100%;
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface);
  font: inherit;
  font-size: var(--text-sm);
  box-sizing: border-box;
  resize: vertical;
}

.decision-buttons { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.decision-buttons button { min-height: 34px; padding: 0 var(--space-4); font-size: var(--text-xs); }
.decision-buttons button:disabled { cursor: not-allowed; opacity: .5; }

.decision-button {
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface-2);
  font: inherit;
  cursor: pointer;
}

.query-button {
  min-height: 38px;
  padding: 0 var(--space-5);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius);
  color: var(--color-on-primary);
  background: var(--color-primary);
  font: inherit;
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  cursor: pointer;
}

@media (max-width: 900px) {
  .table-card { overflow: auto; }
}
</style>
