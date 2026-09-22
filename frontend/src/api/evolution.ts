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
