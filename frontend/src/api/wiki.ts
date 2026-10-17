import api from '../utils/api'

export interface WikiPage {
  id: number
  kb_id: string
  title: string
  slug: string
  content: string
  tags: string[]
  current_revision_id: number
  created_at: number
  updated_at: number
}

export interface OutgoingLink {
  target_title: string
  target_page_id: number | null
}

export interface WikiPageDetail {
  page: WikiPage
  outgoing_links: OutgoingLink[]
  backlinks: WikiPage[]
  revision_count: number
}

export interface WikiRevision {
  id: number
  page_id: number
  revision_num: number
  title: string
  content: string
  delta: string | null
  author_id: string | null
  comment: string
  sha256_hash: string
  created_at: number
}

export interface WikiRevisionDiff {
  page_id: number
  revision_num: number
  delta: string | null
  comment: string
  sha256_hash: string
  created_at: number
}

export interface CreateWikiPagePayload {
  kb_id: string
  title: string
  content: string
  slug?: string
  tags?: string[]
}

export interface UpdateWikiPagePayload {
  title?: string
  content?: string
  tags?: string[]
  comment?: string
}

export async function getWikiPages(params?: {
  kb_id?: string
  tag?: string
  limit?: number
  offset?: number
}): Promise<WikiPage[]> {
  const res = await api.get<WikiPage[]>('/wiki/pages', { params })
  return res.data
}

export async function createWikiPage(payload: CreateWikiPagePayload): Promise<{ page: WikiPage; chunks_indexed: number }> {
  const res = await api.post<{ page: WikiPage; chunks_indexed: number }>('/wiki/pages', payload)
  return res.data
}

export async function getWikiPage(pageId: number): Promise<WikiPageDetail> {
  const res = await api.get<WikiPageDetail>(`/wiki/pages/${pageId}`)
  return res.data
}

export async function updateWikiPage(
  pageId: number,
  payload: UpdateWikiPagePayload
): Promise<{ page: WikiPage; chunks_indexed: number }> {
  const res = await api.put<{ page: WikiPage; chunks_indexed: number }>(`/wiki/pages/${pageId}`, payload)
  return res.data
}

export async function deleteWikiPage(pageId: number): Promise<{ deleted: boolean; id: number }> {
  const res = await api.delete<{ deleted: boolean; id: number }>(`/wiki/pages/${pageId}`)
  return res.data
}

export async function getWikiHistory(pageId: number): Promise<WikiRevision[]> {
  const res = await api.get<WikiRevision[]>(`/wiki/pages/${pageId}/history`)
  return res.data
}

export async function rollbackWikiPage(
  pageId: number,
  revisionNum: number
): Promise<{ page: WikiPage; chunks_indexed: number }> {
  const res = await api.post<{ page: WikiPage; chunks_indexed: number }>(`/wiki/pages/${pageId}/rollback`, {
    revision_num: revisionNum
  })
  return res.data
}

export async function getWikiRevisionDiff(pageId: number, revisionNum: number): Promise<WikiRevisionDiff> {
  const res = await api.get<WikiRevisionDiff>(`/wiki/pages/${pageId}/revisions/${revisionNum}/diff`)
  return res.data
}
