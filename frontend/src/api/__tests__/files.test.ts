import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../utils/api', () => ({
  default: { get: vi.fn() }
}))

import api from '../../utils/api'
import { downloadLibraryFile, fileDownloadPath } from '../files'

describe('downloadLibraryFile', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('downloads through the shared axios client so it honours the API base', async () => {
    const blob = new Blob(['private'], { type: 'text/plain' })
    vi.mocked(api.get).mockResolvedValue({ data: blob } as any)

    const result = await downloadLibraryFile('a b.txt')

    expect(fileDownloadPath('a b.txt')).toBe('/files/a%20b.txt/download')
    expect(api.get).toHaveBeenCalledWith('/files/a%20b.txt/download', {
      responseType: 'blob',
      params: undefined
    })
    expect(await result.text()).toBe('private')
  })

  it('requests the explicitly selected material version when one is supplied', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: new Blob(['staged'], { type: 'text/plain' })
    } as any)

    await downloadLibraryFile('policy.txt', 42)

    expect(api.get).toHaveBeenCalledWith('/files/policy.txt/download', {
      responseType: 'blob',
      params: { version_id: 42 }
    })
  })

  it('never treats a dev-proxy HTML fallback as document bytes', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: new Blob(['<!doctype html><div id="app"></div>'], { type: 'text/html' })
    } as any)

    await expect(downloadLibraryFile('missing.pdf')).rejects.toThrow('无法读取文件')
  })

  it('throws when the request fails or returns no payload', async () => {
    vi.mocked(api.get).mockRejectedValue({ response: { status: 403 } })
    await expect(downloadLibraryFile('secret.txt')).rejects.toBeDefined()

    vi.mocked(api.get).mockResolvedValue({ data: undefined } as any)
    await expect(downloadLibraryFile('empty.txt')).rejects.toThrow('无法读取文件')
  })

  it('sends no per-request authorization header', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: new Blob(['x'], { type: 'text/plain' })
    } as any)

    await downloadLibraryFile('policy.txt')

    const config = vi.mocked(api.get).mock.calls[0][1] as Record<string, unknown>
    expect(config).not.toHaveProperty('headers')
  })
})
