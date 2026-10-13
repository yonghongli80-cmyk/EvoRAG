import api from '../utils/api'

/** One diagnosable failure as the API serializes it: stored signals only. */
export interface EvolutionObservation {
  id: number
  cluster_id: number | null
  kb_id: string
  failure_stage: string
  root_cause_feature: string | null
  status: string
  pool: string
  priority: string
  route: string
  chunk_hashes: string[]
  adopted_evidence_hashes: string[]
  best_score: number | null
  answerability_kind: string | null
  confidence_level: string | null
  tool_names: string[]
  structured_query_kind: string | null
  exception_class: string | null
  feedback_kind: string | null
  jev_question_set_version: string | null
  decision_metadata: Record<string, string | number | boolean>
  total_ms: number
  created_at: number
  updated_at: number
}

/** One failure cluster; ``members`` is present only on the detail response. */
export interface EvolutionCluster {
  id: number
  cluster_key: string
  kb_id: string
  failure_stage: string
  root_cause_feature: string | null
  pool: string
  status: string
  priority: string
  member_count: number
  first_seen_at: number
  last_seen_at: number
  created_at: number
  updated_at: number
  members?: EvolutionObservation[]
}

export interface Paged<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface EvolutionEndpoint {
  path: string
  methods: string[]
}

/** Design section 7 transitions plus the phase-2 read-only endpoint inventory. */
export interface EvolutionStateMachine {
  /** 可确认的根因层级（后端由草稿模型派生，前端不另写一份清单）。 */
  confirmable_layers?: string[]
  statuses: string[]
  pools: string[]
  transitions: Record<string, string[]>
  endpoints: EvolutionEndpoint[]
  note: string
}

/** The nine design-section-6 fields plus the confidence and reproduction signals. */
export interface EvolutionDraftPayload {
  failure_category: string
  stage: string
  first_divergence: string
  direct_evidence: string
  root_cause: string
  impact_scope: string
  suggested_layer: string
  reproduction_steps: string
  acceptance_criteria: string
  confidence: string
  reproducible: boolean
  evidence_refs: string[]
}

/** One stored diagnosis draft as the API serializes it (administrator only). */
export interface EvolutionDraft {
  id: number
  cluster_id: number
  payload: EvolutionDraftPayload
  prompt_version: string
  model: string | null
  reproducible: boolean
  created_at: number
}

/** One row of the administrator decision trail. */
export interface EvolutionAuditEntry {
  id: number
  actor_id: string
  action: string
  cluster_id: number
  note: string
  created_at: number
}

/**
 * One member observation's draft slot in the cluster diagnosis read-out.
 *
 * Phase 4 drafts one diagnosis per member observation (a cluster key mixes
 * distinct root causes), so the diagnosis endpoint lists every member with its
 * question, stage, and root-cause feature alongside its own draft (or ``null``
 * when the worker has not drafted it yet). The question text here is
 * administrator-only and is the point of diagnosing the member.
 */
export interface EvolutionMemberDraft {
  observation_id: number
  question: string
  stage: string
  root_cause_feature: string | null
  draft: EvolutionDraft | null
}

/** The diagnosis read-out for one cluster: legacy cluster draft, member drafts, audit trail. */
export interface EvolutionDiagnosis {
  cluster_id: number
  cluster_draft: EvolutionDraft | null
  member_drafts: EvolutionMemberDraft[]
  audit: EvolutionAuditEntry[]
}

/** The three administrator decisions of design section 8. */
export type EvolutionDecisionAction =
  | 'approve_for_candidate'
  | 'reject'
  | 'needs_more_evidence'
  /** 只记录管理员的根因层级判断，不动状态（补丁生成器以它为准）。 */
  | 'confirm_layer'

/** One metric definition with the phase it becomes available in. */
export interface EvolutionMetricDefinition {
  definition: string
  available_from_phase: number
}

/** The phase-2 observation metrics as ``GET /metrics`` serializes them. */
export interface EvolutionMetrics {
  window_days: number
  counts: {
    observations: number
    observed: number
    clustered: number
    by_status: Record<string, number>
    clusters: number
    observation_queue: number
    candidate_pool: number
    skipped: number
    skips_by_reason: Record<string, number>
    funnel: {
      observed: number
      clustered: number
      awaiting_diagnosis_review: number
      skips: Record<string, number>
    }
  }
  rates: {
    problem_solve_rate: number | null
    generalization_rate: number | null
    wrong_answer_rate: number | null
    refusal_accuracy_rate: number | null
    avg_latency_ms: number | null
  }
  definitions: Record<string, EvolutionMetricDefinition>
}

export interface EvolutionQuery {
  status?: string
  pool?: string
  kbId?: string
  page?: number
  pageSize?: number
}

/** Build query parameters the way the backend spells them, omitting unset filters. */
function pageParams(query: EvolutionQuery): Record<string, string | number> {
  const params: Record<string, string | number> = {
    page: query.page ?? 1,
    page_size: query.pageSize ?? 20
  }
  if (query.status) params.status = query.status
  if (query.pool) params.pool = query.pool
  if (query.kbId) params.kb_id = query.kbId
  return params
}

/** Return one page of failure observations (administrator only). */
export async function fetchEvolutionObservations(
  query: EvolutionQuery = {}
): Promise<Paged<EvolutionObservation>> {
  const res = await api.get('/knowledge-operations/evolution/observations', {
    params: pageParams(query)
  })
  return res.data
}

/** Return one page of failure clusters (administrator only). */
export async function fetchEvolutionClusters(
  query: EvolutionQuery = {}
): Promise<Paged<EvolutionCluster>> {
  const res = await api.get('/knowledge-operations/evolution/clusters', {
    params: pageParams(query)
  })
  return res.data
}

/** Return one cluster with its member observations (administrator only). */
export async function fetchEvolutionCluster(clusterId: number): Promise<EvolutionCluster> {
  const res = await api.get(`/knowledge-operations/evolution/clusters/${clusterId}`)
  return res.data
}

/** Return the read-only state-machine reference (administrator only). */
export async function fetchEvolutionStateMachine(): Promise<EvolutionStateMachine> {
  const res = await api.get('/knowledge-operations/evolution/state-machine')
  return res.data
}

/** Return the phase-2 observation metrics (administrator only). */
export async function fetchEvolutionMetrics(): Promise<EvolutionMetrics> {
  const res = await api.get('/knowledge-operations/evolution/metrics')
  return res.data
}

/** Return the cluster-level legacy draft, each member draft, and the audit trail. */
export async function fetchDiagnosis(clusterId: number): Promise<EvolutionDiagnosis> {
  const res = await api.get(`/knowledge-operations/evolution/clusters/${clusterId}/diagnosis`)
  return res.data
}

/** Submit one administrator decision and return the updated cluster. */
export async function submitDecision(
  clusterId: number,
  action: EvolutionDecisionAction,
  note: string,
  /** 仅 confirm_layer 动作使用：管理员确认的根因层级。 */
  confirmedLayer?: string
): Promise<EvolutionCluster> {
  const res = await api.post(`/knowledge-operations/evolution/clusters/${clusterId}/decision`, {
    action,
    note,
    ...(confirmedLayer ? { confirmed_layer: confirmedLayer } : {})
  })
  return res.data
}

/** One entry of the five-layer gate trail (scope → layers 2/3 → canary). */
export interface EvolutionGateEntry {
  gate: string
  result: string
  detail: string
  created_at: number
  /** 这一层写回的结构化指标；老条目没有这个键，读取端据此决定要不要画对照表。 */
  metrics?: SandboxGateMetrics
}

/**
 * 一次**层 2 对照**的数字，沙箱与宿主 A/B 共用这一个形状。
 *
 * 两者都量"干净侧 vs 打补丁侧"，差别只在谁跑的：沙箱一定写 `applied`/`tests_passed`；
 * 宿主 A/B 写 `runs`/`kb_id` 这些它自己才知道的事实。所以这三项按可选处理，渲染端逐个判空。
 */
export interface SandboxGateMetrics {
  applied?: boolean
  tests_passed?: boolean | null
  failed_tests?: string[]
  /** 宿主 A/B：每侧跑了几次、在哪个库上跑、跑了哪个 worktree。 */
  runs?: number
  warmups?: number
  kb_id?: string
  worktrees?: string[]
  duration_s?: number
  error?: string
  verdict?: {
    sides: Record<
      string,
      {
        baseline: SandboxSideMetrics
        patched: SandboxSideMetrics
      }
    >
    route_delta_pp: number
    top5_delta_pp: number
    failed_calls_delta: number
    passed: boolean
    /** patch_verdict 自带的人话结论（含哪一侧、差多少、限多少）。 */
    verdict?: string
    cases?: number
  }
  /** 没有可比的对照（例如回放不可用）：显式标出来，别让"没有数字"看起来像"没变差"。 */
  verdict_unavailable?: boolean
}

/** 宿主 A/B 的指标与沙箱同形（同一个层 2 对照，只是跑在宿主上）。 */
export type HostAbGateMetrics = SandboxGateMetrics

export interface SandboxSideMetrics {
  route_accuracy: number
  top5_hit_rate: number
  failed_calls: number
  prep_p50_ms: number | null
}

/** One row of the candidate lifecycle audit trail. */
export interface EvolutionCandidateAudit {
  id: number
  actor_id: string
  action: string
  candidate_id: number
  note: string
  created_at: number
}

/**
 * A text-free summary of one payload override.
 *
 * The candidate payload itself stays in ``evolution.db``; the UI shows which
 * questions the candidate touches and how much text it moves, because the
 * rollout decision is taken on the gate trail, the rationale and the expected
 * effect — never on a payload the observer cannot verify.
 */
export interface CandidatePayloadQuestion {
  question_id: string
  fields: string[]
  instructions_chars: number
  criteria_entries: number
}

/** Which questions a candidate overrides, and how many of them it touches. */
export interface CandidatePayloadSummary {
  question_count: number
  overridden: number
  questions: CandidatePayloadQuestion[]
}

/** One question-set candidate as the list endpoint serializes it. */
export interface EvolutionCandidate {
  id: number
  observation_id: number | null
  cluster_id: number
  artifact_type: string
  target: string
  status: string
  pool: string
  confidence: string
  prompt_version: string
  model: string | null
  rationale: string
  expected_effect: string
  rollback_ref: Record<string, string>
  payload_summary: CandidatePayloadSummary
  canary_percent: number
  created_at: number
  updated_at: number
}

/** One candidate with its five-layer gate trail and lifecycle audit rows. */
export interface EvolutionCandidateDetail extends EvolutionCandidate {
  gate_log: EvolutionGateEntry[]
  audit: EvolutionCandidateAudit[]
}

export interface CandidateQuery {
  status?: string
  page?: number
  pageSize?: number
}

/** Return one page of question-set candidates (administrator only). */
export async function fetchCandidates(
  query: CandidateQuery = {}
): Promise<Paged<EvolutionCandidate>> {
  const res = await api.get('/knowledge-operations/evolution/candidates', {
    params: pageParams(query)
  })
  return res.data
}

/** Return one candidate with its gate trail and audit history (administrator only). */
export async function fetchCandidate(candidateId: number): Promise<EvolutionCandidateDetail> {
  const res = await api.get(`/knowledge-operations/evolution/candidates/${candidateId}`)
  return res.data
}

/** Roll one candidate out by percentage and return the updated candidate. */
export async function submitCanary(
  candidateId: number,
  percent: number
): Promise<EvolutionCandidate> {
  const res = await api.post(`/knowledge-operations/evolution/candidates/${candidateId}/canary`, {
    percent
  })
  return res.data
}

/** Roll one candidate back to the current question set and return it. */
export async function rollbackCandidate(candidateId: number): Promise<EvolutionCandidate> {
  const res = await api.post(`/knowledge-operations/evolution/candidates/${candidateId}/rollback`)
  return res.data
}

// ----------------------------------------------------------------------
// Task 4: the code-patch review surface.
//
// Layer 4 has no percentage canary for a code patch (there is no second
// instance to serve a share of traffic), so the review is the gate: a list of
// patch rows without the diff body, a detail that carries the diff verbatim plus
// the sandbox conclusion and the five-layer trail, and one audited decision
// endpoint. ``approve_for_apply`` exports ``artifacts/patches/candidate-<id>.patch``
// and records the apply command — the API never applies anything.
// ----------------------------------------------------------------------

/** One code-patch candidate as the list endpoint serializes it (no diff body). */
export interface EvolutionCodePatch {
  id: number
  observation_id: number | null
  cluster_id: number
  artifact_type: string
  layer: string
  /** The files the diff touches, read from the diff headers by the API. */
  paths: string[]
  /** The verification targets the sandbox ran (and the apply command re-runs). */
  test_paths: string[]
  status: string
  pool: string
  confidence: string
  prompt_version: string
  model: string | null
  rationale: string
  expected_effect: string
  rollback_ref: Record<string, string>
  /** 同一条改动的「家族键」（文件:函数）；后端只做提示，从不拦截。 */
  overlap_key?: string
  /** 与它同类（改同一个函数）的其他候选 id；两个以上才会出现。 */
  overlap_ids?: number[]
  /** The sandbox conclusion of layers 2/3, lifted out of the gate trail. */
  sandbox_summary: EvolutionGateEntry[]
  created_at: number
  updated_at: number
}

/** One code patch with its diff body, its five-layer trail and its audit rows. */
export interface EvolutionCodePatchDetail extends EvolutionCodePatch {
  diff: string
  gate_log: EvolutionGateEntry[]
  audit: EvolutionCandidateAudit[]
  /**
   * 层 2 的两个生产者分开给：沙箱（应用 + 目标测试 + 固定回归网）与宿主 A/B
   * （真实的干净侧 vs 打补丁侧）。合并成一个列表会让"这个数字是谁量出来的"消失。
   */
  sandbox_summary: EvolutionGateEntry[]
  host_ab_summary: EvolutionGateEntry[]
  /** 批准后导出的补丁文件；命令只有导出后可用（端点从不执行命令）。 */
  apply_command: string
  revert_command: string
  export_path: string
  exported: boolean
}

/** The three administrator decisions of the code-patch review. */
export type CodePatchDecisionAction = 'approve_for_apply' | 'reject' | 'needs_revision'

export interface CodePatchQuery {
  status?: string
  page?: number
  pageSize?: number
}

/** Return one page of code-patch candidates (administrator only). */
export async function fetchCodePatches(
  query: CodePatchQuery = {}
): Promise<Paged<EvolutionCodePatch>> {
  const res = await api.get('/knowledge-operations/evolution/code-candidates', {
    params: pageParams(query)
  })
  return res.data
}

/**
 * Return one code patch with its diff, gate trail and audit history.
 *
 * This is the only response that carries the diff body: it is the file content a
 * reviewer must read, and the page renders it read-only.
 */
export async function fetchCodePatch(patchId: number): Promise<EvolutionCodePatchDetail> {
  const res = await api.get(`/knowledge-operations/evolution/code-candidates/${patchId}`)
  return res.data
}

/**
 * Submit one review decision and return the updated patch.
 *
 * ``approve_for_apply`` exports the patch and records the apply command; the
 * endpoint never applies it, so the caller only refreshes the trail.
 */
export async function submitCodePatchDecision(
  patchId: number,
  action: CodePatchDecisionAction,
  note: string
): Promise<EvolutionCodePatchDetail> {
  const res = await api.post(
    `/knowledge-operations/evolution/code-candidates/${patchId}/decision`,
    { action, note }
  )
  return res.data
}


/**
 * One knowledge-gap ticket: a request for content, not a candidate.
 *
 * It never enters the candidate pool and no gate reads it; the worker drafts it from an
 * ``undetermined`` cluster, and its deterministic re-check moves it to ``content_added`` once
 * material has been ingested into that knowledge base.
 */
export interface GapTicket {
  id: number
  cluster_id: number
  kb_id: string
  status: string
  occurrences: number
  created_at: number
  updated_at: number
  /** 触发它的问法样本（去重、上限 8），给内容编辑看"到底缺什么问法"。 */
  question_samples: string[]
  /** 召回窗口里没出现的问法词，按出现次数排序——它说的是"该写什么"。 */
  missing_terms: string[]
  rationale: string
  /** 最近一次复测：覆盖了哪些词、还缺哪些、结论。 */
  recheck: {
    checked_at: number
    probed: number
    covered_terms: string[]
    still_missing: string[]
    verdict: string
  } | null
}

export interface GapTicketQuery {
  status?: string
  page?: number
  pageSize?: number
}

/** Return one page of knowledge-gap tickets (administrator only, read-only). */
export async function fetchGapTickets(query: GapTicketQuery = {}): Promise<Paged<GapTicket>> {
  const res = await api.get('/knowledge-operations/evolution/gap-tickets', {
    params: pageParams(query)
  })
  return res.data
}

/**
 * Apply an approved patch behind the backend's guards and optionally restart the service.
 *
 * Guards run in order — the patch must apply cleanly, the app must still import, the patch's own
 * target tests must pass — and any failure reverts the patch and leaves the service untouched.
 */
export async function applyCodePatch(
  candidateId: number,
  restart = true
): Promise<{ applied: boolean; applied_now: boolean; steps: Record<string, unknown>; restart_helper: string }> {
  const res = await api.post(
    `/knowledge-operations/evolution/code-candidates/${candidateId}/apply`,
    { restart }
  )
  return res.data
}

/**
 * Regenerate a patch from the cluster a rolled-back patch came from.
 *
 * `rolled_back` is terminal — the interface cannot move it back to an applicable state — so the
 * way to try the same change again is a **new** candidate for the same cluster, which then walks
 * the ordinary path (sandbox → review → approval → apply). The backend refuses while the cluster
 * already carries another open patch, and refuses when the newest diagnosis draft no longer
 * agrees with the confirmed layer.
 */
export async function regenerateCodePatch(
  candidateId: number,
  note = ''
): Promise<EvolutionCodePatchDetail> {
  const res = await api.post(
    `/knowledge-operations/evolution/code-candidates/${candidateId}/regenerate`,
    { note }
  )
  return res.data
}

/**
 * Close a patch row out as merged: the change reached the code through a **commit**, not through
 * apply (an operator merged it by hand).
 *
 * The backend refuses unless the patch is present in the tree **and** the paths it touches are
 * clean relative to HEAD, so this cannot turn "not shipped" into "accepted". `mergedCommit` is
 * optional: when omitted, the backend names the last commit that touched those paths.
 */
export async function closeOutMergedCodePatch(
  candidateId: number,
  note = '',
  mergedCommit = ''
): Promise<EvolutionCodePatchDetail> {
  const res = await api.post(
    `/knowledge-operations/evolution/code-candidates/${candidateId}/decision`,
    { action: 'close_out_merged', note, merged_commit: mergedCommit }
  )
  return res.data
}
