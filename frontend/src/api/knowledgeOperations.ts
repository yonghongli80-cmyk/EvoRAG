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
