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

// Invalidates /me responses that began before logout, account switching, or a
// shared client-session reset (including an API 401/403 response).
let authSessionGeneration = 0
let unauthorizedProbeGeneration: number | null = null

/**
 * Drop every account-scoped client store. Logout and login both funnel through
 * this routine, and the API layer reuses it for 401/403 responses so a shared
 * browser cannot leak account A's conversations/files to account B.
 */
export function resetClientSession(): void {
  authSessionGeneration += 1
  useChatStore().reset()
  useFilesStore().reset()
}

// Registered at module load; auth is imported by the router before any guarded
// view mounts. The typeof guard keeps partial test mocks of utils/api working.
if (typeof registerSessionResetHandler === 'function') {
  registerSessionResetHandler((status, request) => {
    const requestGeneration = request?.authSessionGeneration ?? request?.authProbeGeneration
    if (requestGeneration !== undefined && requestGeneration !== authSessionGeneration) {
      return false
    }
    if (status === 401 && request?.authProbeGeneration !== undefined) {
      unauthorizedProbeGeneration = request.authProbeGeneration
    }
    resetClientSession()
    return true
  }, () => authSessionGeneration)
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  const sessionChecked = ref(false)
  const isAuthenticated = computed(() => user.value !== null)
  let pendingFetch: Promise<void> | null = null
  let pendingFetchGeneration = -1

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
    if (force) {
      authSessionGeneration += 1
      pendingFetch = null
    }
    const generation = authSessionGeneration
    if (pendingFetch && pendingFetchGeneration === generation) return pendingFetch

    let request!: Promise<void>
    request = (async () => {
      try {
        const response = await api.get('/me', { authProbeGeneration: generation } as any)
        if (generation === authSessionGeneration) user.value = response.data
      } catch (error: any) {
        if (
          error.response?.status === 401
          && (
            generation === authSessionGeneration
            || (
              generation === unauthorizedProbeGeneration
              && authSessionGeneration === generation + 1
            )
          )
        ) {
          user.value = null
          sessionChecked.value = true
          if (generation === authSessionGeneration) authSessionGeneration += 1
          if (generation === unauthorizedProbeGeneration) unauthorizedProbeGeneration = null
        }
      } finally {
        if (generation === authSessionGeneration) sessionChecked.value = true
        if (pendingFetch === request) {
          pendingFetch = null
          pendingFetchGeneration = -1
        }
      }
    })()
    pendingFetch = request
    pendingFetchGeneration = generation
    return request
  }

  async function logout() {
    authSessionGeneration += 1
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
