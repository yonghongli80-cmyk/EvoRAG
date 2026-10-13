<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  closeOutMergedCodePatch,
  fetchCandidate,
  fetchCandidates,
  applyCodePatch,
  fetchCodePatch,
  fetchCodePatches,
  fetchDiagnosis,
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  fetchEvolutionMetrics,
  fetchEvolutionObservations,
  fetchEvolutionStateMachine,
  fetchGapTickets,
  regenerateCodePatch,
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
  EvolutionStateMachine,
  GapTicket
} from '../api/evolution'

type Tab =
  | 'observations'
  | 'clusters'
  | 'metrics'
  | 'candidates'
  | 'code-patches'
  | 'gap-tickets'

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
/** 决策成功后的可见提示：成功时界面此前**什么都不显示**，看起来像"点了没用"。 */
const decisionHint = ref('')
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

const confirmLayerValue = ref('')

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
    error.value = ''
    decisionHint.value = '已确认根因层级'
    await loadDiagnosis(updated.id)
  } catch (err: any) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = err?.response?.data?.detail || '根因层级确认失败'
  } finally {
    deciding.value = false
  }
}

/** 只有"待审"状态的聚类才接受诊断决策（与后端转移表一致，避免点了必然 409）。 */
const clusterDecidable = computed(() => selectedCluster.value?.status === 'awaiting_diagnosis_review')

/** 已批准的补丁可以一键应用（后端会先跑前置检查，失败就回滚且不重启）。 */
async function runApply() {
  if (!selectedCodePatch.value || codePatchActing.value) return
  codePatchActing.value = true
  try {
    const result = await applyCodePatch(selectedCodePatch.value.id, true)
    const steps = result.steps || {}
    decisionHint.value =
      `已应用（${result.applied_now ? '本次应用' : '此前已应用'}）` +
      `，导入冒烟 ${steps.import_smoke ? '通过' : '未通过'}` +
      `，目标测试 ${steps.target_tests ? '通过' : '未通过'}` +
      `${steps.restart_requested ? '，重启已排队（助手会等端口释放、拉起服务并轮询 /health，起不来会自动回滚）' : ''}`
    error.value = ''
    await reloadCodePatch()
    await loadCodePatches()
  } catch (err: any) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = err?.response?.data?.detail || '应用失败'
  } finally {
    codePatchActing.value = false
  }
}

/** 只有"待验证"的补丁才接受审核决策（同上）。 */
const codePatchDecidable = computed(
  () => selectedCodePatch.value?.status === 'awaiting_verification'
)

/**
 * "已合入结案"只对**已经走到上线那一步**的补丁有意义（approved_for_canary / canary）。
 *
 * 它处理的是 2026-10-12 演练发现的那类陈旧行：改动早就随提交落地，候选行却停在
 * approved_for_canary，作为"未决补丁"一直占着聚类的准入名额。
 */
const codePatchClosable = computed(() =>
  ['approved_for_canary', 'canary'].includes(selectedCodePatch.value?.status || '')
)

async function runCloseOutMerged() {
  if (!selectedCodePatch.value || codePatchActing.value) return
  codePatchActing.value = true
  try {
    const updated = await closeOutMergedCodePatch(selectedCodePatch.value.id, codePatchNote.value)
    selectedCodePatch.value = updated
    decisionHint.value = `已结案为 ${updated.status}（改动已随提交落地）`
    error.value = ''
    codePatchNote.value = ''
    await loadCodePatches()
  } catch (err: any) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = err?.response?.data?.detail || '结案失败'
  } finally {
    codePatchActing.value = false
  }
}

/**
 * 回滚后从同一聚类重新生成一条候选。
 *
 * 回滚把状态写成终态 `rolled_back`，接口层面无法再应用；这条路径产出一条**新**候选回到
 * `candidate_testing`，重新走沙箱 → 审核 → 批准 → 应用，因此它是"再试一次"的正道，
 * 而不是绕过状态机的复活。
 */
const codePatchRegenerable = computed(
  () => selectedCodePatch.value?.status === 'rolled_back'
)

async function runRegenerate() {
  if (!selectedCodePatch.value || codePatchActing.value) return
  const source = selectedCodePatch.value
  codePatchActing.value = true
  try {
    const created = await regenerateCodePatch(source.id, codePatchNote.value)
    decisionHint.value =
      `已从同一聚类重新生成候选 #${created.id}（状态 ${created.status}）` +
      `：它需要重新走沙箱与审核，批准后才会进入应用`
    error.value = ''
    codePatchNote.value = ''
    await loadCodePatches()
    await openCodePatch(created)
  } catch (err: any) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = err?.response?.data?.detail || '重新生成失败'
  } finally {
    codePatchActing.value = false
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
  rollback: '层 5 回滚',
  sandbox: '层 2/3 沙箱（隔离 worktree）',
  host_ab: '层 2 宿主 A/B 对照'
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
    decisionHint.value = `已提交：${action}`
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

// ---- 内容缺口工单（队列 5 的第三种制品：请求补内容，不是候选）----
const gapTickets = ref<GapTicket[]>([])
const gapTicketsLoaded = ref(false)
const copyHint = ref('')

async function loadGapTickets() {
  try {
    const data = await fetchGapTickets({ pageSize: 100 })
    gapTickets.value = data.items
    gapTicketsLoaded.value = true
    error.value = ''
  } catch (err) {
    if (isForbidden(err)) {
      forbidden.value = true
      return
    }
    error.value = '无法加载内容缺口工单'
  }
}

/**
 * 最近一次带结构化指标的**沙箱**条目（没有就返回 null，让面板如实说不显示对照）。
 *
 * 只看 `sandbox_summary`，不看整条 gate_log：宿主 A/B 的条目的 `metrics.verdict` 形状与沙箱
 * 那份相同，若在 gate_log 里按"有 verdict"找，会把宿主 A/B 的数字当成沙箱的结论画出来。
 */
const sandboxMetrics = computed(() => {
  const entries = selectedCodePatch.value?.sandbox_summary || []
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const metrics = entries[index].metrics
    if (metrics?.verdict) return { entry: entries[index], verdict: metrics.verdict }
  }
  return null
})

const sandboxMetricsUnavailable = computed(() => {
  const entries = selectedCodePatch.value?.sandbox_summary || []
  return entries.some((entry) => entry.metrics?.verdict_unavailable)
})

/** 最近一次带对照数字的宿主 A/B 条目（层 2 的另一个生产者，不动候选状态）。 */
const hostAbMetrics = computed(() => {
  const entries = selectedCodePatch.value?.host_ab_summary || []
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const metrics = entries[index].metrics
    if (metrics?.verdict) return { entry: entries[index], verdict: metrics.verdict, metrics }
  }
  return null
})

const hostAbUnavailable = computed(() => {
  const entries = selectedCodePatch.value?.host_ab_summary || []
  return entries.some((entry) => entry.metrics?.verdict_unavailable)
})

function formatDelta(value: number): string {
  return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1)
}

function showGapTickets() {
  activeTab.value = 'gap-tickets'
  if (!gapTicketsLoaded.value) void loadGapTickets()
}

/** 只读地给出命令供复制：这个界面从不执行 apply / revert（那是管理员在终端做的事）。 */
async function copyCommand(text: string, what: string) {
  try {
    await navigator.clipboard.writeText(text)
    copyHint.value = `已复制${what}`
  } catch {
    copyHint.value = `复制失败，请手动选中${what}`
  }
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
      <button
        type="button"
        class="tab"
        :class="{ active: activeTab === 'gap-tickets' }"
        data-test="tab-gap-tickets"
        @click="showGapTickets"
      >
        内容缺口工单
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
              <th>ID</th><th>层级</th><th>状态</th><th>置信度</th><th>文件数</th><th>同类</th>
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
              <td class="cluster-meta" data-test="code-candidate-overlap">
                <span v-if="patch.overlap_ids && patch.overlap_ids.length" class="badge badge-warn">
                  同类 { patch.overlap_ids.length } 条
                </span>
                <span v-else>—</span>
              </td>

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

          <p
            v-if="selectedCodePatch.overlap_ids && selectedCodePatch.overlap_ids.length"
            class="cluster-meta"
            data-test="code-candidate-overlap-note"
          >
            与
            <code v-for="other in selectedCodePatch.overlap_ids" :key="other" class="term-chip">
              #{{ other }}
            </code>
            改的是同一个函数（{{ selectedCodePatch.overlap_key }}）——它们是同一条改动的变体，
            建议只留一条：并排应用两条会留下两套实现。
          </p>
          <h4 class="panel-subtitle">应用与回滚命令（只复制，不执行）</h4>
          <p class="cluster-meta" data-test="export-state">
            {{
              selectedCodePatch.exported
                ? `已导出：${selectedCodePatch.export_path}`
                : '尚未导出：先“批准并导出”才会写出补丁文件，命令届时才可用'
            }}
          </p>
          <div class="command-row">
            <code class="command-text" data-test="apply-command">{{
              selectedCodePatch.apply_command
            }}</code>
            <button
              type="button"
              class="query-button"
              data-test="copy-apply-command"
              @click="copyCommand(selectedCodePatch.apply_command, '应用命令')"
            >
              复制应用命令
            </button>
          </div>
          <div class="command-row">
            <code class="command-text" data-test="revert-command">{{
              selectedCodePatch.revert_command
            }}</code>
            <button
              type="button"
              class="query-button"
              data-test="copy-revert-command"
              @click="copyCommand(selectedCodePatch.revert_command, '回滚命令')"
            >
              复制回滚命令
            </button>
          </div>
          <p v-if="copyHint" class="cluster-meta" data-test="copy-hint">{{ copyHint }}</p>

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
          <div
            v-if="sandboxMetrics"
            class="ab-table"
            data-test="sandbox-metrics"
          >
            <p class="cluster-meta">
              对照：{{ sandboxMetrics.verdict.passed ? '通过' : '未通过' }} ·
              route {{ formatDelta(sandboxMetrics.verdict.route_delta_pp) }}pp ·
              top-5 {{ formatDelta(sandboxMetrics.verdict.top5_delta_pp) }}pp ·
              失败调用 {{ formatDelta(sandboxMetrics.verdict.failed_calls_delta) }}
            </p>
            <table v-for="(side, name) in sandboxMetrics.verdict.sides" :key="name">
              <thead>
                <tr>
                  <th>{{ name }}</th><th>route 准确率</th><th>Top-5 命中</th>
                  <th>失败调用</th><th>预处理 P50</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>干净侧</td>
                  <td>{{ formatRate(side.baseline.route_accuracy) }}</td>
                  <td>{{ formatRate(side.baseline.top5_hit_rate) }}</td>
                  <td>{{ side.baseline.failed_calls }}</td>
                  <td>{{ formatLatency(side.baseline.prep_p50_ms) }}</td>
                </tr>
                <tr>
                  <td>打补丁侧</td>
                  <td>{{ formatRate(side.patched.route_accuracy) }}</td>
                  <td>{{ formatRate(side.patched.top5_hit_rate) }}</td>
                  <td>{{ side.patched.failed_calls }}</td>
                  <td>{{ formatLatency(side.patched.prep_p50_ms) }}</td>
                </tr>
              </tbody>
            </table>
            <p class="cluster-meta">
              注：P50 在回放模式下取自录制（两侧相减时抵消，绝对值不是生产时延）。
            </p>
          </div>
          <p
            v-else-if="sandboxMetricsUnavailable"
            class="cluster-meta"
            data-test="sandbox-metrics-unavailable"
          >
            这次沙箱运行没有可比的对照（回放不可用或未产出报告）——"没有数字"不等于"没有变差"。
          </p>

          <h4 class="panel-subtitle">宿主 A/B 对照（层 2 的真实回归：干净树 vs 打过补丁的树）</h4>
          <div v-if="hostAbMetrics" class="ab-table" data-test="host-ab-metrics">
            <p class="cluster-meta">
              对照：{{ hostAbMetrics.verdict.passed ? '通过' : '未通过' }} ·
              route {{ formatDelta(hostAbMetrics.verdict.route_delta_pp) }}pp ·
              top-5 {{ formatDelta(hostAbMetrics.verdict.top5_delta_pp) }}pp ·
              失败调用 {{ formatDelta(hostAbMetrics.verdict.failed_calls_delta) }} ·
              每侧 {{ hostAbMetrics.metrics.runs ?? '—' }} 次（kb={{ hostAbMetrics.metrics.kb_id || '—' }}）
            </p>
            <table v-for="(side, name) in hostAbMetrics.verdict.sides" :key="name">
              <thead>
                <tr>
                  <th>{{ name }}</th><th>route 准确率</th><th>Top-5 命中</th>
                  <th>失败调用</th><th>预处理 P50</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>干净侧</td>
                  <td>{{ formatRate(side.baseline.route_accuracy) }}</td>
                  <td>{{ formatRate(side.baseline.top5_hit_rate) }}</td>
                  <td>{{ side.baseline.failed_calls }}</td>
                  <td>{{ formatLatency(side.baseline.prep_p50_ms) }}</td>
                </tr>
                <tr>
                  <td>打补丁侧</td>
                  <td>{{ formatRate(side.patched.route_accuracy) }}</td>
                  <td>{{ formatRate(side.patched.top5_hit_rate) }}</td>
                  <td>{{ side.patched.failed_calls }}</td>
                  <td>{{ formatLatency(side.patched.prep_p50_ms) }}</td>
                </tr>
              </tbody>
            </table>
            <p class="cluster-meta">
              这是管理员在**宿主**上跑出来的对照（沙箱无凭据，量不了回归）；它只作为审核证据，
              从不自动改候选状态。
            </p>
          </div>
          <p
            v-else-if="hostAbUnavailable"
            class="cluster-meta"
            data-test="host-ab-unavailable"
          >
            最近一次宿主 A/B 没有产出可比的对照——"没有数字"不等于"没有变差"。
          </p>
          <p v-else class="cluster-meta" data-test="host-ab-empty">
            还没跑过宿主 A/B：层 2 的真实对照发生在应用之后的宿主上
            （<code>scripts/measure_patch_regression_ab.py</code>），跑完会回流到这条轨迹。
          </p>

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
          <div class="apply-bar" data-test="code-candidate-apply-bar">


          <button


            type="button"


            class="query-button"


            data-test="code-candidate-apply"


            :disabled="codePatchActing || selectedCodePatch?.status !== 'approved_for_canary'"


            @click="runApply"


          >


            应用并重启（先跑前置检查；失败会回滚且不重启）


          </button>


          <span class="cluster-meta">


            只有「已批准（approved_for_canary）」的补丁可用；已应用的补丁再点即为"校验 + 重启"。


          </span>


          <button


            type="button"


            class="query-button"


            data-test="code-candidate-regenerate"


            :disabled="codePatchActing || !codePatchRegenerable"


            @click="runRegenerate"


          >


            回滚后重新生成（同一聚类产出一条新候选，重新走沙箱与审核）


          </button>


          <span class="cluster-meta">


            回滚是终态：这条路径不回滚状态机，而是让同一聚类的**新**候选重新走一遍五层门禁。


          </span>


          </div>


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
                :disabled="codePatchActing || !codePatchDecidable"
                @click="decideCodePatch('approve_for_apply')"
              >
                批准并导出
              </button>
              <button
                type="button"
                class="decision-button"
                data-test="code-candidate-reject"
                :disabled="codePatchActing || !codePatchDecidable"
                @click="decideCodePatch('reject')"
              >
                拒绝
              </button>
              <button
                type="button"
                class="decision-button"
                data-test="code-candidate-needs-revision"
                :disabled="codePatchActing || !codePatchDecidable"
                @click="decideCodePatch('needs_revision')"
              >
                需修订
              </button>
              <button
                type="button"
                class="decision-button"
                data-test="code-candidate-close-merged"
                :disabled="codePatchActing || !codePatchClosable"
                @click="runCloseOutMerged"
              >
                已合入：结案
              </button>
            </div>
            <span class="cluster-meta">
              「结案」用于**人已经把改动合进提交**的情形（不经 apply）；后端会核对改动确实在代码里、
              且那些文件相对 HEAD 干净，否则拒绝。
            </span>
          </div>
        </template>
      </div>
    </div>
      <div v-else-if="activeTab === 'gap-tickets'" class="tab-panel">
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

.command-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 6px 0;
}

.command-text {
  flex: 1;
  overflow-x: auto;
  white-space: nowrap;
  padding: 4px 6px;
  background: rgba(0, 0, 0, 0.04);
  border-radius: 4px;
}

.term-chip {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 1px 6px;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 10px;
}

.ab-table {
  margin: 8px 0 12px;
}

.ab-table table {
  width: 100%;
  margin-top: 4px;
}
</style>
