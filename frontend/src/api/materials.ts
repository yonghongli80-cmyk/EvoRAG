import api from '../utils/api'

function headers() {
  return { Authorization: `Bearer ${localStorage.getItem('rag_token') || ''}` }
}

export interface ArchiveEntry {
  id: number
  relative_path: string
  normalized_path: string
  size_bytes: number
  state: string
  reason_code: string
}

export interface ArchiveManifest {
  manifest_id: number
  status: string
  entry_count: number
  expires_at: number
  entries: ArchiveEntry[]
}

export interface MaterialJob {
  job_id: number
  document_id: number
  version_id: number
  status: string
  error_code: string
}

export interface RecycleItem {
  document_id: number
  filename: string
  deleted_at: number
  deleted_by: number
  restore_until: number
}

export interface OrganizationItem {
  id: number
  document_id: number
  target_path: string
  document_type: string
  status: string
  reason_code: string
  confidence: number
}

export async function uploadMaterial(file: File): Promise<any> {
  const form = new FormData()
  form.append('file', file)
  const res = await api.post('/materials/upload', form, { headers: headers() })
  return res.data
}

export async function getArchiveManifest(manifestId: number): Promise<ArchiveManifest> {
  const res = await api.get(`/materials/archives/${manifestId}`, { headers: headers() })
  return res.data
}

export async function confirmArchive(
  manifestId: number,
  selectedEntryIds: number[],
  expectedVersion: number,
  idempotencyKey: string
): Promise<any> {
  const res = await api.post(
    `/materials/archives/${manifestId}/confirm`,
    { selected_entry_ids: selectedEntryIds, expected_version: expectedVersion, idempotency_key: idempotencyKey },
    { headers: headers() }
  )
  return res.data
}

export async function getMaterialJob(jobId: number): Promise<MaterialJob> {
  const res = await api.get(`/materials/jobs/${jobId}`, { headers: headers() })
  return res.data
}

export async function deleteMaterial(documentId: number): Promise<any> {
  const res = await api.delete(`/materials/${documentId}`, { headers: headers() })
  return res.data
}

export async function restoreMaterial(documentId: number, expectedMetadataVersion: number): Promise<any> {
  const res = await api.post(
    `/materials/${documentId}/restore`,
    { expected_metadata_version: expectedMetadataVersion },
    { headers: headers() }
  )
  return res.data
}

export async function listRecycle(): Promise<{ items: RecycleItem[] }> {
  const res = await api.get('/materials/recycle', { headers: headers() })
  return res.data
}

export async function locateMaterial(text: string, topK = 10): Promise<any> {
  const res = await api.post('/materials/locate', { text, top_k: topK }, { headers: headers() })
  return res.data
}

export async function organizationPreview(text: string): Promise<{ proposal_id: number; items: OrganizationItem[] }> {
  const res = await api.post('/materials/organization/preview', { text }, { headers: headers() })
  return res.data
}

export async function organizationConfirm(
  proposalId: number,
  selectedItemIds: number[],
  idempotencyKey: string
): Promise<any> {
  const res = await api.post(
    `/materials/organization/${proposalId}/confirm`,
    { selected_item_ids: selectedItemIds, idempotency_key: idempotencyKey },
    { headers: headers() }
  )
  return res.data
}

export async function organizationRollback(batchId: number, idempotencyKey: string): Promise<any> {
  const res = await api.post(
    `/materials/organization/batches/${batchId}/rollback`,
    { idempotency_key: idempotencyKey },
    { headers: headers() }
  )
  return res.data
}
