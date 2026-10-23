import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useChatStore } from '../chat'

vi.mock('../../utils/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  isAbortError: (error: any) => error?.name === 'AbortError' || error?.name === 'CanceledError',
  streamRequest: vi.fn()
}))

import api, { streamRequest } from '../../utils/api'

const conversation = (id: number, mode: 'file' | 'kb' | 'analysis' = 'file') => ({
  id, title: 'New conversation', mode, created_at: '2026-10-23'
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function emptyStream() {
  return {
    ok: true,
    body: { getReader: () => ({ read: vi.fn().mockResolvedValue({ done: true }) }) }
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('chat review regressions', () => {
  it('discards a conversation creation response after reset', async () => {
    const pending = deferred<any>()
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const store = useChatStore()

    const creation = store.createConversation('file')
    store.reset()
    pending.resolve({ data: conversation(11) })
    await creation

    expect(store.conversations).toEqual([])
    expect(store.currentConvId).toBeNull()
  })

  it('does not apply suggestions after the request is stopped and the conversation changes', async () => {
    const pendingSuggestion = deferred<any>()
    vi.mocked(streamRequest).mockResolvedValueOnce({
      ok: true,
      body: { getReader: () => ({
        read: vi.fn()
          .mockResolvedValueOnce({ done: false, value: new TextEncoder().encode('data: {"type":"token","content":"answer"}\n') })
          .mockResolvedValueOnce({ done: true })
      }) }
    } as any)
    vi.mocked(api.post).mockReturnValueOnce(pendingSuggestion.promise)
    const store = useChatStore()
    store.conversations = [conversation(1), conversation(2)]
    store.currentConvId = 1

    const sending = store.sendMessage('question')
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/suggest', { question: 'question', answer: 'answer' }))
    store.stopStreaming()
    store.currentConvId = 2
    store.suggestedQuestions = ['new conversation suggestion']
    pendingSuggestion.resolve({ data: { questions: ['stale suggestion'] } })
    await sending

    expect(store.suggestedQuestions).toEqual(['new conversation suggestion'])
  })

  it('aborts the old stream when switching mode during conversation creation', async () => {
    const pendingCreation = deferred<any>()
    vi.mocked(api.post).mockReturnValueOnce(pendingCreation.promise)
    const store = useChatStore()

    const sending = store.sendMessage('question', 'file')
    store.resetModeState('kb')
    pendingCreation.resolve({ data: conversation(12) })
    await sending

    expect(store.currentMode).toBe('kb')
    expect(store.currentConvId).toBeNull()
    expect(store.conversations).toEqual([])
    expect(streamRequest).not.toHaveBeenCalled()
  })

  it('allows only one send while the automatic conversation creation is pending', async () => {
    const pendingCreation = deferred<any>()
    vi.mocked(api.post).mockReturnValueOnce(pendingCreation.promise)
    vi.mocked(streamRequest).mockResolvedValue(emptyStream() as any)
    const store = useChatStore()

    const first = store.sendMessage('first', 'file')
    const second = store.sendMessage('second', 'file')
    pendingCreation.resolve({ data: conversation(13) })
    await Promise.all([first, second])

    expect(api.post).toHaveBeenCalledTimes(1)
    expect(streamRequest).toHaveBeenCalledTimes(1)
    expect(store.messages.filter(message => message.role === 'user').map(message => message.content)).toEqual(['first'])
  })
})
