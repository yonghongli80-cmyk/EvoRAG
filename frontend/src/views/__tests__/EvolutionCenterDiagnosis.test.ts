import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// Mock the axios instance the API wrappers share: the wrappers under test stay
// real, so these tests also pin the endpoint paths and the request bodies.
vi.mock('../../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))

import api from '../../utils/api'
import EvolutionCenterView from '../EvolutionCenterView.vue'

const DIAGNOSIS_URL = '/knowledge-operations/evolution/clusters/7/diagnosis'
const DECISION_URL = '/knowledge-operations/evolution/clusters/7/decision'

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

// Phase-4 shape: a draft now belongs to one member observation, so the read-out
// carries a legacy cluster-level draft (or null) plus one slot per member.
const memberDraft = {
  id: 21,
  cluster_id: 7,
  prompt_version: 'diagnosis-v1',
  model: 'deepseek/deepseek-v4.1-flash',
  reproducible: true,
  created_at: 1_700_000_200,
  payload: {
    failure_category: 'recall_miss',
    stage: 'retrieval',
    first_divergence: '重检索未命中目标块',
    direct_evidence: '失败与重检索的块哈希集合均缺少 h1',
    root_cause: '假设：切分把目标段落截断',
    impact_scope: 'kb1 内年假类问题',
    suggested_layer: 'chunking',
    reproduction_steps: '以原问题重检索并核对块哈希',
    acceptance_criteria: '重检索命中 h1 且答案引用证据',
    confidence: 'low',
    reproducible: true,
    evidence_refs: ['a'.repeat(64)]
  }
}

const memberWithDraft = {
  observation_id: 1,
  question: '年假额度剩几天',
  stage: 'retrieval',
  root_cause_feature: 'recall_miss',
  draft: memberDraft
}

const memberWithoutDraft = {
  observation_id: 2,
  question: '报销流程是什么',
  stage: 'retrieval',
  root_cause_feature: 'recall_miss',
  draft: null
}

const diagnosis = {
  cluster_id: 7,
  cluster_draft: null,
  member_drafts: [memberWithDraft, memberWithoutDraft],
  audit: [
    {
      id: 5,
      actor_id: '3',
      action: 'needs_more_evidence',
      cluster_id: 7,
      note: '缺少管理员确认标签',
      created_at: 1_700_000_150
    }
  ]
}

const stateMachine = {
  statuses: ['observed', 'clustered', 'awaiting_diagnosis_review', 'approved_for_candidate'],
  pools: ['observation_queue', 'candidate_pool'],
  transitions: { clustered: ['awaiting_diagnosis_review'] },
  endpoints: [
    { path: '/knowledge-operations/evolution/observations', methods: ['GET'] },
    { path: '/knowledge-operations/evolution/clusters', methods: ['GET'] },
    { path: '/knowledge-operations/evolution/clusters/{cluster_id}', methods: ['GET'] },
    { path: '/knowledge-operations/evolution/clusters/{cluster_id}/diagnosis', methods: ['GET'] },
    { path: '/knowledge-operations/evolution/state-machine', methods: ['GET'] },
    { path: '/knowledge-operations/evolution/metrics', methods: ['GET'] },
    { path: '/knowledge-operations/evolution/clusters/{cluster_id}/decision', methods: ['POST'] }
  ],
  note: '第 4 阶段成员级诊断审核'
}

/** Route every GET the view issues on mount to a canned response. */
function mockRoutes(
  options: { diagnosis?: any; diagnosisError?: any; metrics?: any } = {}
) {
  vi.mocked(api.get).mockImplementation(async (url: string) => {
    const path = String(url)
    if (path.endsWith('/diagnosis')) {
      if (options.diagnosisError) return Promise.reject(options.diagnosisError)
      return { data: options.diagnosis ?? diagnosis }
    }
    if (path.endsWith('/state-machine')) {
      return { data: stateMachine }
    }
    if (path.endsWith('/metrics')) {
      return { data: options.metrics ?? metrics }
    }
    if (path.endsWith('/knowledge-operations/evolution/clusters')) {
      return { data: { items: [candidateCluster], total: 1, page: 1, page_size: 100 } }
    }
    if (path.startsWith('/knowledge-operations/evolution/clusters/')) {
      return { data: { ...candidateCluster, members: [observation] } }
    }
    return { data: { items: [observation], total: 1, page: 1, page_size: 20 } }
  })
}

const metrics = {
  window_days: 30,
  counts: {
    observations: 5,
    observed: 0,
    clustered: 0,
    by_status: { awaiting_diagnosis_review: 1, clustered: 4 },
    clusters: 1,
    observation_queue: 0,
    candidate_pool: 1,
    skipped: 2,
    skips_by_reason: { test_env: 2 },
    funnel: {
      observed: 5,
      clustered: 5,
      awaiting_diagnosis_review: 1,
      skips: { test_env: 2 }
    }
  },
  rates: {
    problem_solve_rate: null,
    generalization_rate: 0.25,
    wrong_answer_rate: null,
    refusal_accuracy_rate: 0.5,
    avg_latency_ms: 1234.6
  },
  definitions: {
    problem_solve_rate: { definition: '关闭问题数 / 进入候选池的问题数', available_from_phase: 4 }
  }
}

/** Open the clusters tab and select the candidate cluster. */
async function openClusterDetail(wrapper: any) {
  await wrapper.get('[data-test="tab-clusters"]').trigger('click')
  await flushPromises()
  await wrapper.get('[data-test="cluster-card"]').trigger('click')
  await flushPromises()
}

function mountView() {
  return mount(EvolutionCenterView)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.post).mockResolvedValue({ data: { ...candidateCluster, members: [observation] } } as any)
  mockRoutes()
})

describe('EvolutionCenterView member-draft review', () => {
  it('requests the diagnosis of the selected cluster', async () => {
    const wrapper = mountView()
    await flushPromises()
    await openClusterDetail(wrapper)

    expect(api.get).toHaveBeenCalledWith(DIAGNOSIS_URL)
    expect(wrapper.get('[data-test="cluster-detail"]').text()).toContain('awaiting_diagnosis_review')
  })

  it('renders each member with its own draft (fields, flag) and the audit list', async () => {
    const wrapper = mountView()
    await flushPromises()
    await openClusterDetail(wrapper)

    const members = wrapper.findAll('[data-test="member-draft"]')
    expect(members).toHaveLength(2)

    // The drafted member carries the admin-visible question and all nine fields.
    const drafted = members[0]
    expect(drafted.get('[data-test="member-draft-question"]').text()).toContain('年假额度剩几天')
    for (const field of [
      'recall_miss',
      '重检索未命中目标块',
      '失败与重检索的块哈希集合均缺少 h1',
      '假设：切分把目标段落截断',
      'kb1 内年假类问题',
      'chunking',
      '以原问题重检索并核对块哈希',
      '重检索命中 h1 且答案引用证据'
    ]) {
      expect(drafted.text()).toContain(field)
    }
    expect(drafted.get('[data-test="member-draft-reproducible"]').text()).toContain('可复现')
    expect(drafted.text()).toContain('low')
    expect(drafted.text()).toContain('diagnosis-v1')
    expect(drafted.text()).toContain('deepseek/deepseek-v4.1-flash')
    expect(drafted.get('[data-test="member-draft-evidence-ref"]').text()).toContain('aaaa')

    // The undrafted member keeps its question and shows the explicit empty state.
    const undrafted = members[1]
    expect(undrafted.get('[data-test="member-draft-question"]').text()).toContain('报销流程是什么')
    expect(undrafted.get('[data-test="member-draft-empty"]').text()).toContain('暂无草案')

    const audit = wrapper.get('[data-test="diagnosis-audit"]')
    expect(audit.text()).toContain('needs_more_evidence')
    expect(audit.text()).toContain('缺少管理员确认标签')
  })

  it('renders a legacy cluster-level draft alongside the member drafts', async () => {
    mockRoutes({
      diagnosis: {
        cluster_id: 7,
        cluster_draft: memberDraft,
        member_drafts: [memberWithoutDraft],
        audit: []
      }
    })
    const wrapper = mountView()
    await flushPromises()
    await openClusterDetail(wrapper)

    const legacy = wrapper.get('[data-test="cluster-draft"]')
    expect(legacy.text()).toContain('遗留草案')
    expect(legacy.get('[data-test="cluster-draft-field-root_cause"]').text()).toContain(
      '假设：切分把目标段落截断'
    )
    expect(wrapper.get('[data-test="member-draft-empty"]').text()).toContain('暂无草案')
  })

  it('shows an explicit empty state when no member has a draft', async () => {
    mockRoutes({
      diagnosis: {
        cluster_id: 7,
        cluster_draft: null,
        member_drafts: [memberWithoutDraft],
        audit: []
      }
    })
    const wrapper = mountView()
    await flushPromises()
    await openClusterDetail(wrapper)

    expect(wrapper.get('[data-test="member-draft-empty"]').text()).toContain('暂无草案')
    expect(wrapper.findAll('[data-test="decision-approve"]')).toHaveLength(1)
  })

  it('submits each of the three decisions with the note', async () => {
    const wrapper = mountView()
    await flushPromises()
    await openClusterDetail(wrapper)

    await wrapper.get('[data-test="decision-note"]').setValue('根因已确认：切分策略问题')
    await wrapper.get('[data-test="decision-approve"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(DECISION_URL, {
      action: 'approve_for_candidate',
      note: '根因已确认：切分策略问题'
    })
    // A recorded decision clears the note, so the next one starts from a blank box.
    expect(
      (wrapper.get('[data-test="decision-note"]').element as HTMLTextAreaElement).value
    ).toBe('')

    await wrapper.get('[data-test="decision-note"]').setValue('证据仍不足')
    await wrapper.get('[data-test="decision-reject"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(DECISION_URL, { action: 'reject', note: '证据仍不足' })

    await wrapper.get('[data-test="decision-note"]').setValue('缺少管理员确认标签')
    await wrapper.get('[data-test="decision-needs-more-evidence"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(DECISION_URL, {
      action: 'needs_more_evidence',
      note: '缺少管理员确认标签'
    })
    expect(wrapper.get('[data-test="cluster-detail"]').text()).toContain('awaiting_diagnosis_review')
  })

  it('labels the three decisions for the administrator', async () => {
    const wrapper = mountView()
    await flushPromises()
    await openClusterDetail(wrapper)

    expect(wrapper.get('[data-test="decision-approve"]').text()).toContain('确认进入候选')
    expect(wrapper.get('[data-test="decision-reject"]').text()).toContain('拒绝')
    expect(wrapper.get('[data-test="decision-needs-more-evidence"]').text()).toContain('证据不足')
  })

  it('explains the administrator boundary when a decision is rejected', async () => {
    mockRoutes({ diagnosisError: { response: { status: 403 } } })
    const wrapper = mountView()
    await flushPromises()
    await openClusterDetail(wrapper)

    expect(wrapper.get('[data-test="evolution-forbidden"]').text()).toContain('管理员')
  })

  it('reports a rejected decision without hiding the draft', async () => {
    vi.mocked(api.post).mockRejectedValue({
      response: { status: 409, data: { detail: '该聚类已不在诊断审核状态' } }
    })
    const wrapper = mountView()
    await flushPromises()
    await openClusterDetail(wrapper)

    await wrapper.get('[data-test="decision-reject"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-test="evolution-error"]').text()).toContain('已不在诊断审核状态')
    expect(wrapper.get('[data-test="diagnosis-panel"]').text()).toContain('recall_miss')
  })

  it('renders the observation metrics behind the metrics tab', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-test="tab-metrics"]').trigger('click')
    await flushPromises()

    const panel = wrapper.get('[data-test="metrics-panel"]')
    expect(panel.get('[data-test="metrics-window"]').text()).toBe('30 天')
    expect(panel.get('[data-test="metrics-observations"]').text()).toBe('5')
    expect(panel.get('[data-test="metrics-clusters"]').text()).toBe('1（1）')
    expect(panel.get('[data-test="metrics-solve-rate"]').text()).toBe('样本不足')
    expect(panel.get('[data-test="metrics-generalization-rate"]').text()).toBe('25.0%')
    expect(panel.get('[data-test="metrics-refusal-rate"]').text()).toBe('50.0%')
    expect(panel.get('[data-test="metrics-latency"]').text()).toBe('1235 ms')
    expect(wrapper.get('[data-test="metrics-definitions"]').text()).toContain('problem_solve_rate')
    // The endpoint inventory is honest about the single write surface.
    await wrapper.get('[data-test="tab-clusters"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="endpoint-inventory"]').text()).toContain('POST')
  })
})

// ------------------------------------------------------------------
// Task 5: the candidate rollout panel (layers 4-5).
//
// The page lists question-set candidates, shows the five-layer gate trail and
// the payload summary, and offers the two audited layer-4/5 actions: a
// percentage canary and an instant rollback. The payload text stays in
// evolution.db, so the assertions pin the summary shape, never the text.
// ------------------------------------------------------------------

const CANDIDATES_URL = '/knowledge-operations/evolution/candidates'
const CANARY_URL = `${CANDIDATES_URL}/5/canary`
const ROLLBACK_URL = `${CANDIDATES_URL}/5/rollback`

const candidate = {
  id: 5,
  observation_id: 12,
  cluster_id: 7,
  artifact_type: 'jev_question_set',
  target: 'preflight',
  status: 'canary',
  pool: 'candidate_pool',
  confidence: 'medium',
  prompt_version: 'candidate-v1',
  model: 'deepseek/deepseek-v4.1-flash',
  rationale: 'intent 判据在结构化查询边界与线上分歧集中',
  expected_effect: '结构化查询意图判定更稳定',
  rollback_ref: {
    question_set_version: 'preflight-v1',
    payload_sha256: 'b'.repeat(64)
  },
  payload_summary: {
    question_count: 3,
    overridden: 2,
    questions: [
      { question_id: 'intent', fields: ['instructions'], instructions_chars: 44, criteria_entries: 0 },
      { question_id: 'complexity', fields: ['criteria'], instructions_chars: 0, criteria_entries: 3 },
      { question_id: 'needs_query_rewrite', fields: [], instructions_chars: 0, criteria_entries: 0 }
    ]
  },
  canary_percent: 30,
  created_at: 1_700_000_300,
  updated_at: 1_700_000_400
}

const candidateDetail = {
  ...candidate,
  gate_log: [
    {
      gate: 'scope',
      result: 'accepted',
      detail: 'jev_question_set/preflight questions=3',
      created_at: 1_700_000_300
    },
    { gate: 'benchmark', result: 'passed', detail: '路线准确率不降', created_at: 1_700_000_310 },
    { gate: 'shadow', result: 'passed', detail: '一致率 0.98', created_at: 1_700_000_350 },
    { gate: 'canary', result: 'passed', detail: 'percent=30', created_at: 1_700_000_400 }
  ],
  audit: [
    {
      id: 1,
      actor_id: '3',
      action: 'generate',
      candidate_id: 5,
      note: 'generated preflight candidate from question_set_version=preflight-v1',
      created_at: 1_700_000_300
    },
    {
      id: 2,
      actor_id: '3',
      action: 'canary',
      candidate_id: 5,
      note: '灰度 30%｜canary_percent=30',
      created_at: 1_700_000_400
    }
  ]
}

/** Route the reads the candidates tab issues; other paths keep the canned routes. */
function mockCandidateRoutes(options: { candidates?: any; candidate?: any; candidateError?: any } = {}) {
  vi.mocked(api.get).mockImplementation(async (url: string) => {
    const path = String(url)
    if (path === CANDIDATES_URL) {
      return {
        data:
          options.candidates ?? { items: [candidate], total: 1, page: 1, page_size: 100 }
      }
    }
    if (path.startsWith(`${CANDIDATES_URL}/`)) {
      if (options.candidateError) return Promise.reject(options.candidateError)
      return { data: options.candidate ?? candidateDetail }
    }
    if (path.endsWith('/state-machine')) return { data: stateMachine }
    if (path.endsWith('/metrics')) return { data: metrics }
    if (path.endsWith('/knowledge-operations/evolution/clusters')) {
      return { data: { items: [candidateCluster], total: 1, page: 1, page_size: 100 } }
    }
    if (path.startsWith('/knowledge-operations/evolution/clusters/')) {
      return { data: { ...candidateCluster, members: [observation] } }
    }
    return { data: { items: [], total: 0, page: 1, page_size: 20 } }
  })
}

describe('EvolutionCenterView candidate rollout', () => {
  it('lists candidates with target, status, confidence and canary percentage', async () => {
    mockCandidateRoutes()
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-test="tab-candidates"]').trigger('click')
    await flushPromises()

    expect(api.get).toHaveBeenCalledWith(CANDIDATES_URL, {
      params: { page: 1, page_size: 100 }
    })
    const row = wrapper.get('[data-test="candidate-row"]')
    expect(row.text()).toContain('预判')
    expect(row.text()).toContain('canary')
    expect(row.text()).toContain('medium')
    expect(row.get('[data-test="candidate-row-percent"]').text()).toBe('30%')
  })

  it('shows an explicit empty state when no candidate was generated', async () => {
    mockCandidateRoutes({ candidates: { items: [], total: 0, page: 1, page_size: 100 } })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-test="tab-candidates"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-test="candidates-empty"]').text()).toContain('暂无候选')
  })

  it('renders the five-layer gate trail, the payload summary and the rollback reference', async () => {
    mockCandidateRoutes()
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-test="tab-candidates"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="candidate-row"]').trigger('click')
    await flushPromises()

    const detail = wrapper.get('[data-test="candidate-detail"]')
    const trail = detail.get('[data-test="candidate-gate-log"]')
    const entries = trail.findAll('[data-test="candidate-gate-entry"]')
    expect(entries).toHaveLength(4)
    expect(trail.text()).toContain('层 1 产物边界')
    expect(trail.text()).toContain('层 2 基准门禁')
    expect(trail.text()).toContain('层 3 影子对比')
    expect(trail.text()).toContain('层 4 灰度')
    expect(trail.text()).toContain('percent=30')

    expect(detail.get('[data-test="candidate-rationale"]').text()).toContain(
      'intent 判据在结构化查询边界与线上分歧集中'
    )
    expect(detail.get('[data-test="candidate-expected-effect"]').text()).toContain(
      '结构化查询意图判定更稳定'
    )
    const rollbackRef = detail.get('[data-test="candidate-rollback-ref"]')
    expect(rollbackRef.text()).toContain('preflight-v1')
    expect(rollbackRef.text()).toContain('b'.repeat(64))

    const summary = detail.get('[data-test="candidate-payload-summary"]')
    expect(summary.text()).toContain('共 3 个问题，改动 2 个')
    const questions = summary.findAll('[data-test="candidate-payload-question"]')
    expect(questions).toHaveLength(3)
    expect(questions[0].text()).toContain('intent(instructions)')
    expect(questions[1].text()).toContain('complexity(criteria)')
    expect(questions[2].text()).toContain('未改动')

    // The candidate lifecycle audit is rendered next to the trail.
    const audit = detail.get('[data-test="candidate-audit"]')
    expect(audit.text()).toContain('canary')
    expect(audit.text()).toContain('灰度 30%')
  })

  it('submits the canary percentage and the rollback with the candidate id', async () => {
    mockCandidateRoutes()
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-test="tab-candidates"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="candidate-row"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-test="candidate-percent-input"]').setValue('60')
    await wrapper.get('[data-test="candidate-canary"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(CANARY_URL, { percent: 60 })

    await wrapper.get('[data-test="candidate-rollback"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(ROLLBACK_URL)
  })

  it('refuses a percentage outside 0..100 without calling the API', async () => {
    mockCandidateRoutes()
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-test="tab-candidates"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="candidate-row"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-test="candidate-percent-input"]').setValue('140')
    await wrapper.get('[data-test="candidate-canary"]').trigger('click')
    await flushPromises()

    expect(api.post).not.toHaveBeenCalledWith(CANARY_URL, { percent: 140 })
    expect(wrapper.get('[data-test="evolution-error"]').text()).toContain('0..100')
  })

  it('reports a rejected canary (percent above the configured ceiling) on the banner', async () => {
    mockCandidateRoutes()
    vi.mocked(api.post).mockRejectedValue({
      response: { status: 400, data: { detail: '请求的灰度百分比 30 超过配置上限 10' } }
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-test="tab-candidates"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="candidate-row"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-test="candidate-canary"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-test="evolution-error"]').text()).toContain('超过配置上限')
    expect(wrapper.get('[data-test="candidate-detail"]').text()).toContain('结构化查询意图判定更稳定')
  })

  it('explains the administrator boundary when the candidate reads are rejected', async () => {
    mockCandidateRoutes({ candidateError: { response: { status: 403 } } })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-test="tab-candidates"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="candidate-row"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-test="evolution-forbidden"]').text()).toContain('管理员')
  })
})
