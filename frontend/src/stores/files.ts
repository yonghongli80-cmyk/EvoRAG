import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../utils/api'
import {
  confirmArchive,
  getArchiveManifest,
  listRecycle,
  organizationConfirm,
  organizationPreview,
  restoreMaterial
} from '../api/materials'

export interface FileInfo {
  name: string
  size: number
  size_human: string
  ext: string
  is_public?: boolean
  protected?: boolean
  owner_id?: number
  is_owner?: boolean
  in_kb?: boolean
  downloadable?: boolean
  document_id?: number
  version_id?: number
}

export const useFilesStore = defineStore('files', () => {
  const files = ref<FileInfo[]>([])
  const loading = ref(false)
  let _loaded = false

  // Material lifecycle state (archive confirmation, recycle, organization).
  const archiveManifestId = ref<number | null>(null)
  const archiveEntries = ref<any[]>([])
  const archiveVisible = ref(false)
  const recycleItems = ref<any[]>([])
  const recycleVisible = ref(false)
  const organizationItems = ref<any[]>([])
  const organizationProposalId = ref<number | null>(null)
  const organizationVisible = ref(false)

  async function loadFiles(force = false) {
    if (_loaded && !force) return
    loading.value = true
    try {
      const res = await api.get('/files')
      files.value = Array.isArray(res.data?.files) ? res.data.files : []
      _loaded = true
    } catch (err: any) {
      // Don't mark as loaded on failure so next call retries
      _loaded = false
      console.error('[files] loadFiles failed:', err?.response?.status, err?.message)
    } finally {
      loading.value = false
    }
  }

  async function uploadFile(file: File) {
    const formData = new FormData()
    formData.append('file', file)

    const res = await api.post('/materials/upload', formData)

    if (res.data?.status === 'awaiting_archive_confirmation' && res.data?.manifest_id) {
      archiveManifestId.value = res.data.manifest_id
      await loadArchiveManifest(res.data.manifest_id)
      archiveVisible.value = true
      return res.data
    }

    await loadFiles(true)
    return res.data
  }

  async function loadArchiveManifest(manifestId: number) {
    const data = await getArchiveManifest(manifestId)
    archiveEntries.value = data.entries || []
    return data
  }

  async function confirmArchiveImport(selectedIds: number[]) {
    if (archiveManifestId.value == null) return
    const key = `arc-${Date.now()}`
    await confirmArchive(archiveManifestId.value, selectedIds, 0, key)
    archiveVisible.value = false
    archiveManifestId.value = null
    archiveEntries.value = []
    await loadFiles(true)
  }

  async function openRecycle() {
    const data = await listRecycle()
    recycleItems.value = data.items || []
    recycleVisible.value = true
  }

  function closeRecycle() {
    recycleVisible.value = false
  }

  async function restoreDocument(documentId: number) {
    const expected = 0
    await restoreMaterial(documentId, expected)
    await openRecycle()
    await loadFiles(true)
  }

  async function previewOrganization(text: string) {
    const data = await organizationPreview(text)
    organizationProposalId.value = data.proposal_id
    organizationItems.value = data.items || []
    organizationVisible.value = true
  }

  async function confirmOrganization(selectedIds: number[]) {
    if (organizationProposalId.value == null) return
    const key = `org-${Date.now()}`
    await organizationConfirm(organizationProposalId.value, selectedIds, key)
    organizationVisible.value = false
    organizationProposalId.value = null
    organizationItems.value = []
    await loadFiles(true)
  }

  function closeOrganization() {
    organizationVisible.value = false
  }

  /**
   * Drop all account-scoped state. Called on login/logout and when the API
   * answers 401/403 so the next account cannot see the previous one's data.
   */
  function reset() {
    files.value = []
    loading.value = false
    _loaded = false
    archiveManifestId.value = null
    archiveEntries.value = []
    archiveVisible.value = false
    recycleItems.value = []
    recycleVisible.value = false
    organizationItems.value = []
    organizationProposalId.value = null
    organizationVisible.value = false
  }

  async function deleteFile(name: string) {
    await api.delete(`/files/${encodeURIComponent(name)}`)
    await loadFiles(true)
  }

  return {
    files,
    loading,
    reset,
    loadFiles,
    uploadFile,
    deleteFile,
    archiveManifestId,
    archiveEntries,
    archiveVisible,
    loadArchiveManifest,
    confirmArchiveImport,
    recycleItems,
    recycleVisible,
    openRecycle,
    closeRecycle,
    restoreDocument,
    organizationItems,
    organizationProposalId,
    organizationVisible,
    previewOrganization,
    confirmOrganization,
    closeOrganization
  }
})
