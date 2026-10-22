import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import api, { registerSessionResetHandler } from '../utils/api'
import { useChatStore } from './chat'
import { useFilesStore } from './files'

interface AuthUser {
  id: number
  username: string
  is_admin?: boolean
  profile?: {
    person_code: string
    display_name: string
    job_title: string
    primary_org: { code: string; name: string } | null
    secondary_orgs: { code: string; name: string }[]
  }
}

/**
 * Drop every account-scoped client store. Logout and login both funnel through
 * this routine, and the API layer reuses it for 401/403 responses so a shared
 * browser cannot leak account A's conversations/files to account B.
 */
export function resetClientSession(): void {
  useChatStore().reset()
  useFilesStore().reset()
}

// Registered at module load; auth is imported by the router before any guarded
// view mounts. The typeof guard keeps partial test mocks of utils/api working.
if (typeof registerSessionResetHandler === 'function') {
  registerSessionResetHandler(resetClientSession)
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  const sessionChecked = ref(false)
  const isAuthenticated = computed(() => user.value !== null)

  async function login(username: string, password: string) {
    const response = await api.post('/login', { username, password })
    user.value = null
    sessionChecked.value = false
    resetClientSession()
    await fetchUser(true)
    return response.data
  }

  async function fetchUser(force = false) {
    if (user.value && !force) {
      sessionChecked.value = true
      return
    }
    try {
      const response = await api.get('/me')
      user.value = response.data
    } catch (error: any) {
      if (error.response?.status === 401) user.value = null
    } finally {
      sessionChecked.value = true
    }
  }

  async function logout() {
    try {
      await api.post('/logout')
    } finally {
      user.value = null
      sessionChecked.value = true
      resetClientSession()
    }
  }

  function getAuthHeaders() {
    return {}
  }

  return {
    user,
    sessionChecked,
    isAuthenticated,
    login,
    fetchUser,
    logout,
    resetClientSession,
    getAuthHeaders,
  }
})
