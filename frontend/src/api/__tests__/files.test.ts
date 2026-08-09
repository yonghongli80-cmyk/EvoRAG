import { afterEach, describe, expect, it, vi } from 'vitest'
import { downloadLibraryFile } from '../files'

describe('downloadLibraryFile', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('downloads through the authenticated API route', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('private', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const blob = await downloadLibraryFile('a b.txt', { Authorization: 'Bearer token' })

    expect(await blob.text()).toBe('private')
    expect(fetchMock).toHaveBeenCalledWith('/files/a%20b.txt/download', {
      headers: { Authorization: 'Bearer token' }
    })
  })

  it('throws without returning a blob when authorization fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 403 })))

    await expect(downloadLibraryFile('secret.txt', {})).rejects.toThrow('无法读取文件')
  })
})
