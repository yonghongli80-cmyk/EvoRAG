import api from '../utils/api'
import { useAuthStore } from '../stores/auth'

export type DuplicateAction = 'reject' | 'overwrite' | 'keep'

export interface Citation {
  citation_id: number
  doc_name: string
  kb_id: string
  section_title: string
  version_no: number
  updated_at: number
  is_historical: boolean
  text_preview: string
  download_url: string
}

export interface DocumentVersion {
  id: number
  document_id: number
  version_no: number
  filename: string
  status: string
  validity_status: string
  created_at: number
  superseded_at: number | null
  content_hash: string
  uploaded_by?: number
  restored_from_version_id?: number | null
}

/** Upload a file with an explicit duplicate resolution action. */
export async function uploadWithDuplicateAction(
  file: File,
  kbId: string,
  action: DuplicateAction
): Promise<any> {
  const auth = useAuthStore()
  const url = kbId === 'rag_docs' ? '/upload' : `/knowledge-bases/${kbId}/documents`
  const form = new FormData()
  form.append('file', file)
  form.append('duplicate_action', action)
  const res = await api.post(url, form, { headers: auth.getAuthHeaders() })
  return res.data
}

/** Link an existing document into another KB without a browser download. */
export async function linkDocumentToKb(
  documentId: number,
  kbId: string,
  action: DuplicateAction = 'reject'
): Promise<any> {
  const res = await api.post(`/knowledge-bases/${kbId}/documents/link`, {
    document_id: documentId,
    duplicate_action: action
  })
  return res.data
}

/** List immutable versions for one logical document. */
export async function fetchDocumentVersions(documentId: number): Promise<DocumentVersion[]> {
  const auth = useAuthStore()
  const res = await api.get(`/documents/${documentId}/versions`, {
    headers: auth.getAuthHeaders()
  })
  return res.data.versions
}
