import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/evolution', () => ({
  fetchEvolutionObservations: vi.fn(),
  fetchEvolutionClusters: vi.fn(),
  fetchEvolutionCluster: vi.fn(),
  fetchEvolutionStateMachine: vi.fn(),
  fetchGapTickets: vi.fn(),
  fetchCodePatches: vi.fn(),
  fetchCodePatch: vi.fn()
}))

import {
  fetchCodePatch,
  fetchCodePatches,
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  fetchEvolutionObservations,
  fetchEvolutionStateMachine,
  fetchGapTickets
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
  vi.mocked(fetchGapTickets).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
  vi.mocked(fetchCodePatches).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
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

  it('shows gap tickets with their samples and missing terms, and never as candidates', async () => {
    vi.mocked(fetchGapTickets).mockResolvedValue({
      items: [
        {
          id: 4,
          cluster_id: 9,
          kb_id: 'kb_accessories',
          status: 'open',
          occurrences: 3,
          created_at: 1_700_000_000,
          updated_at: 1_700_000_100,
          question_samples: ['R50 电池卖多少钱'],
          missing_terms: ['电池', '价格'],
          rationale: '3 次提问全部因证据不足被拒',
          recheck: null
        }
      ],
      total: 1,
      page: 1,
      page_size: 20
    })

    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="tab-gap-tickets"]').trigger('click')
    await flushPromises()

    const row = wrapper.get('[data-test="gap-ticket-row"]')
    expect(row.text()).toContain('kb_accessories')
    expect(row.text()).toContain('R50 电池卖多少钱')
    expect(row.text()).toContain('电池')
    expect(row.text()).toContain('未复测')
    // 恒常渲染的说明（空态那句只在没工单时出现）
    expect(wrapper.text()).toContain('自动起草')
    expect(wrapper.text()).toContain('材料入库上传补充资料')
  })

  it('offers the apply and revert commands as copy-only, and says the patch is not exported yet', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    vi.mocked(fetchCodePatches).mockResolvedValue({
      items: [
        {
          id: 12,
          observation_id: null,
          cluster_id: 9,
          artifact_type: 'code_patch',
          layer: 'code',
          paths: ['rag/pipeline.py'],
          test_paths: ['tests/test_pipeline.py'],
          status: 'awaiting_verification',
          pool: 'code_pool',
          confidence: 'low',
          prompt_version: 'patch-v3',
          model: 'scripted',
          rationale: '理由',
          expected_effect: '效果',
          rollback_ref: { base_commit: 'a'.repeat(40) },
          sandbox_summary: [],
          created_at: 1_700_000_000,
          updated_at: 1_700_000_000
        }
      ],
      total: 1,
      page: 1,
      page_size: 20
    })
    vi.mocked(fetchCodePatch).mockResolvedValue({
      id: 12,
      observation_id: null,
      cluster_id: 9,
      artifact_type: 'code_patch',
      layer: 'code',
      paths: ['rag/pipeline.py'],
      test_paths: ['tests/test_pipeline.py'],
      status: 'awaiting_verification',
      pool: 'code_pool',
      confidence: 'low',
      prompt_version: 'patch-v3',
      model: 'scripted',
      rationale: '理由',
      expected_effect: '效果',
      rollback_ref: { base_commit: 'a'.repeat(40) },
      sandbox_summary: [],
      created_at: 1_700_000_000,
      updated_at: 1_700_000_000,
      diff: 'diff --git a/rag/pipeline.py b/rag/pipeline.py',
      gate_log: [],
      audit: [],
      apply_command: 'git -C /repo apply --3way artifacts/patches/candidate-12.patch',
      revert_command: 'git -C /repo apply -R artifacts/patches/candidate-12.patch',
      export_path: '/repo/artifacts/patches/candidate-12.patch',
      exported: false
    })

    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="tab-code-patches"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="code-candidate-row"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-test="apply-command"]').text()).toContain('apply --3way')
    expect(wrapper.get('[data-test="revert-command"]').text()).toContain('apply -R')
    expect(wrapper.get('[data-test="export-state"]').text()).toContain('尚未导出')

    await wrapper.get('[data-test="copy-apply-command"]').trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith(
      'git -C /repo apply --3way artifacts/patches/candidate-12.patch'
    )
    expect(wrapper.get('[data-test="copy-hint"]').text()).toContain('已复制')
  })
})
