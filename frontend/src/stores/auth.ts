import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../utils/api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('rag_token') || '')
  const user = ref<{ id: number; username: string; is_admin?: boolean } | null>(null)

  const isAuthenticated = computed(() => !!token.value)

  async function login(username: string, password: string) {
    const res = await api.post('/login', { username, password })
    token.value = res.data.token
    localStorage.setItem('rag_token', token.value)
    // Fetch user info immediately after login
    user.value = null
    await fetchUser()
    return res.data
  }

  async function register(username: string, password: string) {
    const res = await api.post('/register', { username, password })
    token.value = res.data.token
    localStorage.setItem('rag_token', token.value)
    // Fetch user info immediately after register
    user.value = null
    await fetchUser()
    return res.data
  }

  async function fetchUser() {
    if (!token.value) return
    // Allow re-fetch if user is null (e.g. after page refresh)
    if (user.value) return
    try {
      const res = await api.get('/me')
      user.value = res.data
    } catch (err: any) {
      if (err.response?.status === 401) {
        logout()
      }
      // Other errors (network, 500) — keep token, user can retry
    }
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('rag_token')
  }

  function getAuthHeaders() {
    return { Authorization: `Bearer ${token.value}` }
  }

  return {
    token,
    user,
    isAuthenticated,
    login,
    register,
    fetchUser,
    logout,
    getAuthHeaders
  }
})
