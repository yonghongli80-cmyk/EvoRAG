import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import type { AxiosAdapter } from 'axios'
import { useChatStore } from '../chat'

vi.mock('../../utils/api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn()
  },
  isAbortError: (error: any) =>
    error?.name === 'AbortError' ||
    error?.name === 'CanceledError' ||
    error?.code === 'ERR_CANCELED' ||
    error?.code === 'ABORT_ERR',
  streamRequest: vi.fn(async (path: string, init: RequestInit) => {
    const response = await fetch(`/api${path}`, init)
    if (!response.ok) {
      const data = await response.json().catch(() => ({ detail: '请求失败' }))
      const error: any = new Error(data.detail || '请求失败')
      error.response = { status: response.status, data }
      throw error
    }
    return response
  })
}))

vi.mock('../auth', () => ({
  useAuthStore: vi.fn(() => ({
    token: 'test-token',
    getAuthHeaders: () => ({ Authorization: 'Bearer test-token' })
  }))
}))

import api, { streamRequest } from '../../utils/api'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('chat store', () => {
  it('resetModeState clears conversation state only when mode changes', () => {
    const store = useChatStore()
    store.resetModeState('file')
    store.currentConvId = 7
    store.messages = [{ role: 'user', content: 'file question' }]
    store.selectFile('file.pdf')

    store.resetModeState('file')
    expect(store.currentConvId).toBe(7)
    expect(store.messages).toHaveLength(1)
    expect(store.selectedFile).toBe('file.pdf')

    store.resetModeState('kb')
    expect(store.currentMode).toBe('kb')
    expect(store.currentConvId).toBeNull()
    expect(store.messages).toEqual([])
    expect(store.selectedFile).toBeNull()
  })

  it('selectConversation failure keeps the previous conversation and file selection', async () => {
    const store = useChatStore()
    store.currentConvId = 1
    store.messages = [{ role: 'assistant', content: 'existing answer' }]
    store.selectFile('existing.pdf')
    vi.mocked(api.get).mockRejectedValue(new Error('network'))

    await store.selectConversation(2)

    expect(store.currentConvId).toBe(1)
    expect(store.messages).toEqual([{ role: 'assistant', content: 'existing answer' }])
    expect(store.selectedFile).toBe('existing.pdf')
  })

  // ── loadConversations ──────────────────────────────────
  it('loadConversations fills the list', async () => {
    const convos = [
      { id: 1, title: 'First', mode: 'file', created_at: '2026-01-01' },
      { id: 2, title: 'Second', mode: 'file', created_at: '2026-01-02' }
    ]
    vi.mocked(api.get).mockResolvedValue({ data: convos } as any)

    const store = useChatStore()
    await store.loadConversations()

    expect(store.conversations).toEqual(convos)
  })

  it('loadConversations filters by mode on server side', async () => {
    const kbConvos = [
      { id: 10, title: 'KB Chat', mode: 'kb', created_at: '2026-03-01' }
    ]
    vi.mocked(api.get).mockResolvedValue({ data: kbConvos } as any)

    const store = useChatStore()
    await store.loadConversations('kb')

    expect(api.get).toHaveBeenCalledWith(
      '/conversations',
      expect.objectContaining({ params: { mode: 'kb' } })
    )
    expect(store.conversations).toEqual(kbConvos)
    expect(store.conversations).toHaveLength(1)
    expect(store.conversations[0].mode).toBe('kb')
  })

  it('loadConversations does not pass mode param when not provided', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.loadConversations()

    expect(api.get).toHaveBeenCalledWith(
      '/conversations',
      expect.objectContaining({ params: {} })
    )
  })

  it('loadConversations clears previous state before fetching', async () => {
    const store = useChatStore()
    // Pre-populate with stale data from another mode
    store.conversations = [
      { id: 1, title: 'Old', mode: 'file', created_at: '2026-01-01' }
    ]
    store.currentConvId = 1
    store.messages = [{ role: 'user', content: 'old' }]

    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)
    await store.loadConversations('kb')

    expect(store.conversations).toEqual([])
    expect(store.currentConvId).toBeNull()
    expect(store.messages).toEqual([])
  })

  it('loadConversations silently fails on error', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('network'))

    const store = useChatStore()
    await store.loadConversations()

    expect(store.conversations).toEqual([])
  })

  // ── createConversation ──────────────────────────────────
  it('createConversation adds to list and sets currentConvId', async () => {
    const newConv = { id: 3, title: 'New', mode: 'file', created_at: '2026-01-03' }
    vi.mocked(api.post).mockResolvedValue({ data: newConv } as any)

    const store = useChatStore()
    const result = await store.createConversation()

    expect(result).toEqual(newConv)
    expect(store.conversations[0]).toEqual(newConv)
    expect(store.currentConvId).toBe(3)
  })

  it('createConversation passes mode parameter', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { id: 4, title: 'KB', mode: 'kb', created_at: '2026-04-01' }
    } as any)

    const store = useChatStore()
    await store.createConversation('kb')

    expect(api.post).toHaveBeenCalledWith(
      '/conversations',
      { mode: 'kb' }
    )
  })

  it('createConversation clears messages and selectedFile', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { id: 5, title: 'New', mode: 'file', created_at: '2026-05-01' }
    } as any)

    const store = useChatStore()
    store.messages = [{ role: 'user', content: 'stale' }]
    store.selectFile('old.pdf')

    await store.createConversation()

    expect(store.messages).toEqual([])
    expect(store.selectedFile).toBeNull()
  })

  // ── deleteConversation ─────────────────────────────────
  it('deleteConversation removes from list', async () => {
    const store = useChatStore()
    store.conversations = [
      { id: 1, title: 'A', mode: 'file', created_at: '2026-01-01' },
      { id: 2, title: 'B', mode: 'file', created_at: '2026-01-02' }
    ]
    store.currentConvId = 1

    vi.mocked(api.delete).mockResolvedValue({} as any)

    await store.deleteConversation(1)

    expect(store.conversations.find(c => c.id === 1)).toBeUndefined()
    expect(store.currentConvId).toBeNull()
  })

  it('deleteConversation does not change currentConvId when deleting non-current conversation', async () => {
    const store = useChatStore()
    store.conversations = [
      { id: 1, title: 'A', mode: 'file', created_at: '2026-01-01' },
      { id: 2, title: 'B', mode: 'file', created_at: '2026-01-02' }
    ]
    store.currentConvId = 1

    vi.mocked(api.delete).mockResolvedValue({} as any)

    await store.deleteConversation(2)

    expect(store.conversations.find(c => c.id === 2)).toBeUndefined()
    expect(store.currentConvId).toBe(1)
    expect(store.conversations).toHaveLength(1)
  })

  // ── selectConversation ─────────────────────────────────
  it('selectConversation sets currentConvId', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.selectConversation(5)

    expect(store.currentConvId).toBe(5)
  })

  it('selectConversation loads messages from API', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: [
        { id: 1, role: 'user', content: 'Hello' },
        { id: 2, role: 'assistant', content: 'Hi there' }
      ]
    } as any)

    const store = useChatStore()
    await store.selectConversation(1)

    expect(store.messages).toHaveLength(2)
    expect(store.messages[0]).toEqual({
      id: 1, role: 'user', content: 'Hello', sources: [], feedback: undefined
    })
    expect(store.messages[1]).toEqual({
      id: 2, role: 'assistant', content: 'Hi there', sources: [], feedback: undefined
    })
  })

  it('selectConversation calls GET /conversations/{id}/messages', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.selectConversation(7)

    expect(api.get).toHaveBeenCalledWith(
      '/conversations/7/messages',
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    )
  })

  // ── sendMessage ─────────────────────────────────────────
  it('sendMessage routes /query/stream through the unified stream client', async () => {
    const mockResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: new TextEncoder().encode('data: {"type":"token","content":"Hi"}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.sendMessage('Hello')

    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/query/stream',
      expect.objectContaining({ method: 'POST' })
    )
    expect(streamRequest).toHaveBeenCalledWith(
      '/query/stream',
      expect.objectContaining({ method: 'POST' })
    )
    expect(store.messages).toHaveLength(2)
    expect(store.messages[0].role).toBe('user')
    expect(store.messages[1].role).toBe('assistant')
  })

  it('sendMessage carries the selected knowledge base ID', async () => {
    const mockResponse = {
      ok: true,
      body: { getReader: () => ({ read: vi.fn().mockResolvedValue({ done: true }) }) }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)
    vi.mocked(api.post).mockResolvedValue({ data: { id: 1, mode: 'kb' } } as any)

    const store = useChatStore()
    await store.sendMessage('leave policy', 'kb', 'kb_hr')

    expect(JSON.parse(String(vi.mocked(globalThis.fetch).mock.calls[0][1]?.body))).toMatchObject({
      question: 'leave policy',
      kb_id: 'kb_hr'
    })
  })

  it('sendMessage streams tokens into assistant message', async () => {
    const encoder = new TextEncoder()
    const mockResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"token","content":"Hello "}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"token","content":"world"}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"sources","sources":[{"doc_name":"test.pdf","chunk_index":0}]}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.sendMessage('Question')

    const assistantMsg = store.messages[1]
    expect(assistantMsg.content).toBe('Hello world')
    expect(assistantMsg.sources).toEqual([{ doc_name: 'test.pdf', chunk_index: 0 }])
  })

  it('stores structured result values without numeric coercion', async () => {
    const encoder = new TextEncoder()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, body: { getReader: () => ({ read: vi.fn()
      .mockResolvedValueOnce({ done: false, value: encoder.encode('data: {"type":"structured_result","kind":"structured_result","facts":[{"label":"销售额 sum","value":"100.00"}],"sources":[],"rows_scanned":42}\n') })
      .mockResolvedValueOnce({ done: true, value: undefined }) }) } } as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)
    const store = useChatStore()
    await store.sendMessage('销售额合计', 'kb', 'kb_sales')
    expect(store.messages[1].structuredResult?.facts[0].value).toBe('100.00')
    expect(store.messages[1].structuredResult?.rowsScanned).toBe(42)
  })

  it('sendMessage includes selectedFile as doc_name in payload', async () => {
    const mockResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    store.selectFile('specific.pdf')
    await store.sendMessage('Question')

    const fetchCall = vi.mocked(globalThis.fetch).mock.calls[0]
    const init = fetchCall[1] as RequestInit
    const body = JSON.parse(init.body as string)
    expect(body.doc_name).toBe('specific.pdf')
  })

  it('sendMessage shows error text on non-ok response', async () => {
    const mockResponse = {
      ok: false,
      json: vi.fn().mockResolvedValue({ detail: 'Server error occurred' })
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)

    const store = useChatStore()
    await store.sendMessage('Hello')

    const assistantMsg = store.messages[1]
    expect(assistantMsg.content).toBe('Server error occurred')
  })

  it('sendMessage shows default error when JSON parse fails', async () => {
    const mockResponse = {
      ok: false,
      json: vi.fn().mockRejectedValue(new Error('invalid JSON'))
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)

    const store = useChatStore()
    await store.sendMessage('Hello')

    const assistantMsg = store.messages[1]
    expect(assistantMsg.content).toBe('请求失败')
  })

  it('sendMessage shows data.error text when the error body has no detail', async () => {
    const mockResponse = {
      ok: false,
      json: vi.fn().mockResolvedValue({ error: '模型暂不可用' })
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)

    const store = useChatStore()
    await store.sendMessage('Hello')

    const assistantMsg = store.messages[1]
    expect(assistantMsg.content).toBe('模型暂不可用')
  })

  it('sendMessage shows fallback error on network failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('Network error'))

    const store = useChatStore()
    await store.sendMessage('Hello')

    const assistantMsg = store.messages[1]
    expect(assistantMsg.content).toBe('请求失败，请稍后重试。')
  })

  it('sendMessage sets isStreaming to false after completion', async () => {
    const mockResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn().mockResolvedValue({ done: true, value: undefined })
        })
      }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    expect(store.isStreaming).toBe(false)

    await store.sendMessage('Hello')

    expect(store.isStreaming).toBe(false)
  })

  it('sendMessage updates suggestedQuestions after streaming completes', async () => {
    const encoder = new TextEncoder()
    const mockStreamResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"token","content":"Answer"}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(mockStreamResponse as any)
    vi.mocked(api.post).mockResolvedValue({
      data: { questions: ['Follow-up 1', 'Follow-up 2'] }
    } as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.sendMessage('Question')

    expect(store.suggestedQuestions).toEqual(['Follow-up 1', 'Follow-up 2'])
  })

  it('sendMessage updates conversation title locally after streaming', async () => {
    const encoder = new TextEncoder()
    const mockStreamResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"token","content":"Answer"}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(mockStreamResponse as any)
    vi.mocked(api.post)
      .mockResolvedValueOnce({ data: { title: 'Question' } } as any)
      .mockResolvedValueOnce({ data: { questions: [] } } as any)

    const store = useChatStore()
    // Set up a current conversation with empty title
    store.conversations = [
      { id: 1, title: '', mode: 'file', created_at: '2026-01-01' }
    ]
    store.currentConvId = 1

    await store.sendMessage('Question')

    // Title should be updated locally from the first user message
    expect(store.conversations[0].title).toBe('Question')
  })

  // ── sendFeedback ────────────────────────────────────────
  it('sendFeedback calls /feedback', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {} } as any)

    const store = useChatStore()
    store.messages = [{ id: 10, role: 'assistant', content: 'Answer' }]

    await store.sendFeedback(0, 'positive')

    expect(api.post).toHaveBeenCalledWith(
      '/feedback',
      { message_id: 10, value: 'positive' }
    )
    expect(store.messages[0].feedback).toBe('positive')
  })

  it('sendFeedback toggles feedback off when clicking same value', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {} } as any)

    const store = useChatStore()
    store.messages = [{ id: 10, role: 'assistant', content: 'Answer', feedback: 'positive' }]

    await store.sendFeedback(0, 'positive')

    expect(store.messages[0].feedback).toBeUndefined()
  })

  it('sendFeedback does nothing for invalid message index', async () => {
    const store = useChatStore()
    store.messages = []

    await store.sendFeedback(5, 'positive')

    expect(api.post).not.toHaveBeenCalled()
  })

  // ── regenerate ──────────────────────────────────────────
  it('regenerate calls /regenerate', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {
      answer: 'New answer',
      sources: [{ doc_name: 'new.pdf', chunk_index: 2 }],
      reliability: {
        confidence_level: 'high',
        confidence_reasons: ['fresh'],
        answerability_kind: 'answerable'
      }
    } } as any)

    const store = useChatStore()
    store.currentConvId = 7
    store.messages = [{
      id: 20,
      role: 'assistant',
      content: 'Old',
      sources: [{ doc_name: 'old.pdf', chunk_index: 0 }],
      confidence_level: 'low',
      confidence_reasons: ['stale'],
      answerability_kind: 'insufficient_evidence'
    }]

    await store.regenerate(0)

    expect(api.post).toHaveBeenCalledWith(
      '/regenerate',
      { conversation_id: 7, message_id: 20 },
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    )
    expect(store.messages[0].content).toBe('New answer')
    expect(store.messages[0].sources).toEqual([{ doc_name: 'new.pdf', chunk_index: 2 }])
    expect(store.messages[0].confidence_level).toBe('high')
    expect(store.messages[0].confidence_reasons).toEqual(['fresh'])
    expect(store.messages[0].answerability_kind).toBe('answerable')
  })

  it('regenerate clears stale sources and reliability when response omits them', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { answer: 'New answer' } } as any)
    const store = useChatStore()
    store.currentConvId = 7
    store.messages = [{
      id: 20,
      role: 'assistant',
      content: 'Old',
      sources: [{ doc_name: 'old.pdf', chunk_index: 0 }],
      confidence_level: 'high',
      confidence_reasons: ['stale'],
      answerability_kind: 'answerable'
    }]

    await store.regenerate(0)

    expect(store.messages[0].sources).toEqual([])
    expect(store.messages[0].confidence_level).toBe('unavailable')
    expect(store.messages[0].confidence_reasons).toEqual([])
    expect(store.messages[0].answerability_kind).toBe('answerable')
  })

  it('regenerate does nothing for non-assistant message', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {} } as any)

    const store = useChatStore()
    store.currentConvId = 7
    store.messages = [{ id: 20, role: 'user', content: 'Question' }]

    await store.regenerate(0)

    expect(api.post).not.toHaveBeenCalled()
  })

  it('regenerate does nothing when no current conversation', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {} } as any)

    const store = useChatStore()
    store.messages = [{ id: 20, role: 'assistant', content: 'Old' }]

    await store.regenerate(0)

    expect(api.post).not.toHaveBeenCalled()
  })

  it('regenerate sets isStreaming during and after call', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { answer: 'New' } } as any)

    const store = useChatStore()
    store.currentConvId = 1
    store.messages = [{ id: 1, role: 'assistant', content: 'Old' }]

    expect(store.isStreaming).toBe(false)
    await store.regenerate(0)
    expect(store.isStreaming).toBe(false)
  })

  it('ignores a late successful regenerate response after a newer request takes ownership', async () => {
    const pending: Array<(value: any) => void> = []
    const previousPostImplementation = vi.mocked(api.post).getMockImplementation()
    vi.mocked(api.post).mockImplementation((() => new Promise((resolve) => {
      pending.push(resolve)
    })) as any)

    const store = useChatStore()
    store.currentConvId = 7
    store.messages = [{
      id: 20,
      role: 'assistant',
      content: 'Original answer',
      sources: [{ doc_name: 'original.pdf', chunk_index: 0 }],
      confidence_level: 'low',
      confidence_reasons: ['original'],
      answerability_kind: 'insufficient_evidence'
    }]

    try {
      const oldRequest = store.regenerate(0)
      expect(store.isStreaming).toBe(true)
      store.stopStreaming()

      const newRequest = store.regenerate(0)
      const newRequestId = store.activeRequestId
      expect(store.isStreaming).toBe(true)
      expect(pending).toHaveLength(2)

      pending[0]({ data: {
        answer: 'Late old answer',
        reliability: {
          confidence_level: 'high',
          confidence_reasons: ['old request'],
          answerability_kind: 'answerable'
        }
      } })
      await oldRequest

      expect(store.messages[0]).toMatchObject({
        content: 'Original answer',
        confidence_level: 'low',
        confidence_reasons: ['original'],
        answerability_kind: 'insufficient_evidence'
      })
      expect(store.activeRequestId).toBe(newRequestId)
      expect(store.isStreaming).toBe(true)

      pending[1]({ data: {
        answer: 'Current answer',
        reliability: {
          confidence_level: 'medium',
          confidence_reasons: ['new request'],
          answerability_kind: 'answerable'
        }
      } })
      await newRequest

      expect(store.messages[0]).toMatchObject({
        content: 'Current answer',
        confidence_level: 'medium',
        confidence_reasons: ['new request'],
        answerability_kind: 'answerable'
      })
      expect(store.isStreaming).toBe(false)
      expect(store.activeRequestId).toBeNull()
    } finally {
      if (previousPostImplementation) {
        vi.mocked(api.post).mockImplementation(previousPostImplementation)
      } else {
        vi.mocked(api.post).mockReset()
      }
    }
  })

  it.each(['stop', 'reset', 'switch conversation'] as const)(
    'regenerate aborts its Axios request and ignores a late response after %s',
    async (action) => {
      const { default: realAxios } = await vi.importActual<typeof import('axios')>('axios')
      const store = useChatStore()
      store.currentConvId = 1
      store.messages = [{
        id: 10,
        role: 'assistant',
        content: 'Original answer',
        sources: [{ doc_name: 'original.pdf', chunk_index: 0 }],
        confidence_level: 'low',
        confidence_reasons: ['original'],
        answerability_kind: 'insufficient_evidence'
      }]

      let adapterConfig: Parameters<AxiosAdapter>[0] | undefined
      let resolveDelayedResponse: (() => void) | undefined
      let transportError: unknown
      const previousPostImplementation = vi.mocked(api.post).getMockImplementation()
      vi.mocked(api.post).mockReset()
      vi.mocked(api.post).mockImplementation(((_url: string, _data: unknown, config?: any) => {
        const adapter: AxiosAdapter = (axiosConfig) => {
          adapterConfig = axiosConfig
          return new Promise((resolve) => {
            // This delayed adapter intentionally resolves even after abort so
            // the store must reject a response that no longer owns the UI.
            resolveDelayedResponse = () => resolve({
              data: {
                answer: 'Late answer',
                sources: [{ doc_name: 'late.pdf', chunk_index: 1 }],
                reliability: {
                  confidence_level: 'high',
                  confidence_reasons: ['late'],
                  answerability_kind: 'answerable'
                }
              },
              status: 200,
              statusText: 'OK',
              headers: {},
              config: axiosConfig
            } as any)
          })
        }
        try {
          return realAxios.post('http://unit.test/regenerate', _data, {
            ...config,
            adapter
          }).catch((error) => {
            transportError = error
            throw error
          })
        } catch (error) {
          transportError = error
          throw error
        }
      }) as any)

      const pending = store.regenerate(0)
      try {
        await vi.waitFor(() => {
          if (transportError) throw transportError
          expect(resolveDelayedResponse).toBeDefined()
        })

        if (action === 'stop') {
          store.stopStreaming()
        } else if (action === 'reset') {
          store.reset()
        } else {
          vi.mocked(api.get).mockResolvedValueOnce({
            data: [{ id: 20, role: 'assistant', content: 'Current conversation answer' }]
          } as any)
          await store.selectConversation(2)
        }

        expect(adapterConfig?.signal).toBeInstanceOf(AbortSignal)
        expect(adapterConfig?.signal?.aborted).toBe(true)

        resolveDelayedResponse!()
        await pending

        if (action === 'switch conversation') {
          expect(store.currentConvId).toBe(2)
          expect(store.messages).toEqual([{
            id: 20,
            role: 'assistant',
            content: 'Current conversation answer',
            sources: [],
            feedback: undefined,
            confidence_level: undefined,
            confidence_reasons: undefined,
            answerability_kind: undefined
          }])
        } else if (action === 'reset') {
          expect(store.currentConvId).toBeNull()
          expect(store.messages).toEqual([])
        } else {
          expect(store.messages[0]).toMatchObject({
            content: 'Original answer',
            sources: [{ doc_name: 'original.pdf', chunk_index: 0 }],
            confidence_level: 'low',
            confidence_reasons: ['original'],
            answerability_kind: 'insufficient_evidence'
          })
        }
      } finally {
        if (resolveDelayedResponse) {
          resolveDelayedResponse()
          await pending
        }
        if (previousPostImplementation) {
          vi.mocked(api.post).mockImplementation(previousPostImplementation)
        }
      }
    }
  )

  // ── selectFile ──────────────────────────────────────────
  it('selectFile sets selectedFile', () => {
    const store = useChatStore()
    expect(store.selectedFile).toBeNull()

    store.selectFile('report.pdf')
    expect(store.selectedFile).toBe('report.pdf')
  })

  it('selectFile can set to null to clear selection', () => {
    const store = useChatStore()
    store.selectFile('report.pdf')
    expect(store.selectedFile).toBe('report.pdf')

    store.selectFile(null)
    expect(store.selectedFile).toBeNull()
  })

  // ── conversationsByMode ─────────────────────────────────
  it('conversationsByMode filters conversations by mode', () => {
    const store = useChatStore()
    store.conversations = [
      { id: 1, title: 'File 1', mode: 'file', created_at: '2026-01-01' },
      { id: 2, title: 'KB 1', mode: 'kb', created_at: '2026-01-02' },
      { id: 3, title: 'File 2', mode: 'file', created_at: '2026-01-03' }
    ]

    const fileConvos = store.conversationsByMode('file')
    expect(fileConvos.value).toHaveLength(2)
    expect(fileConvos.value.every(c => c.mode === 'file')).toBe(true)

    const kbConvos = store.conversationsByMode('kb')
    expect(kbConvos.value).toHaveLength(1)
    expect(kbConvos.value[0].mode).toBe('kb')
  })

  it('conversationsByMode returns empty when no matches', () => {
    const store = useChatStore()
    store.conversations = [
      { id: 1, title: 'File 1', mode: 'file', created_at: '2026-01-01' }
    ]

    const analysisConvos = store.conversationsByMode('analysis')
    expect(analysisConvos.value).toHaveLength(0)
  })

  // ── assistant message id from done event ────────────────
  it('stores assistant message id from the done event', async () => {
    const encoder = new TextEncoder()
    const mockResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"token","content":"answer"}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"sources","sources":[{"doc_name":"a.pdf","chunk_index":0}]}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"done","assistant_message_id":42}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.sendMessage('question', 'file')

    expect(store.messages.at(-1)?.id).toBe(42)
    expect(store.messages.at(-1)?.assistantMessageId).toBe(42)
  })

  // ── currentConversation computed ────────────────────────
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
    store.currentConvId = 10

    expect(store.currentConversation).toBeDefined()
    expect(store.currentConversation!.id).toBe(10)
  })

  // ── Multi-model & Arena streaming ────────────────────────
  it('loadAvailableModels fetches models from GET /models', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        default_model: 'deepseek',
        models: [
          { id: 'deepseek', name: 'DeepSeek V3', model: 'deepseek-chat', healthy: true },
          { id: 'qwen', name: '通义千问', model: 'qwen-plus', healthy: true }
        ]
      }
    } as any)

    const store = useChatStore()
    await store.loadAvailableModels()

    expect(store.availableModels).toHaveLength(2)
    expect(store.selectedModel).toBe('deepseek')
  })

  // ── session reset ────────────────────────────────────────
  it('reset clears conversation state and per-conversation file selections', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    store.conversations = [
      { id: 1, title: 'A private title', mode: 'file', created_at: '2026-01-01' }
    ]
    store.currentConvId = 1
    store.messages = [
      { role: 'user', content: 'a private question' },
      { role: 'assistant', content: 'a private answer' }
    ]
    store.selectFile('a-private.pdf')
    store.suggestedQuestions = ['a follow-up']
    store.activeRequestId = 9

    // Selecting another conversation stores 1 -> a-private.pdf in the internal map.
    await store.selectConversation(2)

    store.reset()

    expect(store.conversations).toEqual([])
    expect(store.currentConvId).toBeNull()
    expect(store.messages).toEqual([])
    expect(store.selectedFile).toBeNull()
    expect(store.suggestedQuestions).toEqual([])
    expect(store.isStreaming).toBe(false)

    // The previous account's per-conversation file must not come back.
    await store.selectConversation(1)
    expect(store.selectedFile).toBeNull()
  })

  // ── SSE error events ────────────────────────────────────
  it('turns an SSE error event into a visible failed answer instead of a blank bubble', async () => {
    const encoder = new TextEncoder()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"error","reason":"流式生成失败，请稍后重试"}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    } as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.sendMessage('question')

    const assistantMsg = store.messages.at(-1)!
    expect(assistantMsg.content).toBe('流式生成失败，请稍后重试')
    expect(assistantMsg.content).not.toBe('')
    expect(assistantMsg.error).toBe('流式生成失败，请稍后重试')
    expect(assistantMsg.failed).toBe(true)
    expect(store.isStreaming).toBe(false)
    expect(store.messages).toHaveLength(2)
  })

  it('keeps a partial answer when the stream fails midway and marks it failed', async () => {
    const encoder = new TextEncoder()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"token","content":"Partial answer"}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"error","reason":"生成中断，请重试"}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    } as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.sendMessage('question')

    const assistantMsg = store.messages.at(-1)!
    expect(assistantMsg.content).toBe('Partial answer')
    expect(assistantMsg.error).toBe('生成中断，请重试')
    expect(assistantMsg.failed).toBe(true)
  })

  it('accepts an SSE error event that only carries an error field', async () => {
    const encoder = new TextEncoder()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"error","error":"模型暂不可用"}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    } as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.sendMessage('question')

    const assistantMsg = store.messages.at(-1)!
    expect(assistantMsg.content).toBe('模型暂不可用')
    expect(assistantMsg.error).toBe('模型暂不可用')
    expect(assistantMsg.failed).toBe(true)
  })

  it('keeps an assistant message id supplied by an SSE error event', async () => {
    const encoder = new TextEncoder()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"error","reason":"生成失败","assistant_message_id":77}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    } as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    await store.sendMessage('question')

    const assistantMsg = store.messages.at(-1)!
    expect(assistantMsg.id).toBe(77)
    expect(assistantMsg.assistantMessageId).toBe(77)
  })

  it('sendMessage in arena mode sends models array and parses multiplexed arenaResponses', async () => {
    const encoder = new TextEncoder()
    const mockResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn()
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"token","model":"deepseek","content":"Hello from D"}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"token","model":"qwen","content":"Hello from Q"}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"model_done","model":"deepseek"}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"model_done","model":"qwen"}\n')
            })
            .mockResolvedValueOnce({
              done: false,
              value: encoder.encode('data: {"type":"done"}\n')
            })
            .mockResolvedValueOnce({ done: true, value: undefined })
        })
      }
    }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(mockResponse as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    store.arenaEnabled = true
    store.arenaModels = ['deepseek', 'qwen']

    await store.sendMessage('compare question', 'file')

    const lastMsg = store.messages.at(-1)
    expect(lastMsg?.arenaResponses).toBeDefined()
    expect(lastMsg?.arenaResponses?.deepseek).toBe('Hello from D')
    expect(lastMsg?.arenaResponses?.qwen).toBe('Hello from Q')
  })

  // ── E-05: abortable streaming ───────────────────────────
  it('passes an AbortController signal to the streaming request', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      body: { getReader: () => ({ read: vi.fn().mockResolvedValue({ done: true }) }) }
    } as any)

    const store = useChatStore()
    await store.sendMessage('question')

    const init = vi.mocked(streamRequest).mock.calls.at(-1)![1] as RequestInit
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(init.signal?.aborted).toBe(false)
  })

  it('stopStreaming aborts the request and marks the answer stopped', async () => {
    vi.mocked(streamRequest).mockImplementationOnce(((_path: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        ;(init.signal as AbortSignal).addEventListener('abort', () => {
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
        })
      })
    }) as any)

    const store = useChatStore()
    const pending = store.sendMessage('question')
    expect(store.isStreaming).toBe(true)
    expect(store.activeRequestId).not.toBeNull()

    store.stopStreaming()
    await pending

    const assistant = store.messages.at(-1)!
    expect(assistant.stopped).toBe(true)
    expect(assistant.content).toBe('已停止生成')
    expect(assistant.error).toBeUndefined()
    expect(store.isStreaming).toBe(false)
    expect(store.activeRequestId).toBeNull()
  })

  it('keeps partial output when the user stops mid-stream', async () => {
    const encoder = new TextEncoder()
    let capturedSignal: AbortSignal | undefined
    vi.mocked(streamRequest).mockImplementationOnce(((_path: string, init: RequestInit) => {
      capturedSignal = init.signal as AbortSignal
      return Promise.resolve({
        ok: true,
        body: {
          getReader: () => {
            let first = true
            return {
              read: () => {
                if (first) {
                  first = false
                  return Promise.resolve({
                    done: false,
                    value: encoder.encode('data: {"type":"token","content":"Partial"}\n')
                  })
                }
                return new Promise((_resolve, reject) => {
                  capturedSignal!.addEventListener('abort', () => {
                    reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
                  })
                })
              }
            }
          }
        }
      } as any)
    }) as any)

    const store = useChatStore()
    const pending = store.sendMessage('question')
    await vi.waitFor(() => expect(store.messages.at(-1)?.content).toBe('Partial'))

    store.stopStreaming()
    await pending

    const assistant = store.messages.at(-1)!
    expect(capturedSignal?.aborted).toBe(true)
    expect(assistant.content).toBe('Partial')
    expect(assistant.stopped).toBe(true)
    expect(assistant.error).toBeUndefined()
    expect(assistant.failed).toBeUndefined()
    expect(store.isStreaming).toBe(false)
  })

  it('a stopped request cannot unlock the stream that replaced it', async () => {
    let resolveFirst!: (value: any) => void
    vi.mocked(streamRequest).mockImplementationOnce(() => new Promise((resolve) => {
      resolveFirst = resolve
    }) as any)

    const store = useChatStore()
    const first = store.sendMessage('first')
    store.stopStreaming()

    vi.mocked(streamRequest).mockImplementationOnce(((_path: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        ;(init.signal as AbortSignal).addEventListener('abort', () => {
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
        })
      })
    }) as any)
    const second = store.sendMessage('second')
    expect(store.isStreaming).toBe(true)

    // The abandoned request resolves late with an empty stream.
    resolveFirst({ ok: true, body: { getReader: () => ({ read: vi.fn().mockResolvedValue({ done: true }) }) } })
    await first

    expect(store.isStreaming).toBe(true)

    store.stopStreaming()
    await second
    expect(store.isStreaming).toBe(false)
  })

  it('stops the active stream and never retitles the new conversation on switch', async () => {
    let finishRead!: () => void
    const readPromise = new Promise<{ done: boolean }>((resolve) => {
      finishRead = () => resolve({ done: true })
    })
    vi.mocked(streamRequest).mockImplementationOnce((() => Promise.resolve({
      ok: true,
      body: { getReader: () => ({ read: () => readPromise }) }
    })) as any)
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)
    vi.mocked(api.post).mockResolvedValue({ data: { title: 'should not be used' } } as any)

    const store = useChatStore()
    store.conversations = [{ id: 1, title: '', mode: 'file', created_at: '2026-01-01' }]
    store.currentConvId = 1
    const pending = store.sendMessage('question')

    await store.selectConversation(2)
    finishRead()
    await pending

    expect(store.isStreaming).toBe(false)
    expect(store.currentConvId).toBe(2)
    expect(api.post).not.toHaveBeenCalledWith(
      '/conversations/2/generate-title',
      expect.anything()
    )
  })

  it('ignores a second sendMessage while a stream is active', async () => {
    vi.mocked(streamRequest).mockImplementationOnce(((_path: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        ;(init.signal as AbortSignal).addEventListener('abort', () => {
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
        })
      })
    }) as any)

    const store = useChatStore()
    const pending = store.sendMessage('first')
    await store.sendMessage('second')

    expect(store.messages).toHaveLength(2)
    expect(vi.mocked(streamRequest)).toHaveBeenCalledTimes(1)

    store.stopStreaming()
    await pending
  })

  // ── E-06: out-of-order conversation switches ────────────
  it('drops an out-of-order messages response from a superseded switch', async () => {
    const pending = new Map<number, (value: any) => void>()
    vi.mocked(api.get).mockImplementation(((url: string) => new Promise((resolve) => {
      const id = Number(String(url).split('/')[2])
      pending.set(id, resolve)
    })) as any)

    const store = useChatStore()
    store.currentConvId = 1
    store.messages = [{ role: 'assistant', content: 'conversation one' }]

    const first = store.selectConversation(2)
    const second = store.selectConversation(3)

    // The newest click answers first...
    pending.get(3)!({ data: [{ id: 30, role: 'assistant', content: 'conversation three' }] })
    await second
    expect(store.currentConvId).toBe(3)
    expect(store.messages[0].content).toBe('conversation three')

    // ...and the slow response for conversation 2 must not overwrite it.
    pending.get(2)!({ data: [{ id: 20, role: 'assistant', content: 'conversation two' }] })
    await first
    expect(store.currentConvId).toBe(3)
    expect(store.messages[0].content).toBe('conversation three')
  })

  it('discards a conversation response that lands after a session reset', async () => {
    const pending = new Map<number, (value: any) => void>()
    vi.mocked(api.get).mockImplementation(((url: string) => new Promise((resolve) => {
      const id = Number(String(url).split('/')[2])
      pending.set(id, resolve)
    })) as any)

    const store = useChatStore()
    store.currentConvId = 1
    const selection = store.selectConversation(2)

    store.reset()
    pending.get(2)!({ data: [{ id: 9, role: 'assistant', content: 'other account history' }] })
    await selection

    expect(store.currentConvId).toBeNull()
    expect(store.messages).toEqual([])
  })

  it('prunes per-conversation file selections for conversations that disappeared', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)

    const store = useChatStore()
    store.currentConvId = 1
    store.selectFile('one.pdf')
    await store.selectConversation(2)
    expect(store.selectedFile).toBeNull()

    // Conversation 1 is gone from the refreshed list: its cached selection is stale.
    vi.mocked(api.get).mockResolvedValue({
      data: [{ id: 2, title: 'Two', mode: 'file', created_at: '2026-01-02' }]
    } as any)
    await store.loadConversations()
    store.currentConvId = 2

    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)
    await store.selectConversation(1)

    expect(store.selectedFile).toBeNull()
  })

  it('drops the cached file selection when its conversation is deleted', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)
    vi.mocked(api.delete).mockResolvedValue({} as any)

    const store = useChatStore()
    store.currentConvId = 1
    store.selectFile('secret.pdf')
    await store.selectConversation(2)

    await store.deleteConversation(1)

    vi.mocked(api.get).mockResolvedValue({ data: [] } as any)
    await store.selectConversation(1)

    expect(store.selectedFile).toBeNull()
  })
})

