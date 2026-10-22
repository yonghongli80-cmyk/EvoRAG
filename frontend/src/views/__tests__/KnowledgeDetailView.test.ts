import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createTestingPinia } from '@pinia/testing'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('../../utils/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  streamRequest: vi.fn(),
  registerSessionResetHandler: vi.fn()
}))

vi.mock('../../composables/useAnalysis', () => ({
  useAnalysis: () => ({
    addToAnalysis: vi.fn(),
    dialogVisible: { value: false },
    dialogQuestion: { value: '' },
    dialogAnswer: { value: '' },
    handleConfirm: vi.fn(),
  })
}))

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))

import api from '../../utils/api'
import { useChatStore } from '../../stores/chat'
import KnowledgeDetailView from '../KnowledgeDetailView.vue'

describe('KnowledgeDetailView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // jsdom does not implement element scrolling; the chat area watchers call it.
    Element.prototype.scrollTo = vi.fn()
    vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve({
      data: { name: `KB ${url.split('/').pop()}`, documents: [] }
    }) as any)
  })

  it('reloads detail when the reused route component receives a new id', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/kb/:id', component: KnowledgeDetailView }]
    })
    await router.push('/kb/first')
    await router.isReady()

    mount(KnowledgeDetailView, {
      global: {
        plugins: [router, createTestingPinia({ createSpy: vi.fn })],
        stubs: {
          MessageBubble: true,
          ChatInput: true,
          AddToAnalysisDialog: true,
          'el-button': true,
          'el-icon': true,
          'el-dropdown': true,
          'el-dropdown-menu': true,
          'el-dropdown-item': true,
          'el-tag': true,
          'el-dialog': true,
          'el-input': true,
        }
      }
    })
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/knowledge-bases/first')

    await router.push('/kb/second')
    await flushPromises()

    expect(api.get).toHaveBeenCalledWith('/knowledge-bases/second')
  })

  it('shows a spreadsheet viewer action for spreadsheet documents', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        name: 'Ops',
        documents: [{
          filename: 'report.xlsx',
          document_id: 7,
          document_version_id: 9,
          chunk_count: 1,
          status: 'indexed',
        }],
      }
    } as any)
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/kb/:id', component: KnowledgeDetailView }]
    })
    await router.push('/kb/ops')
    await router.isReady()

    const wrapper = mount(KnowledgeDetailView, {
      global: {
        plugins: [router, createTestingPinia({ createSpy: vi.fn })],
        stubs: {
          MessageBubble: true,
          ChatInput: true,
          AddToAnalysisDialog: true,
          'el-button': true,
          'el-icon': true,
          'el-dropdown': true,
          'el-dropdown-menu': true,
          'el-dropdown-item': true,
          'el-tag': true,
          'el-dialog': true,
          'el-input': true,
        }
      }
    })
    await flushPromises()

    expect(wrapper.find('[data-test="open-spreadsheet-7-9"]').exists()).toBe(true)
  })

  it('does not offer knowledge-base deletion to a content manager', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { name: 'Managed KB', documents: [], effective_role: 'manage' }
    } as any)
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/kb/:id', component: KnowledgeDetailView }]
    })
    await router.push('/kb/managed')
    await router.isReady()

    const wrapper = mount(KnowledgeDetailView, {
      global: {
        plugins: [router, createTestingPinia({ createSpy: vi.fn })],
        stubs: {
          MessageBubble: true, ChatInput: true, AddToAnalysisDialog: true,
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<i><slot /></i>' },
          'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
          'el-dropdown-menu': { template: '<div><slot /></div>' },
          'el-dropdown-item': { template: '<button><slot /></button>' },
          'el-tag': true, 'el-dialog': true, 'el-input': true,
        }
      }
    })
    await flushPromises()

    expect(wrapper.text()).not.toContain('删除知识库')
    expect(wrapper.text()).toContain('添加文件')
  })

  // ── E-08: switching knowledge bases clears conversation state ──
  it('clears the previous knowledge base conversation when the id changes', async () => {
    vi.mocked(api.get).mockImplementation(((url: string) => Promise.resolve({
      data: url === '/conversations'
        ? []
        : { name: `KB ${url.split('/').pop()}`, documents: [] }
    })) as any)

    const pinia = createPinia()
    setActivePinia(pinia)
    const chatStore = useChatStore()

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/kb/:id', component: KnowledgeDetailView }]
    })
    await router.push('/kb/first')
    await router.isReady()

    mount(KnowledgeDetailView, {
      global: {
        plugins: [router, pinia],
        stubs: {
          MessageBubble: true, ChatInput: true, AddToAnalysisDialog: true,
          'el-button': true, 'el-icon': true, 'el-dropdown': true,
          'el-dropdown-menu': true, 'el-dropdown-item': true,
          'el-tag': true, 'el-dialog': true, 'el-input': true,
        }
      }
    })
    await flushPromises()

    // Simulate a conversation that belongs to the first KB.
    chatStore.messages = [{ role: 'assistant', content: 'first-kb answer' }]
    chatStore.currentConvId = 11
    chatStore.suggestedQuestions = ['first-kb follow-up']
    chatStore.selectedFile = 'first-kb.pdf'

    await router.push('/kb/second')
    await flushPromises()

    expect(chatStore.messages).toEqual([])
    expect(chatStore.currentConvId).toBeNull()
    expect(chatStore.suggestedQuestions).toEqual([])
    expect(chatStore.selectedFile).toBeNull()

    // Conversations are reloaded for every KB entry, not inherited.
    const conversationLoads = vi.mocked(api.get).mock.calls
      .filter(([url]) => url === '/conversations')
    expect(conversationLoads).toHaveLength(2)
    expect(vi.mocked(api.get)).toHaveBeenCalledWith('/knowledge-bases/second')
  })
})
