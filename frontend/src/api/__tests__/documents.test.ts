import { beforeEach, describe, expect, it, vi } from 'vitest'
import { uploadWithDuplicateAction, fetchDocumentVersions } from '../documents'

vi.mock('../../utils/api', () => ({ default: { post: vi.fn(), get: vi.fn() } }))
vi.mock('../../stores/auth', () => ({
  useAuthStore: vi.fn(() => ({ getAuthHeaders: () => ({ Authorization: 'Bearer t' }) }))
}))

import api from '../../utils/api'

beforeEach(() => vi.clearAllMocks())

describe('uploadWithDuplicateAction', () => {
  it('posts the file with the duplicate action', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { status: 'overwritten', version_no: 2 } })
    const file = new File(['v2'], 'policy.txt')

    const result = await uploadWithDuplicateAction(file, 'rag_docs', 'overwrite')

    expect(result.status).toBe('overwritten')
    const [url, form] = vi.mocked(api.post).mock.calls[0]
    expect(url).toBe('/upload')
    expect((form as FormData).get('duplicate_action')).toBe('overwrite')
    expect((form as FormData).get('file')).toBe(file)
  })
})

describe('fetchDocumentVersions', () => {
  it('returns versions from the API', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { versions: [{ id: 1, version_no: 1, filename: 'policy.txt' }] }
    } as any)

    const versions = await fetchDocumentVersions(7)

    expect(api.get).toHaveBeenCalledWith('/documents/7/versions', expect.anything())
    expect(versions).toHaveLength(1)
  })
})
