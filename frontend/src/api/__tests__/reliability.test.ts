import { beforeEach, describe, expect, it, vi } from 'vitest'
import { compareDocuments } from '../reliability'

vi.mock('../../utils/api', () => ({ default: { post: vi.fn() } }))

import api from '../../utils/api'

beforeEach(() => vi.clearAllMocks())

describe('compareDocuments', () => {
  it('posts only logical document IDs and returns the comparison result', async () => {
    const result = { rows: [], partial: false, failures: [] }
    vi.mocked(api.post).mockResolvedValue({ data: result })

    await expect(compareDocuments({
      question: '比较制度',
      document_ids: [7, 8],
      dimensions: ['年假']
    })).resolves.toEqual(result)

    expect(api.post).toHaveBeenCalledWith('/reliability/compare', {
      question: '比较制度',
      document_ids: [7, 8],
      dimensions: ['年假']
    })
  })
})
