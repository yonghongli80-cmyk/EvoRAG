<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  fetchEvolutionObservations,
  fetchEvolutionStateMachine
} from '../api/evolution'
import type {
  EvolutionObservation,
  EvolutionStateMachine
} from '../api/evolution'
import { isForbidden } from './evolution/helpers'
import ObservationsTab from './evolution/ObservationsTab.vue'
import ClustersTab from './evolution/ClustersTab.vue'
import MetricsTab from './evolution/MetricsTab.vue'
import CandidatesTab from './evolution/CandidatesTab.vue'
import CodePatchesTab from './evolution/CodePatchesTab.vue'
import GapTicketsTab from './evolution/GapTicketsTab.vue'

type Tab =
  | 'observations'
  | 'clusters'
  | 'metrics'
  | 'candidates'
  | 'code-patches'
  | 'gap-tickets'

const PAGE_SIZE = 20

const activeTab = ref<Tab>('observations')
const observations = ref<EvolutionObservation[]>([])
const observationsTotal = ref(0)
const page = ref(1)
const statusFilter = ref('')
const kbFilter = ref('')
const stateMachine = ref<EvolutionStateMachine | null>(null)
const loading = ref(false)
const error = ref('')
const forbidden = ref(false)

const totalPages = computed(() => Math.max(1, Math.ceil(observationsTotal.value / PAGE_SIZE)))
const statusOptions = computed(() => stateMachine.value?.statuses ?? [])

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

async function loadStateMachine() {
  try {
    stateMachine.value = await fetchEvolutionStateMachine()
  } catch (err) {
    if (isForbidden(err)) forbidden.value = true
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

function onError(msg: string) {
  error.value = msg
}

function onForbidden() {
  forbidden.value = true
}

function clearError() {
  error.value = ''
}

function showClusters() {
  activeTab.value = 'clusters'
}

function showMetrics() {
  activeTab.value = 'metrics'
}

function showCandidates() {
  activeTab.value = 'candidates'
}

function showCodePatches() {
  activeTab.value = 'code-patches'
}

function showGapTickets() {
  activeTab.value = 'gap-tickets'
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
        @click="showMetrics"
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

    <ObservationsTab
      v-if="activeTab === 'observations'"
      :observations="observations"
      :loading="loading"
      :page="page"
      :total-pages="totalPages"
      :observations-total="observationsTotal"
      @change-page="changePage"
    />

    <ClustersTab
      v-else-if="activeTab === 'clusters'"
      :state-machine="stateMachine"
      @error="onError"
      @forbidden="onForbidden"
      @clear-error="clearError"
    />

    <MetricsTab
      v-else-if="activeTab === 'metrics'"
      :forbidden="forbidden"
      @error="onError"
      @forbidden="onForbidden"
      @clear-error="clearError"
    />

    <CandidatesTab
      v-else-if="activeTab === 'candidates'"
      @error="onError"
      @forbidden="onForbidden"
      @clear-error="clearError"
    />

    <CodePatchesTab
      v-else-if="activeTab === 'code-patches'"
      @error="onError"
      @forbidden="onForbidden"
      @clear-error="clearError"
    />

    <GapTicketsTab
      v-else-if="activeTab === 'gap-tickets'"
      @error="onError"
      @forbidden="onForbidden"
      @clear-error="clearError"
    />
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
}
</style>
