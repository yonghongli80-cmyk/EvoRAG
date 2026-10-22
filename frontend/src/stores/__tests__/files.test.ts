import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useFilesStore } from '../files'

vi.mock('../../utils/api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn()
  }
}))

vi.mock('../auth', () => ({
  useAuthStore: vi.fn(() => ({
    token: 'test-token',
    getAuthHeaders: () => ({ Authorization: 'Bearer test-token' })
  }))
}))

import api from '../../utils/api'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('files store', () => {
  // ── loadFiles ───────────────────────────────────────────
  it('loadFiles fills the list', async () => {
    const files = [
      { name: 'doc.pdf', size: 1024, size_human: '1 KB', ext: '.pdf' },
      { name: 'data.csv', size: 2048, size_human: '2 KB', ext: '.csv' }
    ]
    vi.mocked(api.get).mockResolvedValue({ data: { files } } as any)

    const store = useFilesStore()
    await store.loadFiles()

    expect(store.files).toEqual(files)
  })

  it('loadFiles returns in_kb field when present in API response', async () => {
    const files = [
      { name: 'doc.pdf', size: 1024, size_human: '1 KB', ext: '.pdf', in_kb: true },
      { name: 'data.csv', size: 2048, size_human: '2 KB', ext: '.csv', in_kb: false }
    ]
    vi.mocked(api.get).mockResolvedValue({ data: { files } } as any)

    const store = useFilesStore()
    await store.loadFiles()

    expect(store.files[0]).toHaveProperty('in_kb', true)
    expect(store.files[1]).toHaveProperty('in_kb', false)
  })

  it('loadFiles does not re-fetch when already loaded', async () => {
    const files = [{ name: 'doc.pdf', size: 512, size_human: '512 B', ext: '.pdf' }]
    vi.mocked(api.get).mockResolvedValue({ data: { files } } as any)

    const store = useFilesStore()
    await store.loadFiles()
    expect(api.get).toHaveBeenCalledTimes(1)

    // Second call should not hit API
    await store.loadFiles()
    expect(api.get).toHaveBeenCalledTimes(1)
  })

  it('loadFiles re-fetches when force=true', async () => {
    const files = [{ name: 'doc.pdf', size: 512, size_human: '512 B', ext: '.pdf' }]
    vi.mocked(api.get).mockResolvedValue({ data: { files } } as any)

    const store = useFilesStore()
    await store.loadFiles()
    expect(api.get).toHaveBeenCalledTimes(1)

    await store.loadFiles(true)
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  it('loadFiles handles error gracefully', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('network error'))

    const store = useFilesStore()
    await store.loadFiles()

    expect(store.files).toEqual([])
    expect(store.loading).toBe(false)
  })

  it('loadFiles sets loading to true during fetch and false after', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { files: [] } } as any)

    const store = useFilesStore()
    expect(store.loading).toBe(false)

    const promise = store.loadFiles()
    expect(store.loading).toBe(true)

    await promise
    expect(store.loading).toBe(false)
  })

  // ── uploadFile ──────────────────────────────────────────
  it('uploadFile calls /upload', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { success: true } } as any)
    vi.mocked(api.get).mockResolvedValue({ data: { files: [] } } as any)

    const store = useFilesStore()
    const fakeFile = new File(['content'], 'test.txt', { type: 'text/plain' })
    await store.uploadFile(fakeFile)

    expect(api.post).toHaveBeenCalledWith(
      '/materials/upload',
      expect.any(FormData)
    )
  })

  it('uploadFile refreshes file list after successful upload', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { success: true } } as any)
    const refreshedFiles = [
      { name: 'new.txt', size: 100, size_human: '100 B', ext: '.txt' }
    ]
    vi.mocked(api.get).mockResolvedValue({ data: { files: refreshedFiles } } as any)

    const store = useFilesStore()
    const fakeFile = new File(['content'], 'new.txt', { type: 'text/plain' })
    await store.uploadFile(fakeFile)

    // loadFiles is called with force=true after upload
    expect(api.get).toHaveBeenCalledWith('/files')
    expect(store.files).toEqual(refreshedFiles)
  })

  it('uploadFile leaves authorization to the shared interceptor', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {} } as any)
    vi.mocked(api.get).mockResolvedValue({ data: { files: [] } } as any)

    const store = useFilesStore()
    const fakeFile = new File([''], 'f.txt', { type: 'text/plain' })
    await store.uploadFile(fakeFile)

    expect(vi.mocked(api.post).mock.calls[0]).toHaveLength(2)
  })

  it('uploadFile of an archive opens the manifest dialog without confirming', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { status: 'awaiting_archive_confirmation', manifest_id: 7 }
    } as any)
    vi.mocked(api.get).mockResolvedValue({
      data: { manifest_id: 7, entries: [{ id: 1, state: 'approved' }] }
    } as any)

    const store = useFilesStore()
    const fakeFile = new File([''], 'onboarding.zip', { type: 'application/zip' })
    const result = await store.uploadFile(fakeFile)

    expect(result.status).toBe('awaiting_archive_confirmation')
    expect(store.archiveManifestId).toBe(7)
    expect(store.archiveVisible).toBe(true)
    expect(store.archiveEntries).toHaveLength(1)
    // No confirm call until the user confirms in the dialog.
    expect(api.post).not.toHaveBeenCalledWith(
      '/materials/archives/7/confirm',
      expect.anything(),
      expect.anything()
    )
  })

  // ── deleteFile ──────────────────────────────────────────
  it('deleteFile calls DELETE /files/{name}', async () => {
    vi.mocked(api.delete).mockResolvedValue({} as any)
    vi.mocked(api.get).mockResolvedValue({ data: { files: [] } } as any)

    const store = useFilesStore()
    await store.deleteFile('my document.pdf')

    expect(api.delete).toHaveBeenCalledWith('/files/my%20document.pdf')
  })

  it('deleteFile refreshes file list after deletion', async () => {
    vi.mocked(api.delete).mockResolvedValue({} as any)
    vi.mocked(api.get).mockResolvedValue({ data: { files: [] } } as any)

    const store = useFilesStore()
    await store.deleteFile('to-delete.txt')

    expect(api.get).toHaveBeenCalledWith('/files')
  })

  it('deleteFile URL-encodes special characters in filename', async () => {
    vi.mocked(api.delete).mockResolvedValue({} as any)
    vi.mocked(api.get).mockResolvedValue({ data: { files: [] } } as any)

    const store = useFilesStore()
    await store.deleteFile('file (1).pdf')

    expect(api.delete).toHaveBeenCalledWith('/files/file%20(1).pdf')
  })

  it('deleteFile leaves authorization to the shared interceptor', async () => {
    vi.mocked(api.delete).mockResolvedValue({} as any)
    vi.mocked(api.get).mockResolvedValue({ data: { files: [] } } as any)

    const store = useFilesStore()
    await store.deleteFile('doc.txt')

    expect(vi.mocked(api.delete).mock.calls[0]).toHaveLength(1)
  })

  // ── reset ───────────────────────────────────────────────
  it('reset drops the cached file list and forces the next loadFiles to refetch', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { files: [{ name: 'a-private.pdf', size: 1, size_human: '1 B', ext: '.pdf' }] }
    } as any)

    const store = useFilesStore()
    await store.loadFiles()
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(store.files).toHaveLength(1)

    store.reset()

    expect(store.files).toEqual([])
    expect(store.loading).toBe(false)

    await store.loadFiles()
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  it('reset clears archive, recycle and organization state', () => {
    const store = useFilesStore()
    store.archiveManifestId = 7
    store.archiveEntries = [{ id: 1 }]
    store.archiveVisible = true
    store.recycleItems = [{ document_id: 3, filename: 'deleted.pdf' }]
    store.recycleVisible = true
    store.organizationItems = [{ id: 2, target_path: '财务/发票' }]
    store.organizationProposalId = 9
    store.organizationVisible = true

    store.reset()

    expect(store.archiveManifestId).toBeNull()
    expect(store.archiveEntries).toEqual([])
    expect(store.archiveVisible).toBe(false)
    expect(store.recycleItems).toEqual([])
    expect(store.recycleVisible).toBe(false)
    expect(store.organizationItems).toEqual([])
    expect(store.organizationProposalId).toBeNull()
    expect(store.organizationVisible).toBe(false)
  })
})
