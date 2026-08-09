import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../utils/api'

interface FileInfo {
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
}

export const useFilesStore = defineStore('files', () => {
  const files = ref<FileInfo[]>([])
  const loading = ref(false)
  let _loaded = false

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

    const res = await api.post('/upload', formData)

    await loadFiles(true)
    return res.data
  }

  async function deleteFile(name: string) {
    await api.delete(`/files/${encodeURIComponent(name)}`)
    await loadFiles(true)
  }

  return {
    files,
    loading,
    loadFiles,
    uploadFile,
    deleteFile
  }
})
