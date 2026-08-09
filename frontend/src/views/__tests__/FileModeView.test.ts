import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../stores/auth', () => ({
  useAuthStore: vi.fn(() => ({
    token: 'test-token',
    getAuthHeaders: () => ({ Authorization: 'Bearer test-token' })
  }))
}))

vi.mock('../../utils/api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn()
  }
}))

import api from '../../utils/api'
import { useChatStore } from '../../stores/chat'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('FileModeView auto-create conversation behavior', () => {
  it('chatStore.createConversation posts with mode=file', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { id: 1, title: 'New', mode: 'file', created_at: '2026-01-01' }
    } as any)

    const store = useChatStore()
    const result = await store.createConversation('file')

    expect(api.post).toHaveBeenCalledWith(
      '/conversations',
      { mode: 'file' }
    )
    expect(result.mode).toBe('file')
    expect(store.currentConvId).toBe(1)
  })

  it('chatStore.createConversation posts with mode=kb', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { id: 2, title: 'New', mode: 'kb', created_at: '2026-01-01' }
    } as any)

    const store = useChatStore()
    const result = await store.createConversation('kb')

    expect(api.post).toHaveBeenCalledWith(
      '/conversations',
      { mode: 'kb' }
    )
    expect(result.mode).toBe('kb')
  })

  it('currentConversation is null when no conversation selected', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.loadConversations()

    expect(store.currentConversation).toBeFalsy()
  })

  it('currentConversation returns the active conversation', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: [{ id: 10, title: 'Active', mode: 'file', created_at: '2026-01-01' }]
    } as any)

    const store = useChatStore()
    await store.loadConversations()
    // Manually set currentConvId to simulate selection
    store.currentConvId = 10

    expect(store.currentConversation).toBeDefined()
    expect(store.currentConversation!.id).toBe(10)
  })
})
