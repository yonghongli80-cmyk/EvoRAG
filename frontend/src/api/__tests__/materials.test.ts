import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../../utils/api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn()
  }
}))

import api from '../../utils/api'
import {
  confirmArchive,
  deleteMaterial,
  getArchiveManifest,
  getMaterialJob,
  listRecycle,
  locateMaterial,
  organizationConfirm,
  organizationPreview,
  organizationRollback,
  restoreMaterial,
  uploadMaterial
} from '../materials'

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.get).mockResolvedValue({ data: {} } as any)
  vi.mocked(api.post).mockResolvedValue({ data: {} } as any)
  vi.mocked(api.delete).mockResolvedValue({ data: {} } as any)
})

describe('materials API auth', () => {
  it('uploads through the shared axios client without a per-request config', async () => {
    const file = new File(['content'], 'material.txt', { type: 'text/plain' })

    await uploadMaterial(file)

    expect(api.post).toHaveBeenCalledTimes(1)
    const call = vi.mocked(api.post).mock.calls[0]
    expect(call[0]).toBe('/materials/upload')
    expect(call[1]).toBeInstanceOf(FormData)
    expect(call).toHaveLength(2)
  })

  it('reads archive manifests without a per-request config', async () => {
    await getArchiveManifest(12)

    expect(api.get).toHaveBeenCalledWith('/materials/archives/12')
    expect(vi.mocked(api.get).mock.calls[0]).toHaveLength(1)
  })

  it('confirms archives without overriding the session cookie with a bearer header', async () => {
    await confirmArchive(12, [1, 2], 3, 'idem-key')

    expect(api.post).toHaveBeenCalledWith(
      '/materials/archives/12/confirm',
      {
        selected_entry_ids: [1, 2],
        expected_version: 3,
        idempotency_key: 'idem-key'
      }
    )
    expect(vi.mocked(api.post).mock.calls[0]).toHaveLength(2)
  })

  it('keeps every material call free of explicit headers even when a legacy token exists', async () => {
    localStorage.setItem('rag_token', 'legacy-token')

    await uploadMaterial(new File([''], 'a.txt', { type: 'text/plain' }))
    await getArchiveManifest(1)
    await confirmArchive(1, [], 0, 'k1')
    await getMaterialJob(2)
    await deleteMaterial(3)
    await restoreMaterial(3, 1)
    await listRecycle()
    await locateMaterial('invoice')
    await organizationPreview('invoice')
    await organizationConfirm(4, [], 'k2')
    await organizationRollback(5, 'k3')

    const calls = [
      ...vi.mocked(api.get).mock.calls,
      ...vi.mocked(api.post).mock.calls,
      ...vi.mocked(api.delete).mock.calls
    ]
    expect(calls).toHaveLength(11)

    for (const call of calls) {
      for (const arg of call) {
        expect(arg).not.toMatchObject({ headers: expect.anything() })
      }
      expect(JSON.stringify(call)).not.toContain('Bearer')
      expect(JSON.stringify(call)).not.toContain('legacy-token')
    }
  })

  it('lists recycle entries without a per-request config', async () => {
    await listRecycle()

    expect(api.get).toHaveBeenCalledWith('/materials/recycle')
    expect(vi.mocked(api.get).mock.calls[0]).toHaveLength(1)
  })

  it('deletes a material without a per-request config', async () => {
    await deleteMaterial(9)

    expect(api.delete).toHaveBeenCalledWith('/materials/9')
    expect(vi.mocked(api.delete).mock.calls[0]).toHaveLength(1)
  })

  it('restores and locates materials without a per-request config', async () => {
    await restoreMaterial(9, 2)
    await locateMaterial('发票', 5)

    expect(api.post).toHaveBeenNthCalledWith(
      1,
      '/materials/9/restore',
      { expected_metadata_version: 2 }
    )
    expect(api.post).toHaveBeenNthCalledWith(
      2,
      '/materials/locate',
      { text: '发票', top_k: 5 }
    )
    expect(vi.mocked(api.post).mock.calls[0]).toHaveLength(2)
    expect(vi.mocked(api.post).mock.calls[1]).toHaveLength(2)
  })

  it('previews and confirms organization without a per-request config', async () => {
    await organizationPreview('发票')
    await organizationConfirm(7, [11], 'org-key')
    await organizationRollback(8, 'rollback-key')

    expect(api.post).toHaveBeenNthCalledWith(
      1,
      '/materials/organization/preview',
      { text: '发票' }
    )
    expect(api.post).toHaveBeenNthCalledWith(
      2,
      '/materials/organization/7/confirm',
      { selected_item_ids: [11], idempotency_key: 'org-key' }
    )
    expect(api.post).toHaveBeenNthCalledWith(
      3,
      '/materials/organization/batches/8/rollback',
      { idempotency_key: 'rollback-key' }
    )
    for (const call of vi.mocked(api.post).mock.calls) {
      expect(call).toHaveLength(2)
    }
  })
})
