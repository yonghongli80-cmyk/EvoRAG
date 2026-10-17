import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../utils/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
}))

import api from '../../utils/api'
import {
  getWikiPages,
  getWikiPage,
  createWikiPage,
  updateWikiPage,
  deleteWikiPage,
  getWikiHistory,
  rollbackWikiPage,
  getWikiRevisionDiff
} from '../wiki'

describe('Wiki API client', () => {
  beforeEach(() => vi.clearAllMocks())

  it('lists wiki pages with params', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: [{ id: 1, title: '测试页面', kb_id: 'kb_1' }]
    } as any)

    const res = await getWikiPages({ kb_id: 'kb_1', tag: 'test' })

    expect(api.get).toHaveBeenCalledWith('/wiki/pages', {
      params: { kb_id: 'kb_1', tag: 'test' }
    })
    expect(res).toHaveLength(1)
    expect(res[0].title).toBe('测试页面')
  })

  it('creates wiki page and returns page with chunks_indexed', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: {
        page: { id: 2, title: '新页面', kb_id: 'kb_1', content: '内容' },
        chunks_indexed: 3
      }
    } as any)

    const res = await createWikiPage({
      kb_id: 'kb_1',
      title: '新页面',
      content: '内容',
      tags: ['guide']
    })

    expect(api.post).toHaveBeenCalledWith('/wiki/pages', {
      kb_id: 'kb_1',
      title: '新页面',
      content: '内容',
      tags: ['guide']
    })
    expect(res.chunks_indexed).toBe(3)
  })

  it('gets wiki page detail with backlinks and outgoing links', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        page: { id: 1, title: '页面1' },
        outgoing_links: [{ target_title: '页面2', target_page_id: 2 }],
        backlinks: [],
        revision_count: 2
      }
    } as any)

    const res = await getWikiPage(1)

    expect(api.get).toHaveBeenCalledWith('/wiki/pages/1')
    expect(res.outgoing_links[0].target_title).toBe('页面2')
    expect(res.revision_count).toBe(2)
  })

  it('updates wiki page', async () => {
    vi.mocked(api.put).mockResolvedValue({
      data: {
        page: { id: 1, title: '更新标题' },
        chunks_indexed: 2
      }
    } as any)

    const res = await updateWikiPage(1, { title: '更新标题', comment: '修改标题' })

    expect(api.put).toHaveBeenCalledWith('/wiki/pages/1', {
      title: '更新标题',
      comment: '修改标题'
    })
    expect(res.page.title).toBe('更新标题')
  })

  it('deletes wiki page', async () => {
    vi.mocked(api.delete).mockResolvedValue({
      data: { deleted: true, id: 1 }
    } as any)

    const res = await deleteWikiPage(1)

    expect(api.delete).toHaveBeenCalledWith('/wiki/pages/1')
    expect(res.deleted).toBe(true)
  })

  it('gets revision history, diff and rollback', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({
      data: [{ id: 10, revision_num: 1, comment: '初始' }]
    } as any)
    vi.mocked(api.get).mockResolvedValueOnce({
      data: { page_id: 1, revision_num: 1, delta: '@@ -1 +1 @@' }
    } as any)
    vi.mocked(api.post).mockResolvedValueOnce({
      data: { page: { id: 1, title: '回滚后' }, chunks_indexed: 1 }
    } as any)

    const history = await getWikiHistory(1)
    expect(api.get).toHaveBeenCalledWith('/wiki/pages/1/history')
    expect(history).toHaveLength(1)

    const diff = await getWikiRevisionDiff(1, 1)
    expect(api.get).toHaveBeenCalledWith('/wiki/pages/1/revisions/1/diff')
    expect(diff.delta).toBe('@@ -1 +1 @@')

    const rollback = await rollbackWikiPage(1, 1)
    expect(api.post).toHaveBeenCalledWith('/wiki/pages/1/rollback', { revision_num: 1 })
    expect(rollback.chunks_indexed).toBe(1)
  })
})
