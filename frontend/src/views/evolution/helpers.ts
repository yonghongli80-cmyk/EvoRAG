/** The nine design-section-6 text fields, in the order an administrator reads them. */
export const DRAFT_FIELDS = [
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

export const DRAFT_FIELD_LABELS: Record<(typeof DRAFT_FIELDS)[number], string> = {
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

/** The five gate layers in the order an administrator reads the trail. */
export const GATE_LABELS: Record<string, string> = {
  scope: '层 1 产物边界',
  benchmark: '层 2 基准门禁',
  shadow: '层 3 影子对比',
  canary: '层 4 灰度',
  rollback: '层 5 回滚',
  sandbox: '层 2/3 沙箱（隔离 worktree）',
  host_ab: '层 2 宿主 A/B 对照'
}

export const GATE_RESULT_LABELS: Record<string, string> = {
  accepted: '通过',
  passed: '通过',
  failed: '未通过',
  blocked: '阻塞',
  inconclusive: '样本不足'
}

/** The five evolution layers a code patch may address, as the boundary names them. */
export const LAYER_LABELS: Record<string, string> = {
  chunking: '切块策略',
  reranking: '重排流程',
  parser: '解析器',
  tool_workflow: '工具工作流',
  code: '项目代码'
}

export function isForbidden(err: any): boolean {
  return err?.response?.status === 403
}

export function formatTime(timestamp: number): string {
  return new Date(timestamp * 1000).toISOString().slice(0, 19).replace('T', ' ')
}

export function shortHash(hash: string): string {
  return hash.length > 12 ? `${hash.slice(0, 12)}…` : hash
}

export function stageLabel(stage: string): string {
  return ({
    retrieval: '检索',
    generation: '生成',
    structured_query: '结构化查询',
    tool: '工具',
    unknown: '未判定'
  } as Record<string, string>)[stage] || stage
}

export function poolLabel(pool: string): string {
  return pool === 'candidate_pool' ? '候选池' : '观察队列'
}

export function formatRate(rate: number | null): string {
  return rate === null ? '样本不足' : `${(rate * 100).toFixed(1)}%`
}

export function formatLatency(ms: number | null): string {
  return ms === null ? '样本不足' : `${ms.toFixed(0)} ms`
}

export function formatDelta(value: number): string {
  return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1)
}

export function gateLabel(gate: string): string {
  return GATE_LABELS[gate] || gate
}

export function gateResultLabel(result: string): string {
  return GATE_RESULT_LABELS[result] || result
}

export function targetLabel(target: string): string {
  return target === 'passages' ? '段落判定' : '预判'
}

export function payloadFields(fields: string[]): string {
  return fields.map((field) => (field === 'instructions' ? 'instructions' : 'criteria')).join(' + ')
}

export function layerLabel(layer: string): string {
  return LAYER_LABELS[layer] || layer
}

export function shortCommit(commit: string): string {
  return commit.length > 12 ? `${commit.slice(0, 12)}…` : commit
}
