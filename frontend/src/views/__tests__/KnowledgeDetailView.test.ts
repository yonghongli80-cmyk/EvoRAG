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

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const viewStubs = {
  MessageBubble: true,
  ChatInput: true,
  AddToAnalysisDialog: true,
  ConversationSidebar: true,
  KnowledgeChatArea: true,
  KnowledgeRenameDialog: true,
  KnowledgeInfoPanel: {
    props: ['kbName', 'kbOverview', 'documents', 'loading', 'canAdminister'],
    template: `<section>
      <span data-test="kb-name">{{ kbName }}</span>
      <span data-test="can-administer">{{ canAdminister }}</span>
      <span data-test="loading">{{ loading }}</span>
      <span data-test="overview">{{ kbOverview }}</span>
      <span data-test="documents">{{ documents.map(d => d.filename).join(',') }}</span>
      <button data-test="delete-kb" @click="$emit('delete-kb')">delete</button>
      <button data-test="remove-doc" @click="$emit('remove-doc', documents[0])">remove</button>
      <button data-test="open-add" @click="$emit('open-add-dialog')">open-add</button>
    </section>`
  },
  KnowledgeAddFileDialog: {
    props: ['availableFiles'],
    template: `<section>
      <span data-test="available-files">{{ availableFiles.map(f => f.name).join(',') }}</span>
      <button data-test="load-files" @click="$emit('open')">load-files</button>
      <button v-for="file in availableFiles" :key="file.name" @click="$emit('add', file)">{{ file.name }}</button>
    </section>`
  },
}

async function mountKnowledgeDetail(path = '/kb/first') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/kb/:id', component: KnowledgeDetailView },
      { path: '/kb', component: { template: '<div />' } },
    ]
  })
  await router.push(path)
  await router.isReady()
  const wrapper = mount(KnowledgeDetailView, {
    global: { plugins: [router, createTestingPinia({ createSpy: vi.fn })], stubs: viewStubs }
  })
  return { router, wrapper }
}

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

  it('does not let a late detail response overwrite a failed current KB', async () => {
    const first = deferred<any>()
    const second = deferred<any>()
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/conversations') return Promise.resolve({ data: [] }) as any
      if (url === '/knowledge-bases/first') return first.promise
      if (url === '/knowledge-bases/second') return second.promise
      return Promise.resolve({ data: {} }) as any
    })
    const { router, wrapper } = await mountKnowledgeDetail()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/knowledge-bases/first'))

    await router.push('/kb/second')
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/knowledge-bases/second'))
    second.reject(new Error('second failed'))
    await flushPromises()
    first.resolve({ data: { name: 'First secret', effective_role: 'administer', documents: [{ filename: 'first.pdf' }] } })
    await flushPromises()

    expect(wrapper.get('[data-test="kb-name"]').text()).toBe('')
    expect(wrapper.get('[data-test="documents"]').text()).toBe('')
    expect(wrapper.get('[data-test="can-administer"]').text()).toBe('false')
    expect(wrapper.get('[data-test="loading"]').text()).toBe('false')
    expect(router.currentRoute.value.fullPath).toBe('/kb/second')
  })

  it('keeps the current KB loading while an old request finishes', async () => {
    const first = deferred<any>()
    const second = deferred<any>()
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/conversations') return Promise.resolve({ data: [] }) as any
      if (url === '/knowledge-bases/first') return first.promise
      if (url === '/knowledge-bases/second') return second.promise
      return Promise.resolve({ data: {} }) as any
    })
    const { router, wrapper } = await mountKnowledgeDetail()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/knowledge-bases/first'))
    await router.push('/kb/second')
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/knowledge-bases/second'))

    first.resolve({ data: { name: 'First', effective_role: 'administer', documents: [] } })
    await flushPromises()
    expect(wrapper.get('[data-test="loading"]').text()).toBe('true')

    second.resolve({ data: { name: 'Second', documents: [] } })
    await flushPromises()
    expect(wrapper.get('[data-test="kb-name"]').text()).toBe('Second')
    expect(wrapper.get('[data-test="loading"]').text()).toBe('false')
  })

  it('clears loaded details and permissions when the next KB fails', async () => {
    const second = deferred<any>()
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/conversations') return Promise.resolve({ data: [] }) as any
      if (url === '/knowledge-bases/first') return Promise.resolve({ data: {
        name: 'First', effective_role: 'administer', documents: [{ filename: 'first.pdf' }],
      } }) as any
      if (url === '/knowledge-bases/second') return second.promise
      return Promise.resolve({ data: {} }) as any
    })
    const { router, wrapper } = await mountKnowledgeDetail()
    await flushPromises()
    expect(wrapper.get('[data-test="kb-name"]').text()).toBe('First')
    expect(wrapper.get('[data-test="can-administer"]').text()).toBe('true')

    await router.push('/kb/second')
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/knowledge-bases/second'))
    second.reject(new Error('second failed'))
    await flushPromises()

    expect(wrapper.get('[data-test="kb-name"]').text()).toBe('')
    expect(wrapper.get('[data-test="documents"]').text()).toBe('')
    expect(wrapper.get('[data-test="can-administer"]').text()).toBe('false')
    expect(wrapper.get('[data-test="loading"]').text()).toBe('false')
  })

  it('does not navigate away when an old KB detail request returns 404', async () => {
    const first = deferred<any>()
    const second = deferred<any>()
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/conversations') return Promise.resolve({ data: [] }) as any
      if (url === '/knowledge-bases/first') return first.promise
      if (url === '/knowledge-bases/second') return second.promise
      return Promise.resolve({ data: {} }) as any
    })
    const { router } = await mountKnowledgeDetail()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/knowledge-bases/first'))
    await router.push('/kb/second')
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/knowledge-bases/second'))

    first.reject({ response: { status: 404 } })
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/kb/second')

    second.resolve({ data: { name: 'Second', documents: [] } })
    await flushPromises()
  })

  it('keeps a pending removal confirmation bound to the KB where it was opened', async () => {
    const confirmation = deferred<void>()
    vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve({
      data: url === '/conversations' ? [] : {
        name: `KB ${url.split('/').pop()}`,
        documents: [{ filename: 'shared.pdf' }],
      }
    }) as any)
    vi.mocked(api.delete).mockResolvedValue({ data: {} } as any)
    const { router, wrapper } = await mountKnowledgeDetail()
    await flushPromises()
    vi.mocked((await import('element-plus')).ElMessageBox.confirm).mockReturnValue(confirmation.promise as any)

    await wrapper.get('[data-test="remove-doc"]').trigger('click')
    await router.push('/kb/second')
    await flushPromises()
    confirmation.resolve()
    await flushPromises()

    expect(api.delete).toHaveBeenCalledWith('/knowledge-bases/first/documents/shared.pdf')
  })

  it('offers files linked to another KB but excludes files already in the current KB', async () => {
    const link = deferred<any>()
    vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve({
      data: url === '/conversations' ? [] : url === '/files'
        ? { files: [
          { name: 'other-kb.pdf', in_kb: true, document_id: 12 },
          { name: 'already-here.pdf', in_kb: false, document_id: 13 },
        ] }
        : { name: 'First', documents: [{ filename: 'already-here.pdf' }] }
    }) as any)
    vi.mocked(api.post).mockReturnValue(link.promise as any)
    const { router, wrapper } = await mountKnowledgeDetail()
    await flushPromises()
    await wrapper.get('[data-test="open-add"]').trigger('click')
    await wrapper.get('[data-test="load-files"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-test="available-files"]').text()).toBe('other-kb.pdf')

    const otherKbFile = wrapper.findAll('button').find(button => button.text() === 'other-kb.pdf')!
    await otherKbFile.trigger('click')
    await router.push('/kb/second')
    await flushPromises()
    link.resolve({ data: { status: 'linked' } })
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/knowledge-bases/first/documents/link', {
      document_id: 12,
      duplicate_action: 'reject',
    })
  })
})
