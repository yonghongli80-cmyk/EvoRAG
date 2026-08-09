import api from '../utils/api'

export interface KnowledgeFeedbackRequest {
  assistant_message_id: number
  kind: 'useful' | 'not_useful' | 'outdated'
  citation_id?: number
  comment: string
  suggested_answer: string
}

export interface KnowledgeFeedback {
  id: number
  message_id: number
  kind: string
  question_text: string
  citation_id: number | null
  comment: string
  suggested_answer: string
  status: string
  created_at: number
}

export interface Paged<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/** Submit structured knowledge feedback with an optional citation. */
export async function submitKnowledgeFeedback(
  payload: KnowledgeFeedbackRequest
): Promise<any> {
  return api.post('/feedback/knowledge', payload)
}

/** Return the current user's own feedback rows. */
export async function fetchMyFeedback(page = 1, pageSize = 20): Promise<Paged<KnowledgeFeedback>> {
  const res = await api.get('/knowledge-operations/my-feedback', {
    params: { page, page_size: pageSize }
  })
  return res.data
}

export interface KnowledgeTask {
  id: number
  task_type: string
  document_id: number | null
  document_version_id: number | null
  assigned_to: number | null
  status: string
  report_count: number
  resolution_note: string
  due_at: number
  created_at: number
  updated_at: number
  resolved_at: number | null
  reporters?: string[]
}

export interface TaskSummary {
  open: number
  overdue: number
  outdated: number
  not_useful: number
  stale_knowledge_bases: number
}

/** Return tasks assigned to the current user. */
export async function fetchMyTasks(page = 1, pageSize = 20): Promise<Paged<KnowledgeTask>> {
  const res = await api.get('/knowledge-operations/my-tasks', {
    params: { page, page_size: pageSize }
  })
  return res.data
}

/** Return the knowledge-operations summary counts. */
export async function fetchOperationsSummary(): Promise<TaskSummary> {
  const res = await api.get('/knowledge-operations/summary')
  return res.data
}

/** Transition a task state. */
export async function transitionTask(
  taskId: number,
  status: string,
  expectedUpdatedAt: number,
  note = ''
): Promise<KnowledgeTask> {
  const res = await api.put(`/knowledge-operations/tasks/${taskId}`, {
    status,
    note,
    expected_updated_at: expectedUpdatedAt
  })
  return res.data
}
