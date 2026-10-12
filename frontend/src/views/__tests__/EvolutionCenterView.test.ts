import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/evolution', () => ({
  fetchEvolutionObservations: vi.fn(),
  fetchEvolutionClusters: vi.fn(),
  fetchEvolutionCluster: vi.fn(),
  fetchEvolutionStateMachine: vi.fn(),
  fetchGapTickets: vi.fn(),
  fetchCodePatches: vi.fn(),
  fetchCodePatch: vi.fn(),
  regenerateCodePatch: vi.fn(),
  closeOutMergedCodePatch: vi.fn(),
}))

import {
  closeOutMergedCodePatch,
  fetchCodePatch,
  fetchCodePatches,
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  fetchEvolutionObservations,
  fetchEvolutionStateMachine,
  fetchGapTickets,
  regenerateCodePatch
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
  confirmable_layers: ['prompt', 'query_rewrite', 'reranking', 'code', 'none'],
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
    // 完整理由与"该写什么"都在行里（列表接口已带这些字段，不需要详情端点）
    expect(wrapper.get('[data-test="gap-ticket-rationale"]').text()).toContain('证据不足')
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
      gate_log: [
        {
          gate: 'sandbox',
          result: 'passed',
          detail: 'sandbox passed; Jev side route delta +0.0pp, top-5 delta +0.0pp over 20 cases',
          created_at: 1_700_000_000,
          metrics: {
            applied: true,
            tests_passed: true,
            failed_tests: [],
            verdict: {
              sides: {
                jev: {
                  baseline: {
                    route_accuracy: 1,
                    top5_hit_rate: 0.98,
                    failed_calls: 0,
                    prep_p50_ms: 900
                  },
                  patched: {
                    route_accuracy: 1,
                    top5_hit_rate: 0.98,
                    failed_calls: 0,
                    prep_p50_ms: 850
                  }
                }
              },
              route_delta_pp: 0,
              top5_delta_pp: 0,
              failed_calls_delta: 0,
              passed: true
            }
          }
        }
      ],
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

    // 结构化 A/B：两侧数字并排，而不是只有一句结论文本
    const metrics = wrapper.get('[data-test="sandbox-metrics"]')
    expect(metrics.text()).toContain('干净侧')
    expect(metrics.text()).toContain('打补丁侧')
    expect(metrics.text()).toContain('98.0%')
    expect(metrics.text()).toContain('850')

    await wrapper.get('[data-test="copy-apply-command"]').trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith(
      'git -C /repo apply --3way artifacts/patches/candidate-12.patch'
    )
    expect(wrapper.get('[data-test="copy-hint"]').text()).toContain('已复制')
  })

  it('offers regeneration only on a rolled-back patch, and opens the new candidate', async () => {
    const rolledBackPatch = {
      id: 26,
      observation_id: 135,
      cluster_id: 30,
      artifact_type: 'code_patch',
      layer: 'code',
      paths: ['rag/prompt_manager.py'],
      test_paths: ['tests/test_prompt_manager.py'],
      status: 'rolled_back',
      pool: 'candidate_pool',
      confidence: 'low',
      prompt_version: 'patch-v5',
      model: 'scripted',
      rationale: '理由',
      expected_effect: '效果',
      rollback_ref: { base_commit: 'b'.repeat(40) },
      sandbox_summary: [],
      created_at: 1_700_000_000,
      updated_at: 1_700_000_000
    }
    vi.mocked(fetchCodePatches).mockResolvedValue({
      items: [rolledBackPatch], total: 1, page: 1, page_size: 20
    })
    vi.mocked(fetchCodePatch).mockImplementation(async (id: number) => ({
      ...rolledBackPatch,
      id,
      // 重新生成出来的那条是 candidate_testing；详情端点读的就是它
      status: id === 31 ? 'candidate_testing' : 'rolled_back',
      diff: 'diff --git a/rag/prompt_manager.py b/rag/prompt_manager.py',
      gate_log: [],
      audit: [],
      apply_command: `git -C /repo apply artifacts/patches/candidate-${id}.patch`,
      revert_command: `git -C /repo apply -R artifacts/patches/candidate-${id}.patch`,
      export_path: `/repo/artifacts/patches/candidate-${id}.patch`,
      exported: false
    }))
    vi.mocked(regenerateCodePatch).mockResolvedValue({
      ...rolledBackPatch,
      id: 31,
      observation_id: 135,
      status: 'candidate_testing',
      diff: 'diff --git a/rag/prompt_manager.py b/rag/prompt_manager.py',
      gate_log: [],
      audit: [],
      apply_command: 'git -C /repo apply artifacts/patches/candidate-31.patch',
      revert_command: 'git -C /repo apply -R artifacts/patches/candidate-31.patch',
      export_path: '/repo/artifacts/patches/candidate-31.patch',
      exported: false
    })

    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="tab-code-patches"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="code-candidate-row"]').trigger('click')
    await flushPromises()

    // 回滚是终态：应用按钮不可用，重新生成按钮可用
    expect(
      wrapper.get('[data-test="code-candidate-apply"]').attributes('disabled')
    ).toBeDefined()
    const regenerate = wrapper.get('[data-test="code-candidate-regenerate"]')
    expect(regenerate.attributes('disabled')).toBeUndefined()

    await regenerate.trigger('click')
    await flushPromises()

    expect(regenerateCodePatch).toHaveBeenCalledWith(26, '')
    // 新候选成为当前选中项（管理员接着审这一条），而它又回到"不可重新生成"的状态
    expect(wrapper.get('[data-test="code-candidate-detail"]').text()).toContain('补丁 #31')
    expect(
      wrapper.get('[data-test="code-candidate-regenerate"]').attributes('disabled')
    ).toBeDefined()
  })

  it('offers "已合入结案" only on an applied patch and shows the settled status', async () => {
    const appliedPatch = {
      id: 10,
      observation_id: null,
      cluster_id: 30,
      artifact_type: 'code_patch',
      layer: 'code',
      paths: ['rag/agent.py'],
      test_paths: ['tests/test_agent.py'],
      status: 'approved_for_canary',
      pool: 'candidate_pool',
      confidence: 'low',
      prompt_version: 'patch-v3',
      model: 'scripted',
      rationale: '理由',
      expected_effect: '效果',
      rollback_ref: { base_commit: 'c'.repeat(40) },
      sandbox_summary: [],
      created_at: 1_700_000_000,
      updated_at: 1_700_000_000
    }
    vi.mocked(fetchCodePatches).mockResolvedValue({
      items: [appliedPatch], total: 1, page: 1, page_size: 20
    })
    vi.mocked(fetchCodePatch).mockResolvedValue({
      ...appliedPatch,
      diff: 'diff --git a/rag/agent.py b/rag/agent.py',
      gate_log: [],
      audit: [],
      apply_command: 'git -C /repo apply artifacts/patches/candidate-10.patch',
      revert_command: 'git -C /repo apply -R artifacts/patches/candidate-10.patch',
      export_path: '/repo/artifacts/patches/candidate-10.patch',
      exported: true
    })
    vi.mocked(closeOutMergedCodePatch).mockResolvedValue({
      ...appliedPatch,
      status: 'stable',
      diff: 'diff --git a/rag/agent.py b/rag/agent.py',
      gate_log: [],
      audit: [],
      apply_command: '',
      revert_command: '',
      export_path: '',
      exported: false
    })

    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="tab-code-patches"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="code-candidate-row"]').trigger('click')
    await flushPromises()

    const closeOut = wrapper.get('[data-test="code-candidate-close-merged"]')
    expect(closeOut.attributes('disabled')).toBeUndefined()

    await closeOut.trigger('click')
    await flushPromises()

    expect(closeOutMergedCodePatch).toHaveBeenCalledWith(10, '')
    // 结案是终态：按钮随之不可用（状态已不是 approved_for_canary / canary）
    expect(
      wrapper.get('[data-test="code-candidate-close-merged"]').attributes('disabled')
    ).toBeDefined()
  })
})
