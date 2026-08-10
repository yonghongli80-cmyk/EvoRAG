import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchMyFeedback, resolveConflict, submitKnowledgeFeedback } from '../knowledgeOperations'

vi.mock('../../utils/api', () => ({ default: { post: vi.fn(), get: vi.fn(), put: vi.fn() } }))

import api from '../../utils/api'

beforeEach(() => vi.clearAllMocks())

describe('submitKnowledgeFeedback', () => {
  it('posts the structured feedback payload', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { feedback_id: 3 } })

    await submitKnowledgeFeedback({
      assistant_message_id: 7,
      kind: 'outdated',
      citation_id: 1,
      comment: 'old policy',
      suggested_answer: 'new policy'
    })

    expect(api.post).toHaveBeenCalledWith('/feedback/knowledge', {
      assistant_message_id: 7,
      kind: 'outdated',
      citation_id: 1,
      comment: 'old policy',
      suggested_answer: 'new policy'
    })
  })
})

describe('fetchMyFeedback', () => {
  it('fetches the current user feedback page', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { items: [], total: 0 } } as any)

    await fetchMyFeedback(1, 50)

    expect(api.get).toHaveBeenCalledWith(
      '/knowledge-operations/my-feedback',
      expect.objectContaining({ params: { page: 1, page_size: 50 } })
    )
  })
})

describe('resolveConflict', () => {
  it('sends the selected resolution without document details', async () => {
    vi.mocked(api.put).mockResolvedValue({ data: { id: 9, status: 'resolved' } })

    await expect(resolveConflict(9, 'resolved', 'confirmed')).resolves.toEqual({
      id: 9,
      status: 'resolved'
    })

    expect(api.put).toHaveBeenCalledWith('/knowledge-operations/conflicts/9/resolve', {
      resolution: 'resolved',
      note: 'confirmed'
    })
  })
})
