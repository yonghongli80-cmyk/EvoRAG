import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/evolution', () => ({
  fetchEvolutionObservations: vi.fn(),
  fetchEvolutionClusters: vi.fn(),
  fetchEvolutionCluster: vi.fn(),
  fetchEvolutionStateMachine: vi.fn()
}))

import {
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  fetchEvolutionObservations,
  fetchEvolutionStateMachine
} from '../../api/evolution'
import EvolutionCenterView from '../EvolutionCenterView.vue'

const observation = {
  id: 1,
  cluster_id: 7,
  kb_id: 'kb1',
  failure_stage: 'retrieval',
  root_cause_feature: 'recall_miss',
  status: 'clustered',
  pool: 'observation_queue',
  priority: 'normal',
  route: 'rag',
  chunk_hashes: ['a'.repeat(64)],
  adopted_evidence_hashes: [],
  best_score: 0.11,
  answerability_kind: 'insufficient_evidence',
  confidence_level: null,
  tool_names: [],
  structured_query_kind: null,
  exception_class: null,
  feedback_kind: null,
  jev_question_set_version: 'passages-v1',
  decision_metadata: { question_set_version: 'passages-v1' },
  total_ms: 12.5,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000
}

const candidateCluster = {
  id: 7,
  cluster_key: 'kb1|retrieval|recall_miss',
  kb_id: 'kb1',
  failure_stage: 'retrieval',
  root_cause_feature: 'recall_miss',
  pool: 'candidate_pool',
  status: 'awaiting_diagnosis_review',
  priority: 'high',
  member_count: 3,
  first_seen_at: 1_700_000_000,
  last_seen_at: 1_700_000_100,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_100
}

const stateMachine = {
  statuses: ['observed', 'clustered', 'awaiting_diagnosis_review', 'rejected'],
  pools: ['observation_queue', 'candidate_pool'],
  transitions: {
    observed: ['clustered', 'rejected', 'blocked', 'needs_more_evidence'],
    clustered: ['awaiting_diagnosis_review', 'rejected']
  },
  endpoints: [
    { path: '/knowledge-operations/evolution/observations', methods: ['GET'] }
  ],
  note: '第 2 阶段只读观测'
}

function mountView() {
  return mount(EvolutionCenterView)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(fetchEvolutionStateMachine).mockResolvedValue(stateMachine)
  vi.mocked(fetchEvolutionObservations).mockResolvedValue({
    items: [observation], total: 1, page: 1, page_size: 20
  })
  vi.mocked(fetchEvolutionClusters).mockResolvedValue({
    items: [candidateCluster], total: 1, page: 1, page_size: 20
  })
  vi.mocked(fetchEvolutionCluster).mockResolvedValue({
    ...candidateCluster, members: [observation]
  })
})

describe('EvolutionCenterView', () => {
  it('renders stored observation signals and never the question text', async () => {
    const wrapper = mountView()
    await flushPromises()

    const row = wrapper.get('[data-test="observation-row"]')
    expect(row.text()).toContain('kb1')
    expect(row.text()).toContain('recall_miss')
    expect(row.text()).toContain('passages-v1')
    expect(row.text()).toContain('0.11')
    expect(wrapper.text()).not.toContain('secret')
    expect(wrapper.findAll('[data-test="observation-row"]')).toHaveLength(1)
  })

  it('shows an explicit empty state when nothing was observed', async () => {
    vi.mocked(fetchEvolutionObservations).mockResolvedValue({
      items: [], total: 0, page: 1, page_size: 20
    })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-test="observations-empty"]').text()).toContain('暂无观察')
  })

  it('groups clusters by pool and opens one candidate cluster', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="tab-clusters"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-test="candidate-pool"]').text()).toContain('awaiting_diagnosis_review')
    expect(wrapper.get('[data-test="candidate-pool"]').text()).toContain('3')
    expect(wrapper.get('[data-test="observation-queue"]').text()).toContain('暂无观察队列聚类')

    await wrapper.get('[data-test="cluster-card"]').trigger('click')
    await flushPromises()

    expect(fetchEvolutionCluster).toHaveBeenCalledWith(7)
    const detail = wrapper.get('[data-test="cluster-detail"]')
    expect(detail.text()).toContain('awaiting_diagnosis_review')
    expect(detail.text()).toContain('recall_miss')
  })

  it('renders the state machine legend from the API reference', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="tab-clusters"]').trigger('click')
    await flushPromises()

    const legend = wrapper.get('[data-test="state-machine-legend"]')
    expect(legend.text()).toContain('observed')
    expect(legend.text()).toContain('clustered')
    expect(legend.text()).toContain('needs_more_evidence')
    expect(wrapper.get('[data-test="endpoint-inventory"]').text()).toContain('GET')
  })

  it('offers no state-changing control in phase 2', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.findAll('[data-test="observation-action"]')).toHaveLength(0)
    expect(wrapper.findAll('[data-test="cluster-action"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('只读')
    expect(wrapper.text()).not.toContain('驳回')
  })

  it('explains the administrator boundary when the API rejects a member', async () => {
    vi.mocked(fetchEvolutionObservations).mockRejectedValue({ response: { status: 403 } })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-test="evolution-forbidden"]').text()).toContain('管理员')
  })

  it('reports a load failure without hiding the page', async () => {
    vi.mocked(fetchEvolutionObservations).mockRejectedValue(new Error('network'))
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-test="evolution-error"]').text()).toContain('无法加载')
  })

  it('marks the metrics area as the next phase placeholder', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="tab-metrics"]').trigger('click')

    expect(wrapper.get('[data-test="metrics-placeholder"]').text()).toContain('指标')
  })
})
