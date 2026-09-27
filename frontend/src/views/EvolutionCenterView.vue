<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  fetchCandidate,
  fetchCandidates,
  fetchCodePatch,
  fetchCodePatches,
  fetchDiagnosis,
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  fetchEvolutionMetrics,
  fetchEvolutionObservations,
  fetchEvolutionStateMachine,
  rollbackCandidate,
  submitCanary,
  submitCodePatchDecision,
  submitDecision
} from '../api/evolution'
import type {
  CodePatchDecisionAction,
  EvolutionCandidate,
  EvolutionCandidateDetail,
  EvolutionCluster,
  EvolutionCodePatch,
  EvolutionCodePatchDetail,
  EvolutionDecisionAction,
  EvolutionDiagnosis,
  EvolutionEndpoint,
  EvolutionMetrics,
  EvolutionObservation,
  EvolutionStateMachine
} from '../api/evolution'

type Tab = 'observations' | 'clusters' | 'metrics' | 'candidates' | 'code-patches'

const PAGE_SIZE = 20

/** The nine design-section-6 text fields, in the order an administrator reads them. */
const DRAFT_FIELDS = [
  'failure_category',
  'stage',
  'first_divergence',
  'direct_evidence',
  'root_cause',
  'impact_scope',
  'suggested_layer',
  'reproduction_steps',
  'acceptance_criteria'
] as const

const DRAFT_FIELD_LABELS: Record<(typeof DRAFT_FIELDS)[number], string> = {
  failure_category: '失败类别',
  stage: '发生阶段',
  first_divergence: '第一处实质偏离',
  direct_evidence: '直接证据',
  root_cause: '根因（假设级）',
  impact_scope: '影响范围',
  suggested_layer: '建议修改层级',
  reproduction_steps: '复现方法',
  acceptance_criteria: '验收标准'
}

const activeTab = ref<Tab>('observations')
const observations = ref<EvolutionObservation[]>([])
const observationsTotal = ref(0)
const page = ref(1)
const statusFilter = ref('')
const kbFilter = ref('')
const clusters = ref<EvolutionCluster[]>([])
const clustersLoaded = ref(false)
const selectedCluster = ref<EvolutionCluster | null>(null)
const stateMachine = ref<EvolutionStateMachine | null>(null)
const loading = ref(false)
const error = ref('')
const forbidden = ref(false)
const diagnosis = ref<EvolutionDiagnosis | null>(null)
const diagnosisLoading = ref(false)
const decisionNote = ref('')
const deciding = ref(false)
const metrics = ref<EvolutionMetrics | null>(null)
const candidates = ref<EvolutionCandidate[]>([])
const candidatesLoaded = ref(false)
const selectedCandidate = ref<EvolutionCandidateDetail | null>(null)
const selectedCandidateId = ref<number | null>(null)
const candidateLoading = ref(false)
const canaryPercent = ref('5')
const candidateActing = ref(false)
// Task 4: the code-patch review tab. There is no percentage to set — layer 4 for
// a code patch is the administrator review, and ``approve_for_apply`` only
// exports the patch, so the buttons say what they actually do.
const codePatches = ref<EvolutionCodePatch[]>([])
const codePatchesLoaded = ref(false)
const selectedCodePatch = ref<EvolutionCodePatchDetail | null>(null)
const selectedCodePatchId = ref<number | null>(null)
const codePatchLoading = ref(false)
const codePatchNote = ref('')
const codePatchActing = ref(false)

const totalPages = computed(() => Math.max(1, Math.ceil(observationsTotal.value / PAGE_SIZE)))
const statusOptions = computed(() => stateMachine.value?.statuses ?? [])
const transitions = computed<Record<string, string[]>>(
  () => stateMachine.value?.transitions ?? {}
)
const endpoints = computed<EvolutionEndpoint[]>(() => stateMachine.value?.endpoints ?? [])
const candidateClusters = computed(() =>
  clusters.value.filter((cluster) => cluster.pool === 'candidate_pool')
)
const queueClusters = computed(() =>
  clusters.value.filter((cluster) => cluster.pool === 'observation_queue')
)

function isForbidden(err: any): boolean {
  return err?.response?.status === 403
}

function formatTime(timestamp: number): string {
  return new Date(timestamp * 1000).toISOString().slice(0, 19).replace('T', ' ')
}

function shortHash(hash: string): string {
  return hash.length > 12 ? `${hash.slice(0, 12)}…` : hash
}

function stageLabel(stage: string): string {
  return ({
    retrieval: '检索',
    generation: '生成',
    structured_query: '结构化查询',
    tool: '工具',
    unknown: '未判定'
  } as Record<string, string>)[stage] || stage
}

function poolLabel(pool: string): string {
  return pool === 'candidate_pool' ? '候选池' : '观察队列'
}

async function loadObservations() {
  try {
    const data = await fetchEvolutionObservations({
      status: statusFilter.value || undefined,
      kbId: kbFilter.value || undefined,
      page: page.value,
      pageSize: PAGE_SIZE
    })
    observations.value = data.items
    observationsTotal.value = data.total
    error.value = ''
  } catch (err) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载失败观察列表'
  }
}

async function loadClusters() {
  try {
    const data = await fetchEvolutionClusters({ pageSize: 100 })
    clusters.value = data.items
    clustersLoaded.value = true
    error.value = ''
  } catch (err) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载聚类列表'
  }
}

async function loadStateMachine() {
  try {
    stateMachine.value = await fetchEvolutionStateMachine()
  } catch (err) {
    if (isForbidden(err)) forbidden.value = true
  }
}

async function loadMetrics() {
  try {
    metrics.value = await fetchEvolutionMetrics()
    error.value = ''
  } catch (err) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载观测指标'
  }
}

function formatRate(rate: number | null): string {
  return rate === null ? '样本不足' : `${(rate * 100).toFixed(1)}%`
}

function formatLatency(ms: number | null): string {
  return ms === null ? '样本不足' : `${ms.toFixed(0)} ms`
}

async function openCluster(cluster: EvolutionCluster) {
  try {
    selectedCluster.value = await fetchEvolutionCluster(cluster.id)
  } catch {
    error.value = '无法加载聚类详情'
    return
  }
  decisionNote.value = ''
  await loadDiagnosis(cluster.id)
}

async function loadDiagnosis(clusterId: number) {
  diagnosisLoading.value = true
  try {
    diagnosis.value = await fetchDiagnosis(clusterId)
  } catch (err) {
    diagnosis.value = null
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载诊断草案'
  } finally {
    diagnosisLoading.value = false
  }
}

async function decide(action: EvolutionDecisionAction) {
  if (!selectedCluster.value || deciding.value) return
  deciding.value = true
  try {
    const updated = await submitDecision(selectedCluster.value.id, action, decisionNote.value)
    selectedCluster.value = updated
    decisionNote.value = ''
    error.value = ''
    await loadDiagnosis(updated.id)
  } catch (err: any) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = err?.response?.data?.detail || '决策提交失败'
  } finally {
    deciding.value = false
  }
}

function applyFilters() {
  page.value = 1
  void loadObservations()
}

function changePage(next: number) {
  page.value = next
  void loadObservations()
}

function showClusters() {
  activeTab.value = 'clusters'
  if (!clustersLoaded.value) void loadClusters()
}

function showMetrics() {
  activeTab.value = 'metrics'
  if (metrics.value === null) void loadMetrics()
}

/** The five gate layers in the order an administrator reads the trail. */
const GATE_LABELS: Record<string, string> = {
  scope: '层 1 产物边界',
  benchmark: '层 2 基准门禁',
  shadow: '层 3 影子对比',
  canary: '层 4 灰度',
  rollback: '层 5 回滚'
}

const GATE_RESULT_LABELS: Record<string, string> = {
  accepted: '通过',
  passed: '通过',
  failed: '未通过',
  blocked: '阻塞',
  inconclusive: '样本不足'
}

function gateLabel(gate: string): string {
  return GATE_LABELS[gate] || gate
}

function gateResultLabel(result: string): string {
  return GATE_RESULT_LABELS[result] || result
}

function targetLabel(target: string): string {
  return target === 'passages' ? '段落判定' : '预判'
}

function payloadFields(fields: string[]): string {
  return fields.map((field) => (field === 'instructions' ? 'instructions' : 'criteria')).join(' + ')
}

async function loadCandidates() {
  candidateLoading.value = true
  try {
    const data = await fetchCandidates({ pageSize: 100 })
    candidates.value = data.items
    candidatesLoaded.value = true
    error.value = ''
  } catch (err) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载候选列表'
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
    error.value = ''
  } catch (err) {
    selectedCandidate.value = null
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载候选详情'
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
    error.value = '灰度百分比必须是 0..100 的整数'
    return
  }
  candidateActing.value = true
  try {
    await submitCanary(selectedCandidate.value.id, percent)
    error.value = ''
    await reloadCandidate()
    await loadCandidates()
  } catch (err: any) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = err?.response?.data?.detail || '灰度提交失败'
  } finally {
    candidateActing.value = false
  }
}

async function rollback() {
  if (!selectedCandidate.value || candidateActing.value) return
  candidateActing.value = true
  try {
    await rollbackCandidate(selectedCandidate.value.id)
    error.value = ''
    await reloadCandidate()
    await loadCandidates()
  } catch (err: any) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = err?.response?.data?.detail || '回滚失败'
  } finally {
    candidateActing.value = false
  }
}

function showCandidates() {
  activeTab.value = 'candidates'
  if (!candidatesLoaded.value) void loadCandidates()
}

/** The five evolution layers a code patch may address, as the boundary names them. */
const LAYER_LABELS: Record<string, string> = {
  chunking: '切块策略',
  reranking: '重排流程',
  parser: '解析器',
  tool_workflow: '工具工作流',
  code: '项目代码'
}

function layerLabel(layer: string): string {
  return LAYER_LABELS[layer] || layer
}

function shortCommit(commit: string): string {
  return commit.length > 12 ? `${commit.slice(0, 12)}…` : commit
}

async function loadCodePatches() {
  codePatchLoading.value = true
  try {
    const data = await fetchCodePatches({ pageSize: 100 })
    codePatches.value = data.items
    codePatchesLoaded.value = true
    error.value = ''
  } catch (err) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载补丁候选列表'
  } finally {
    codePatchLoading.value = false
  }
}

async function openCodePatch(patch: EvolutionCodePatch) {
  selectedCodePatchId.value = patch.id
  codePatchLoading.value = true
  try {
    selectedCodePatch.value = await fetchCodePatch(patch.id)
    codePatchNote.value = ''
    error.value = ''
  } catch (err) {
    selectedCodePatch.value = null
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载补丁详情'
  } finally {
    codePatchLoading.value = false
  }
}

/** Re-read the open patch after a decision: the trail and the status moved. */
async function reloadCodePatch() {
  if (selectedCodePatchId.value === null) return
  await openCodePatch({ id: selectedCodePatchId.value } as EvolutionCodePatch)
}

async function decideCodePatch(action: CodePatchDecisionAction) {
  if (!selectedCodePatch.value || codePatchActing.value) return
  codePatchActing.value = true
  try {
    await submitCodePatchDecision(selectedCodePatch.value.id, action, codePatchNote.value)
    error.value = ''
    codePatchNote.value = ''
    await reloadCodePatch()
    await loadCodePatches()
  } catch (err: any) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = err?.response?.data?.detail || '审核决策提交失败'
  } finally {
    codePatchActing.value = false
  }
}

function showCodePatches() {
  activeTab.value = 'code-patches'
  if (!codePatchesLoaded.value) void loadCodePatches()
}

onMounted(async () => {
  loading.value = true
  try {
    await Promise.all([loadObservations(), loadStateMachine()])
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <section class="evolution" data-test="evolution-view">
    <header class="page-header">
      <div>
        <span class="eyebrow">EVOLUTION CENTER</span>
        <h2>进化中心</h2>
        <p>只读观测失败轨迹、聚类准入与状态机；诊断草案仅供管理员确认根因，未确认前不生成候选、不改动在线链路。</p>
      </div>
      <div class="filters">
        <select v-model="statusFilter" aria-label="状态筛选" @change="applyFilters">
          <option value="">全部状态</option>
          <option v-for="status in statusOptions" :key="status" :value="status">{{ status }}</option>
        </select>
        <input v-model="kbFilter" placeholder="知识库 ID" aria-label="知识库筛选" @keyup.enter="applyFilters">
        <button class="query-button" type="button" data-test="observation-filter" @click="applyFilters">查询</button>
      </div>
    </header>

    <p v-if="forbidden" class="error-banner" role="alert" data-test="evolution-forbidden">
      需要管理员权限：进化中心仅对管理员开放，普通成员不会看到任何观测数据。
    </p>
    <p v-else-if="error" class="error-banner" role="alert" data-test="evolution-error">{{ error }}</p>

    <nav class="tabs" aria-label="进化中心视图">
      <button
        type="button"
        class="tab"
        :class="{ active: activeTab === 'observations' }"
        data-test="tab-observations"
        @click="activeTab = 'observations'"
      >
        问题观察
      </button>
      <button
        type="button"
        class="tab"
        :class="{ active: activeTab === 'clusters' }"
        data-test="tab-clusters"
        @click="showClusters"
      >
        聚类与状态机
      </button>
      <button
        type="button"
        class="tab"
        :class="{ active: activeTab === 'metrics' }"
        data-test="tab-metrics"
        @click="showMetrics()"
      >
        指标看板
      </button>
      <button
        type="button"
        class="tab"
        :class="{ active: activeTab === 'candidates' }"
        data-test="tab-candidates"
        @click="showCandidates"
      >
        候选灰度
      </button>
      <button
        type="button"
        class="tab"
        :class="{ active: activeTab === 'code-patches' }"
        data-test="tab-code-patches"
        @click="showCodePatches"
      >
        代码补丁
      </button>
    </nav>

    <div v-if="activeTab === 'observations'" class="tab-panel">
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
          <button type="button" :disabled="page <= 1" @click="changePage(page - 1)">上一页</button>
          <span>第 {{ page }} / {{ totalPages }} 页 · 共 {{ observationsTotal }} 条</span>
          <button type="button" :disabled="page >= totalPages" @click="changePage(page + 1)">下一页</button>
        </div>
      </div>
    </div>

    <div v-else-if="activeTab === 'clusters'" class="tab-panel">
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
              :disabled="deciding"
              @click="decide('approve_for_candidate')"
            >
              确认进入候选
            </button>
            <button
              type="button"
              class="decision-button"
              data-test="decision-reject"
              :disabled="deciding"
              @click="decide('reject')"
            >
              拒绝
            </button>
            <button
              type="button"
              class="decision-button"
              data-test="decision-needs-more-evidence"
              :disabled="deciding"
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

    <div v-else-if="activeTab === 'metrics'" class="tab-panel">
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

    <div v-else-if="activeTab === 'candidates'" class="tab-panel">
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

    <div v-else-if="activeTab === 'code-patches'" class="tab-panel">
      <div class="table-card">
        <h3 class="panel-title">代码补丁候选（层 4 = 管理员审核，批准只导出不应用）</h3>
        <p v-if="codePatchLoading && codePatches.length === 0" class="muted" data-test="code-candidates-loading">
          加载补丁候选中…
        </p>
        <p v-else-if="codePatches.length === 0" class="muted" data-test="code-candidates-empty">
          暂无补丁：只有经管理员确认根因的失败聚类才会按层级生成代码补丁，越界路径与限额在层 1 即被拒绝。
        </p>
        <table v-else data-test="code-candidate-list">
          <thead>
            <tr>
              <th>ID</th><th>层级</th><th>状态</th><th>置信度</th><th>文件数</th>
              <th>基线提交</th><th>沙箱</th><th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="patch in codePatches"
              :key="patch.id"
              data-test="code-candidate-row"
              :class="{ 'candidate-row-active': selectedCodePatchId === patch.id }"
              @click="openCodePatch(patch)"
            >
              <td>{{ patch.id }}</td>
              <td>
                {{ layerLabel(patch.layer) }}<code>（{{ patch.layer }}）</code>
              </td>
              <td>
                <span
                  class="badge"
                  :class="{ 'badge-warn': patch.status === 'rejected' || patch.status === 'rolled_back' }"
                >
                  {{ patch.status }}
                </span>
              </td>
              <td>{{ patch.confidence }}</td>
              <td data-test="code-candidate-row-files">{{ patch.paths.length }}</td>
              <td><code>{{ shortCommit(patch.rollback_ref.base_commit || '') || '—' }}</code></td>
              <td>
                <span
                  v-for="(entry, index) in patch.sandbox_summary"
                  :key="index"
                  class="badge"
                  :class="{ 'badge-warn': entry.result !== 'passed' && entry.result !== 'accepted' }"
                >
                  {{ gateResultLabel(entry.result) }}
                </span>
                <span v-if="patch.sandbox_summary.length === 0" class="cluster-meta">未验证</span>
              </td>
              <td>{{ formatTime(patch.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="selectedCodePatch" class="candidate-detail" data-test="code-candidate-detail">
        <h3 class="panel-title">
          补丁 #{{ selectedCodePatch.id }}（{{ layerLabel(selectedCodePatch.layer) }}）
        </h3>
        <p v-if="codePatchLoading" class="muted">加载补丁…</p>
        <template v-else>
          <p class="cluster-meta">
            {{ selectedCodePatch.status }} · 置信度 {{ selectedCodePatch.confidence }} ·
            聚类 #{{ selectedCodePatch.cluster_id }}{{ selectedCodePatch.observation_id
              ? ` · 观测 #${selectedCodePatch.observation_id}` : '' }} ·
            提示词 {{ selectedCodePatch.prompt_version || '—' }} / 模型 {{ selectedCodePatch.model || '—' }}
          </p>
          <dl class="draft-grid">
            <dt>生成理由</dt>
            <dd data-test="code-candidate-rationale">{{ selectedCodePatch.rationale }}</dd>
            <dt>预期效果</dt>
            <dd data-test="code-candidate-expected-effect">{{ selectedCodePatch.expected_effect }}</dd>
            <dt>回滚引用</dt>
            <dd data-test="code-candidate-rollback-ref">
              base_commit={{ selectedCodePatch.rollback_ref.base_commit || '—' }}
            </dd>
          </dl>

          <h4 class="panel-subtitle">文件清单（层 1 边界内，只读）</h4>
          <ul class="patch-files" data-test="code-candidate-files">
            <li v-for="path in selectedCodePatch.paths" :key="path">
              <code>{{ path }}</code>
            </li>
          </ul>
          <p class="cluster-meta" data-test="code-candidate-test-paths">
            验证目标：<code v-for="path in selectedCodePatch.test_paths" :key="path">{{ path }}</code>
          </p>

          <h4 class="panel-subtitle">diff 全文（只读；应用由管理员手动执行）</h4>
          <pre class="patch-diff" data-test="patch-diff">{{ selectedCodePatch.diff }}</pre>

          <h4 class="panel-subtitle">沙箱结论（层 2/3：隔离 worktree 内 apply + 目标测试 + 基准回放）</h4>
          <ul
            v-if="selectedCodePatch.sandbox_summary.length > 0"
            class="audit-list"
            data-test="code-candidate-sandbox"
          >
            <li v-for="(entry, index) in selectedCodePatch.sandbox_summary" :key="index">
              <span class="badge" :class="{ 'badge-warn': entry.result !== 'passed' && entry.result !== 'accepted' }">
                {{ gateResultLabel(entry.result) }}
              </span>
              <span class="cluster-meta">{{ entry.detail || '—' }} · {{ formatTime(entry.created_at) }}</span>
            </li>
          </ul>
          <p v-else class="muted">沙箱尚未验证该补丁：未通过层 2/3 的补丁不予审核。</p>

          <h4 class="panel-subtitle">五层门禁轨迹</h4>
          <ul
            v-if="selectedCodePatch.gate_log.length > 0"
            class="audit-list"
            data-test="code-candidate-gate-log"
          >
            <li
              v-for="(entry, index) in selectedCodePatch.gate_log"
              :key="index"
              data-test="code-candidate-gate-entry"
            >
              <span class="audit-action">{{ gateLabel(entry.gate) }}</span>
              <span
                class="badge"
                :class="{ 'badge-warn': entry.result !== 'passed' && entry.result !== 'accepted' }"
              >
                {{ gateResultLabel(entry.result) }}
              </span>
              <span class="cluster-meta">{{ entry.detail || '—' }} · {{ formatTime(entry.created_at) }}</span>
            </li>
          </ul>
          <p v-else class="muted">暂无门禁记录</p>

          <h4 class="panel-subtitle">生命周期审计</h4>
          <ul
            v-if="selectedCodePatch.audit.length > 0"
            class="audit-list"
            data-test="code-candidate-audit"
          >
            <li v-for="row in selectedCodePatch.audit" :key="row.id">
              <span class="audit-action">{{ row.action }}</span>
              <span class="cluster-meta">
                {{ row.actor_id }} · {{ formatTime(row.created_at) }} · {{ row.note || '无备注' }}
              </span>
            </li>
          </ul>
          <p v-else class="muted">暂无审计记录</p>

          <div class="decision-bar">
            <textarea
              v-model="codePatchNote"
              class="decision-note"
              data-test="code-candidate-note"
              rows="2"
              placeholder="备注：批准依据、驳回理由或还缺的证据（写入审计）"
              :disabled="codePatchActing"
            ></textarea>
            <div class="decision-buttons">
              <button
                type="button"
                class="query-button"
                data-test="code-candidate-approve"
                :disabled="codePatchActing"
                @click="decideCodePatch('approve_for_apply')"
              >
                批准并导出
              </button>
              <button
                type="button"
                class="decision-button"
                data-test="code-candidate-reject"
                :disabled="codePatchActing"
                @click="decideCodePatch('reject')"
              >
                拒绝
              </button>
              <button
                type="button"
                class="decision-button"
                data-test="code-candidate-needs-revision"
                :disabled="codePatchActing"
                @click="decideCodePatch('needs_revision')"
              >
                需修订
              </button>
            </div>
          </div>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.evolution {
  height: 100%;
  padding: var(--space-6);
  overflow: auto;
  box-sizing: border-box;
  background: var(--color-background);
}

.page-header {
  display: flex;
  justify-content: space-between;
  gap: var(--space-6);
  align-items: flex-end;
  margin-bottom: var(--space-5);
}

.eyebrow {
  display: block;
  margin-bottom: var(--space-1);
  color: var(--color-primary);
  font-size: 10px;
  font-weight: var(--font-bold);
  letter-spacing: .1em;
  text-transform: uppercase;
}

.page-header h2 { margin: 0; color: var(--color-foreground); font-family: var(--font-heading); font-size: var(--text-2xl); }
.page-header p { margin: var(--space-1) 0 0; color: var(--color-secondary); font-size: var(--text-sm); }

.filters { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--space-2); }
.filters select, .filters input {
  min-height: 38px;
  padding: 0 var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface);
  font: inherit;
  font-size: var(--text-sm);
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

.tabs { display: flex; gap: var(--space-2); margin-bottom: var(--space-4); }
.tab {
  padding: var(--space-2) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-secondary);
  background: var(--color-surface);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.tab.active { color: var(--color-primary); border-color: var(--color-primary); font-weight: var(--font-medium); }

.tab-panel { display: grid; gap: var(--space-5); }

.table-card, .pool-group, .cluster-detail, .state-machine {
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

.panel-title { margin: 0 0 var(--space-3); font-size: var(--text-md); font-weight: var(--font-semibold); color: var(--color-foreground); }
.panel-subtitle { margin: var(--space-4) 0 var(--space-2); font-size: var(--text-sm); font-weight: var(--font-semibold); color: var(--color-secondary); }

.muted, .empty-state { color: var(--color-secondary); font-size: var(--text-sm); }
.empty-state { min-height: 200px; display: grid; place-content: center; gap: var(--space-2); text-align: center; border: 1px dashed var(--color-border); border-radius: var(--radius-lg); background: var(--color-surface); }
.empty-state strong { color: var(--color-foreground); font-size: var(--text-base); }

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

.state-machine ul { margin: 0; padding-left: var(--space-5); color: var(--color-foreground); font-size: var(--text-sm); }
.state-machine li { margin-bottom: var(--space-1); }
.state-machine code, .endpoints code { font-family: ui-monospace, monospace; font-size: var(--text-xs); }
.note { margin: 0 0 var(--space-3); color: var(--color-secondary); font-size: var(--text-sm); }
.endpoints { list-style: none; padding-left: 0; }
.method { display: inline-block; min-width: 44px; margin-right: var(--space-2); color: var(--color-primary); font-size: var(--text-xs); font-weight: var(--font-semibold); }

.candidate-detail { padding: var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: var(--color-surface); box-shadow: var(--shadow-xs); }
.candidate-detail .draft-grid { margin-bottom: var(--space-4); }
.candidate-row-active { background: #f8fbff; }
.candidate-actions { display: grid; gap: var(--space-2); }
.candidate-actions label { color: var(--color-secondary); font-size: var(--text-xs); }
.candidate-actions .decision-note { max-width: 160px; }

/* The patch body is the file content under review: monospace, read-only, and the
   only place on the page a diff is ever rendered. */
.patch-files { list-style: none; margin: 0; padding-left: 0; }
.patch-files li { padding: var(--space-1) 0; font-size: var(--text-sm); }
.patch-files code { font-family: ui-monospace, monospace; font-size: var(--text-xs); }
.patch-diff {
  max-height: 420px;
  margin: 0;
  padding: var(--space-3);
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface-2);
  font-family: ui-monospace, monospace;
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre;
  user-select: text;
}

.error-banner {
  margin-bottom: var(--space-4);
  padding: var(--space-3) var(--space-4);
  border: 1px solid #fecaca;
  border-radius: var(--radius);
  color: var(--color-destructive);
  background: var(--color-destructive-light);
  font-size: var(--text-sm);
}

@media (max-width: 900px) {
  .page-header { align-items: flex-start; flex-direction: column; }
  .filters { justify-content: flex-start; }
  .table-card { overflow: auto; }
}
</style>
