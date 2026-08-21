import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '../auth'

vi.mock('../../utils/api', () => ({
  default: { post: vi.fn(), get: vi.fn() }
}))

import api from '../../utils/api'

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
})

describe('cookie auth store', () => {
  it('logs in through the HttpOnly session without storing a token', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { username: 'testuser' } } as any)
    vi.mocked(api.get).mockResolvedValue({ data: { id: 1, username: 'testuser' } } as any)
    const store = useAuthStore()

    await store.login('testuser', 'password')

    expect(store.user).toEqual({ id: 1, username: 'testuser' })
    expect(store.isAuthenticated).toBe(true)
    expect(localStorage.getItem('rag_token')).toBeNull()
  })

  it('discovers an existing cookie session on refresh', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { id: 2, username: 'alice' } } as any)
    const store = useAuthStore()

    await store.fetchUser()

    expect(store.user?.username).toBe('alice')
    expect(store.sessionChecked).toBe(true)
  })

  it('treats a 401 session probe as signed out', async () => {
    const error = Object.assign(new Error('Unauthorized'), { response: { status: 401 } })
    vi.mocked(api.get).mockRejectedValue(error)
    const store = useAuthStore()

    await store.fetchUser()

    expect(store.user).toBeNull()
    expect(store.isAuthenticated).toBe(false)
    expect(store.sessionChecked).toBe(true)
  })

  it('logs out on the server and clears user state', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: undefined } as any)
    const store = useAuthStore()
    store.user = { id: 1, username: 'bob' }

    await store.logout()

    expect(api.post).toHaveBeenCalledWith('/logout')
    expect(store.user).toBeNull()
  })

  it('returns no bearer header for browser requests', () => {
    const store = useAuthStore()
    localStorage.setItem('rag_token', 'must-not-be-used')

    expect(store.getAuthHeaders()).toEqual({})
  })
})
