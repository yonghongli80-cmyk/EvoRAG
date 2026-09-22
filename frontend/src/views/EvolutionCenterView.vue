<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  fetchEvolutionObservations,
  fetchEvolutionStateMachine
} from '../api/evolution'
import type {
  EvolutionCluster,
  EvolutionEndpoint,
  EvolutionObservation,
  EvolutionStateMachine
} from '../api/evolution'

type Tab = 'observations' | 'clusters' | 'metrics'

const PAGE_SIZE = 20

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

async function openCluster(cluster: EvolutionCluster) {
  try {
    selectedCluster.value = await fetchEvolutionCluster(cluster.id)
  } catch {
    error.value = '无法加载聚类详情'
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
        <p>只读观测失败轨迹、聚类准入与状态机；第 2 阶段不生成候选、不改动在线链路。</p>
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
        @click="activeTab = 'metrics'"
      >
        指标看板
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

      <div class="state-machine" data-test="state-machine-legend">
        <h3 class="panel-title">状态机（设计第 7 节，只读）</h3>
        <p v-if="stateMachine?.note" class="note">{{ stateMachine.note }}</p>
        <ul>
          <li v-for="(targets, status) in transitions" :key="status">
            <code>{{ status }}</code> → <span>{{ targets.join('、') || '终态' }}</span>
          </li>
        </ul>
        <h4 class="panel-subtitle">第 2 阶段只读端点</h4>
        <ul class="endpoints" data-test="endpoint-inventory">
          <li v-for="endpoint in endpoints" :key="endpoint.path">
            <span class="method">{{ endpoint.methods.join('/') }}</span>
            <code>{{ endpoint.path }}</code>
          </li>
        </ul>
      </div>
    </div>

    <div v-else class="tab-panel">
      <div class="empty-state" data-test="metrics-placeholder">
        <strong>指标看板待接入</strong>
        <span>第 2 阶段只累积观测数据，最小指标集（问题解决率、错误答案率、拒答准确率等）将在下一任务随 /metrics 端点一起提供。</span>
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

.state-machine ul { margin: 0; padding-left: var(--space-5); color: var(--color-foreground); font-size: var(--text-sm); }
.state-machine li { margin-bottom: var(--space-1); }
.state-machine code, .endpoints code { font-family: ui-monospace, monospace; font-size: var(--text-xs); }
.note { margin: 0 0 var(--space-3); color: var(--color-secondary); font-size: var(--text-sm); }
.endpoints { list-style: none; padding-left: 0; }
.method { display: inline-block; min-width: 44px; margin-right: var(--space-2); color: var(--color-primary); font-size: var(--text-xs); font-weight: var(--font-semibold); }

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
