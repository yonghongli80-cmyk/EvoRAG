import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { loadRouteLocation } from 'vue-router'
import router from '../index'
import { useAuthStore } from '../../stores/auth'

vi.mock('../../utils/api', () => ({
  default: { post: vi.fn(), get: vi.fn() }
}))

import api from '../../utils/api'

// vue-router resolves and caches each matched route's lazily-imported component
// while it confirms a navigation. Paying that cost for the first time inside the
// `beforeEach` hook below meant the hook absorbed the cold Vite transform of
// LoginView.vue (plus the Element Plus barrel it pulls in), which reached 10-48s
// on a loaded machine and intermittently exceeded the hook budget, failing this
// file at random (reported against the hook, i.e. next to
// `setActivePinia(createPinia())`). Resolve the components the navigations below
// need once, here, off the timed path; the hooks then only exercise the
// navigation guards. Sequential, not parallel: concurrent loads of the same
// dependency graph make Vite transform it several times over.
for (const path of ['/login', '/files', '/organization']) {
  await loadRouteLocation(router.resolve(path))
}

describe('router navigation guards', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    await router.push('/login')
  })

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
