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
  note: string
): Promise<EvolutionCluster> {
  const res = await api.post(`/knowledge-operations/evolution/clusters/${clusterId}/decision`, {
    action,
    note
  })
  return res.data
}

/** One entry of the five-layer gate trail (scope → layers 2/3 → canary). */
export interface EvolutionGateEntry {
  gate: string
  result: string
  detail: string
  created_at: number
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
