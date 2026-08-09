import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../utils/api'
import { useAuthStore } from './auth'

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
      const auth = useAuthStore()
      const res = await api.get('/files', {
        headers: auth.getAuthHeaders()
      })
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
    const auth = useAuthStore()
    const formData = new FormData()
    formData.append('file', file)

    const res = await api.post('/upload', formData, {
      headers: auth.getAuthHeaders()
    })

    await loadFiles(true)
    return res.data
  }

  async function deleteFile(name: string) {
    const auth = useAuthStore()
    await api.delete(`/files/${encodeURIComponent(name)}`, {
      headers: auth.getAuthHeaders()
    })
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
