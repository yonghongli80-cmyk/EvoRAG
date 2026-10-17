import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import router from '../index'
import { useAuthStore } from '../../stores/auth'

vi.mock('../../utils/api', () => ({
  default: { post: vi.fn(), get: vi.fn() }
}))

import api from '../../utils/api'

describe('router navigation guards', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    await router.push('/login')
  }, 30000)

  it('redirects unauthenticated user to /login', async () => {
    vi.mocked(api.get).mockRejectedValue({ response: { status: 401 } })
    await router.push('/files')
    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('redirects non-admin away from admin-only routes', async () => {
    const authStore = useAuthStore()
    authStore.user = { id: 1, username: 'regular_user', is_admin: false }
    authStore.sessionChecked = true

    await router.push('/organization')
    expect(router.currentRoute.value.path).toBe('/files')
  })

  it('allows admin to access admin-only routes', async () => {
    const authStore = useAuthStore()
    authStore.user = { id: 2, username: 'admin_user', is_admin: true }
    authStore.sessionChecked = true

    await router.push('/organization')
    expect(router.currentRoute.value.path).toBe('/organization')
  })
})
