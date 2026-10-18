<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  fetchDiagnosis,
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  submitDecision
} from '../../api/evolution'
import type {
  EvolutionCluster,
  EvolutionDecisionAction,
  EvolutionDiagnosis,
  EvolutionEndpoint,
  EvolutionStateMachine
} from '../../api/evolution'
import {
  DRAFT_FIELDS,
  DRAFT_FIELD_LABELS,
  formatTime,
  isForbidden,
  poolLabel,
  shortHash,
  stageLabel
} from './helpers'

const props = defineProps<{
  stateMachine: EvolutionStateMachine | null
}>()

const emit = defineEmits<{
  (e: 'error', message: string): void
  (e: 'forbidden'): void
  (e: 'clearError'): void
}>()

const clusters = ref<EvolutionCluster[]>([])
const clustersLoaded = ref(false)
const selectedCluster = ref<EvolutionCluster | null>(null)
const diagnosis = ref<EvolutionDiagnosis | null>(null)
const diagnosisLoading = ref(false)
const decisionNote = ref('')
const confirmLayerValue = ref('')
const deciding = ref(false)
const decisionHint = ref('')

const transitions = computed<Record<string, string[]>>(
  () => props.stateMachine?.transitions ?? {}
)
const endpoints = computed<EvolutionEndpoint[]>(() => props.stateMachine?.endpoints ?? [])
const candidateClusters = computed(() =>
  clusters.value.filter((cluster) => cluster.pool === 'candidate_pool')
)
const queueClusters = computed(() =>
  clusters.value.filter((cluster) => cluster.pool === 'observation_queue')
)

/** 只有"待审"状态的聚类才接受诊断决策（与后端转移表一致，避免点了必然 409）。 */
const clusterDecidable = computed(() => selectedCluster.value?.status === 'awaiting_diagnosis_review')

async function loadClusters() {
  try {
    const data = await fetchEvolutionClusters({ pageSize: 100 })
    clusters.value = data.items
    clustersLoaded.value = true
    emit('clearError')
  } catch (err) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', '无法加载聚类列表')
  }
}

async function openCluster(cluster: EvolutionCluster) {
  try {
    selectedCluster.value = await fetchEvolutionCluster(cluster.id)
  } catch {
    emit('error', '无法加载聚类详情')
    return
  }
  decisionNote.value = ''
  await loadDiagnosis(cluster.id)
}

async function loadDiagnosis(clusterId: number) {
  diagnosisLoading.value = true
  try {
    diagnosis.value = await fetchDiagnosis(clusterId)
    emit('clearError')
  } catch (err) {
    diagnosis.value = null
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', '无法加载诊断草案')
  } finally {
    diagnosisLoading.value = false
  }
}

/** 确认根因层级：不动状态，只记录管理员判断（补丁生成器以它为准）。 */
async function confirmLayer() {
  if (!selectedCluster.value || !confirmLayerValue.value || deciding.value) return
  deciding.value = true
  try {
    const updated = await submitDecision(
      selectedCluster.value.id,
      'confirm_layer',
      decisionNote.value,
      confirmLayerValue.value
    )
    selectedCluster.value = updated
    decisionNote.value = ''
    confirmLayerValue.value = ''
    decisionHint.value = '已确认根因层级'
    emit('clearError')
    await loadDiagnosis(updated.id)
  } catch (err: any) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', err?.response?.data?.detail || '根因层级确认失败')
  } finally {
    deciding.value = false
  }
}

async function decide(action: EvolutionDecisionAction) {
  if (!selectedCluster.value || deciding.value) return
  deciding.value = true
  try {
    const updated = await submitDecision(selectedCluster.value.id, action, decisionNote.value)
    selectedCluster.value = updated
    decisionNote.value = ''
    emit('clearError')
    await loadDiagnosis(updated.id)
  } catch (err: any) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', err?.response?.data?.detail || '决策提交失败')
  } finally {
    deciding.value = false
  }
}

onMounted(() => {
  void loadClusters()
})
</script>

<template>
  <div class="tab-panel">
    <div class="pool-group" data-test="candidate-pool">
      <h3 class="panel-title">候选池（同类根因累计三次自动进入）</h3>
      <p v-if="candidateClusters.length === 0" class="muted">暂无候选池聚类</p>
      <button
        v-for="cluster in candidateClusters"
        :key="cluster.id"
        type="button"
        class="cluster-card"
        data-test="cluster-card"
        @click="openCluster(cluster)"
      >
        <span class="cluster-status">{{ cluster.status }}</span>
        <span class="cluster-meta">{{ cluster.kb_id }} · {{ stageLabel(cluster.failure_stage) }} · {{ cluster.root_cause_feature }}</span>
        <span class="cluster-meta">成员 {{ cluster.member_count }} · 优先级 {{ cluster.priority }}</span>
      </button>
    </div>

    <div class="pool-group" data-test="observation-queue">
      <h3 class="panel-title">观察队列（不可复现或未达阈值的停留区）</h3>
      <p v-if="queueClusters.length === 0" class="muted">暂无观察队列聚类</p>
      <button
        v-for="cluster in queueClusters"
        :key="cluster.id"
        type="button"
        class="cluster-card"
        data-test="cluster-card"
        @click="openCluster(cluster)"
      >
        <span class="cluster-status">{{ cluster.status }}</span>
        <span class="cluster-meta">{{ cluster.kb_id }} · {{ stageLabel(cluster.failure_stage) }} · {{ cluster.root_cause_feature }}</span>
        <span class="cluster-meta">成员 {{ cluster.member_count }} · 优先级 {{ cluster.priority }}</span>
      </button>
    </div>

    <div v-if="selectedCluster" class="cluster-detail" data-test="cluster-detail">
      <h3 class="panel-title">聚类详情 #{{ selectedCluster.id }}</h3>
      <p class="cluster-meta">
        {{ selectedCluster.cluster_key }} · {{ poolLabel(selectedCluster.pool) }} ·
        {{ selectedCluster.status }} · 成员 {{ selectedCluster.member_count }}
      </p>
      <table>
        <thead>
          <tr><th>ID</th><th>知识库</th><th>失败阶段</th><th>根因特征</th><th>状态</th><th>最佳分数</th></tr>
        </thead>
        <tbody>
          <tr v-for="member in selectedCluster.members || []" :key="member.id">
            <td>{{ member.id }}</td>
            <td>{{ member.kb_id }}</td>
            <td>{{ stageLabel(member.failure_stage) }}</td>
            <td>{{ member.root_cause_feature || '未判定' }}</td>
            <td>{{ member.status }}</td>
            <td>{{ member.best_score ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="selectedCluster" class="cluster-detail" data-test="diagnosis-panel">
      <h3 class="panel-title">诊断草案 #{{ selectedCluster.id }}</h3>
      <p v-if="diagnosisLoading" class="muted">加载诊断草案…</p>
      <template v-else-if="diagnosis">
        <div v-if="diagnosis.cluster_draft" class="draft-block" data-test="cluster-draft">
          <p class="block-caption">聚类级遗留草案（第 3 阶段按聚类生成，仅供对照）</p>
          <div class="draft-head">
            <span
              class="badge"
              :class="{ 'badge-warn': !diagnosis.cluster_draft.reproducible }"
              data-test="cluster-draft-reproducible"
            >
              {{ diagnosis.cluster_draft.reproducible ? '可复现' : '不可复现' }}
            </span>
            <span class="cluster-meta">
              置信度 {{ diagnosis.cluster_draft.payload.confidence }} · 提示词版本
              {{ diagnosis.cluster_draft.prompt_version || '—' }} · 模型 {{ diagnosis.cluster_draft.model || '—' }}
            </span>
          </div>
          <dl class="draft-grid">
            <template v-for="field in DRAFT_FIELDS" :key="field">
              <dt>{{ DRAFT_FIELD_LABELS[field] }}</dt>
              <dd :data-test="`cluster-draft-field-${field}`">{{ diagnosis.cluster_draft.payload[field] }}</dd>
            </template>
          </dl>
          <p class="draft-refs">
            <span>证据引用（块哈希）：</span>
            <span
              v-for="hash in diagnosis.cluster_draft.payload.evidence_refs"
              :key="hash"
              class="hash"
              data-test="cluster-draft-evidence-ref"
            >{{ shortHash(hash) }}</span>
            <span v-if="diagnosis.cluster_draft.payload.evidence_refs.length === 0">无</span>
          </p>
        </div>

        <div
          v-for="member in diagnosis.member_drafts"
          :key="member.observation_id"
          class="draft-block"
          data-test="member-draft"
        >
          <p class="member-question" data-test="member-draft-question">
            #{{ member.observation_id }} · {{ member.question }}
            <span class="cluster-meta"> · {{ stageLabel(member.stage) }} · {{ member.root_cause_feature || '未判定' }}</span>
          </p>
          <template v-if="member.draft">
            <div class="draft-head">
              <span
                class="badge"
                :class="{ 'badge-warn': !member.draft.reproducible }"
                data-test="member-draft-reproducible"
              >
                {{ member.draft.reproducible ? '可复现' : '不可复现' }}
              </span>
              <span class="cluster-meta">
                置信度 {{ member.draft.payload.confidence }} · 提示词版本
                {{ member.draft.prompt_version || '—' }} · 模型 {{ member.draft.model || '—' }}
              </span>
            </div>
            <dl class="draft-grid">
              <template v-for="field in DRAFT_FIELDS" :key="field">
                <dt>{{ DRAFT_FIELD_LABELS[field] }}</dt>
                <dd :data-test="`member-draft-field-${field}`">{{ member.draft.payload[field] }}</dd>
              </template>
            </dl>
            <p class="draft-refs">
              <span>证据引用（块哈希）：</span>
              <span
                v-for="hash in member.draft.payload.evidence_refs"
                :key="hash"
                class="hash"
                data-test="member-draft-evidence-ref"
              >{{ shortHash(hash) }}</span>
              <span v-if="member.draft.payload.evidence_refs.length === 0">无</span>
            </p>
          </template>
          <p v-else class="muted member-draft-empty" data-test="member-draft-empty">暂无草案</p>
        </div>

        <p
          v-if="!diagnosis.cluster_draft && diagnosis.member_drafts.length === 0"
          class="muted"
          data-test="diagnosis-empty"
        >暂无草案</p>
      </template>
      <p v-else class="muted" data-test="diagnosis-empty">暂无草案</p>

      <h4 class="panel-subtitle">决策记录（管理员三动作全部留痕）</h4>
      <ul
        v-if="diagnosis && diagnosis.audit.length > 0"
        class="audit-list"
        data-test="diagnosis-audit"
      >
        <li v-for="row in diagnosis.audit" :key="row.id">
          <span class="audit-action">{{ row.action }}</span>
          <span class="cluster-meta">
            {{ row.actor_id }} · {{ formatTime(row.created_at) }} · {{ row.note || '无备注' }}
          </span>
        </li>
      </ul>
      <p v-else class="muted">暂无决策记录</p>

      <p v-if="decisionHint" class="cluster-meta" data-test="decision-hint">
        {{ decisionHint }}（轨迹见下方门禁与审计）
      </p>

      <div class="layer-confirm" data-test="layer-confirm">
        <label>确认根因层级（补丁生成器以它为准）</label>
        <select v-model="confirmLayerValue" data-test="layer-confirm-select">
          <option value="">— 未确认 —</option>
          <option
            v-for="layer in stateMachine?.confirmable_layers || []"
            :key="layer"
            :value="layer"
          >
            {{ layer }}
          </option>
        </select>
        <button
          type="button"
          class="query-button"
          data-test="layer-confirm-submit"
          :disabled="!confirmLayerValue || deciding"
          @click="confirmLayer"
        >
          确认层级
        </button>
      </div>

      <div class="decision-bar">
        <textarea
          v-model="decisionNote"
          class="decision-note"
          data-test="decision-note"
          rows="2"
          placeholder="备注：确认根因的依据、驳回理由或还缺的证据"
          :disabled="deciding"
        ></textarea>
        <div class="decision-buttons">
          <button
            type="button"
            class="query-button"
            data-test="decision-approve"
            :disabled="deciding || !clusterDecidable"
            @click="decide('approve_for_candidate')"
          >
            确认进入候选
          </button>
          <button
            type="button"
            class="decision-button"
            data-test="decision-reject"
            :disabled="deciding || !clusterDecidable"
            @click="decide('reject')"
          >
            拒绝
          </button>
          <button
            type="button"
            class="decision-button"
            data-test="decision-needs-more-evidence"
            :disabled="deciding || !clusterDecidable"
            @click="decide('needs_more_evidence')"
          >
            证据不足
          </button>
        </div>
      </div>
    </div>

    <div class="state-machine" data-test="state-machine-legend">
      <h3 class="panel-title">状态机（设计第 7 节，只读）</h3>
      <p v-if="stateMachine?.note" class="note">{{ stateMachine.note }}</p>
      <ul>
        <li v-for="(targets, status) in transitions" :key="status">
          <code>{{ status }}</code> → <span>{{ targets.join('、') || '终态' }}</span>
        </li>
      </ul>
      <h4 class="panel-subtitle">端点清单</h4>
      <ul class="endpoints" data-test="endpoint-inventory">
        <li v-for="endpoint in endpoints" :key="endpoint.path">
          <span class="method">{{ endpoint.methods.join('/') }}</span>
          <code>{{ endpoint.path }}</code>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.tab-panel { display: grid; gap: var(--space-5); }

.pool-group, .cluster-detail, .state-machine {
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

.cluster-card {
  display: grid;
  gap: var(--space-1);
  width: 100%;
  margin-bottom: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface-2);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.cluster-card:hover { border-color: var(--color-primary); }
.cluster-status { font-size: var(--text-sm); font-weight: var(--font-semibold); color: var(--color-primary); }
.cluster-meta { font-size: var(--text-xs); color: var(--color-secondary); }

.hash { display: inline-block; padding: 2px 6px; margin-right: var(--space-1); border-radius: var(--radius); background: var(--color-surface-2); font-family: ui-monospace, monospace; font-size: var(--text-xs); }

.draft-head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); margin-bottom: var(--space-3); }
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

.draft-grid { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--space-2) var(--space-4); margin: 0; }
.draft-grid dt { color: var(--color-secondary); font-size: var(--text-xs); }
.draft-grid dd { margin: 0; color: var(--color-foreground); font-size: var(--text-sm); white-space: pre-wrap; word-break: break-word; }

.draft-refs { margin: var(--space-3) 0 0; color: var(--color-secondary); font-size: var(--text-xs); }

.draft-block { padding: var(--space-3) 0; border-bottom: 1px solid var(--color-border); }
.draft-block:last-of-type { border-bottom: none; }
.member-question { margin: 0 0 var(--space-2); color: var(--color-foreground); font-size: var(--text-sm); font-weight: var(--font-medium); }
.block-caption { margin: 0 0 var(--space-2); color: var(--color-secondary); font-size: var(--text-xs); }
.member-draft-empty { margin: 0; }

.audit-list { list-style: none; margin: 0; padding-left: 0; }
.audit-list li { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-2); padding: var(--space-2) 0; border-bottom: 1px solid var(--color-border); }
.audit-action { color: var(--color-primary); font-family: ui-monospace, monospace; font-size: var(--text-xs); font-weight: var(--font-semibold); }

.decision-bar { display: grid; gap: var(--space-3); margin-top: var(--space-4); padding-top: var(--space-4); border-top: 1px solid var(--color-border); }
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

.state-machine ul { margin: 0; padding-left: var(--space-5); color: var(--color-foreground); font-size: var(--text-sm); }
.state-machine li { margin-bottom: var(--space-1); }
.state-machine code, .endpoints code { font-family: ui-monospace, monospace; font-size: var(--text-xs); }
.note { margin: 0 0 var(--space-3); color: var(--color-secondary); font-size: var(--text-sm); }
.endpoints { list-style: none; padding-left: 0; }
.method { display: inline-block; min-width: 44px; margin-right: var(--space-2); color: var(--color-primary); font-size: var(--text-xs); font-weight: var(--font-semibold); }
</style>
