import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '../auth'
import { useChatStore } from '../chat'
import { useFilesStore } from '../files'

const { registeredSessionReset } = vi.hoisted(() => ({
  registeredSessionReset: { current: null as null | (() => void) }
}))

vi.mock('../../utils/api', () => ({
  default: { post: vi.fn(), get: vi.fn() },
  streamRequest: vi.fn(),
  registerSessionResetHandler: vi.fn((handler: () => void) => {
    registeredSessionReset.current = handler
  })
}))

import api from '../../utils/api'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

function seedPrivateAccountState() {
  const chat = useChatStore()
  chat.conversations = [
    { id: 1, title: 'Alice private conversation', mode: 'file', created_at: '2026-01-01' }
  ]
  chat.currentConvId = 1
  chat.messages = [
    { role: 'user', content: 'alice secret question' },
    { role: 'assistant', content: 'alice secret answer' }
  ]
  chat.selectFile('alice-secret.pdf')
  chat.suggestedQuestions = ['alice follow-up']

  const files = useFilesStore()
  files.files = [
    { name: 'alice-secret.pdf', size: 10, size_human: '10 B', ext: '.pdf' }
  ]
  files.archiveManifestId = 7
  files.archiveEntries = [{ id: 1, relative_path: 'alice/secret.pdf' }]
  files.archiveVisible = true
  files.recycleItems = [{ document_id: 1, filename: 'alice-deleted.pdf' }]
  files.recycleVisible = true
  files.organizationItems = [{ id: 2, target_path: 'alice/secret' }]
  files.organizationProposalId = 9
  files.organizationVisible = true
}

function expectPrivateAccountStateCleared() {
  const chat = useChatStore()
  expect(chat.conversations).toEqual([])
  expect(chat.currentConvId).toBeNull()
  expect(chat.messages).toEqual([])
  expect(chat.selectedFile).toBeNull()
  expect(chat.suggestedQuestions).toEqual([])

  const files = useFilesStore()
  expect(files.files).toEqual([])
  expect(files.archiveManifestId).toBeNull()
  expect(files.archiveEntries).toEqual([])
  expect(files.archiveVisible).toBe(false)
  expect(files.recycleItems).toEqual([])
  expect(files.recycleVisible).toBe(false)
  expect(files.organizationItems).toEqual([])
  expect(files.organizationProposalId).toBeNull()
  expect(files.organizationVisible).toBe(false)
}

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

  it('does not restore a session when /me resolves after logout', async () => {
    const pendingMe = deferred<any>()
    vi.mocked(api.get).mockReturnValueOnce(pendingMe.promise)
    vi.mocked(api.post).mockResolvedValue({ data: undefined } as any)
    const store = useAuthStore()

    const probe = store.fetchUser()
    await store.logout()
    pendingMe.resolve({ data: { id: 1, username: 'alice' } })
    await probe

    expect(store.user).toBeNull()
    expect(store.sessionChecked).toBe(true)
  })

  it('does not let an old /me response overwrite the account that just logged in', async () => {
    const oldMe = deferred<any>()
    vi.mocked(api.get)
      .mockReturnValueOnce(oldMe.promise)
      .mockResolvedValueOnce({ data: { id: 2, username: 'bob' } } as any)
    vi.mocked(api.post).mockResolvedValue({ data: { username: 'bob' } } as any)
    const store = useAuthStore()

    const oldProbe = store.fetchUser()
    await store.login('bob', 'password')
    oldMe.resolve({ data: { id: 1, username: 'alice' } })
    await oldProbe

    expect(store.user).toEqual({ id: 2, username: 'bob' })
    expect(store.sessionChecked).toBe(true)
  })

  it('deduplicates concurrent session probes in the same session generation', async () => {
    const pendingMe = deferred<any>()
    vi.mocked(api.get).mockReturnValueOnce(pendingMe.promise)
    const store = useAuthStore()

    const first = store.fetchUser()
    const second = store.fetchUser()
    expect(api.get).toHaveBeenCalledTimes(1)
    pendingMe.resolve({ data: { id: 1, username: 'alice' } })
    await Promise.all([first, second])

    expect(store.user?.username).toBe('alice')
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

  // ── E-02: clear previous account data ───────────────────
  it('clears the previous account chat and files state before a new login hydrates', async () => {
    seedPrivateAccountState()
    vi.mocked(api.post).mockResolvedValue({ data: { username: 'bob' } } as any)
    vi.mocked(api.get).mockResolvedValue({ data: { id: 2, username: 'bob' } } as any)

    const store = useAuthStore()
    await store.login('bob', 'password')

    expectPrivateAccountStateCleared()
    expect(store.user?.username).toBe('bob')
  })

  it('clears chat and files state when account A logs out', async () => {
    seedPrivateAccountState()
    vi.mocked(api.post).mockResolvedValue({ data: undefined } as any)

    const store = useAuthStore()
    store.user = { id: 1, username: 'alice' }
    await store.logout()

    expect(api.post).toHaveBeenCalledWith('/logout')
    expect(store.user).toBeNull()
    expectPrivateAccountStateCleared()
  })

  it('still clears client state when the logout request fails', async () => {
    seedPrivateAccountState()
    vi.mocked(api.post).mockRejectedValue(new Error('network down'))

    const store = useAuthStore()
    store.user = { id: 1, username: 'alice' }

    await expect(store.logout()).rejects.toThrow('network down')

    expect(store.user).toBeNull()
    expectPrivateAccountStateCleared()
  })

  it('registers the shared reset routine used by 401/403 interceptor failures', () => {
    expect(registeredSessionReset.current).toBeTypeOf('function')

    seedPrivateAccountState()
    registeredSessionReset.current!()

    expectPrivateAccountStateCleared()
  })

  it('keeps auth valid when the shared reset is triggered by a permission failure', () => {
    const store = useAuthStore()
    store.user = { id: 3, username: 'still-authorized' }
    store.sessionChecked = true

    registeredSessionReset.current!()

    expect(store.user).toEqual({ id: 3, username: 'still-authorized' })
    expect(store.isAuthenticated).toBe(true)
  })
})
