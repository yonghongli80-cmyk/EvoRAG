import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('../../utils/api', () => ({
  default: {
    post: vi.fn(),
    get: vi.fn(),
    put: vi.fn(),
  }
}))

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn() },
}))

import api from '../../utils/api'
import { useAnalysis } from '../useAnalysis'
import { useAuthStore } from '../../stores/auth'

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
})

describe('useAnalysis composable', () => {
  it('addToAnalysis opens dialog with question and answer', () => {
    const { addToAnalysis, dialogVisible, dialogQuestion, dialogAnswer } = useAnalysis()

    addToAnalysis('What is RAG?', 'Retrieval-Augmented Generation')

    expect(dialogVisible.value).toBe(true)
    expect(dialogQuestion.value).toBe('What is RAG?')
    expect(dialogAnswer.value).toBe('Retrieval-Augmented Generation')
  })

  it('handleConfirm with existing card adds question', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { id: 1 } } as any)

    const auth = useAuthStore()
    auth.token = 'test-token'

    const { handleConfirm, dialogVisible, dialogQuestion, dialogAnswer } = useAnalysis()
    dialogQuestion.value = 'What is RAG?'
    dialogAnswer.value = 'Retrieval-Augmented Generation'

    await handleConfirm(42)

    expect(api.post).toHaveBeenCalledWith(
      '/analysis/cards/42/questions',
      { question: 'What is RAG?', answer: 'Retrieval-Augmented Generation' }
    )
    expect(dialogVisible.value).toBe(false)
  })

  it('handleConfirm with new card creates card then adds question', async () => {
    vi.mocked(api.post)
      .mockResolvedValueOnce({ data: { id: 99 } } as any)
      .mockResolvedValueOnce({ data: { id: 100 } } as any)

    const auth = useAuthStore()
    auth.token = 'test-token'

    const { handleConfirm, dialogVisible, dialogQuestion, dialogAnswer } = useAnalysis()
    dialogQuestion.value = 'How does it work?'
    dialogAnswer.value = 'It works by retrieving relevant documents.'

    await handleConfirm(null, 'New Card Group')

    expect(api.post).toHaveBeenCalledWith(
      '/analysis/cards',
      { name: 'New Card Group' }
    )
    expect(api.post).toHaveBeenCalledWith(
      '/analysis/cards/99/questions',
      { question: 'How does it work?', answer: 'It works by retrieving relevant documents.' }
    )
    expect(dialogVisible.value).toBe(false)
  })

  it('handleConfirm does nothing when no card selected and no name', async () => {
    const { handleConfirm } = useAnalysis()

    await handleConfirm(null)

    expect(api.post).not.toHaveBeenCalled()
  })

  it('handleConfirm manages loading state', async () => {
    vi.mocked(api.post).mockImplementation(
      () => new Promise(r => setTimeout(() => r({ data: { id: 1 } } as any), 10))
    )

    const auth = useAuthStore()
    auth.token = 'test-token'

    const { handleConfirm, loading } = useAnalysis()

    expect(loading.value).toBe(false)
    const promise = handleConfirm(1)
    expect(loading.value).toBe(true)
    await promise
    expect(loading.value).toBe(false)
  })
})
