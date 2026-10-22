import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const filesState = vi.hoisted(() => ({
  files: [] as any[],
  loading: false,
  archiveVisible: false,
  archiveEntries: [] as any[],
  archiveManifestId: null as number | null,
  recycleVisible: false,
  recycleItems: [] as any[],
  organizationVisible: false,
  organizationItems: [] as any[],
  organizationProposalId: null as number | null,
  loadFiles: vi.fn(),
  uploadFile: vi.fn(),
  deleteFile: vi.fn(),
  loadArchiveManifest: vi.fn(),
  confirmArchiveImport: vi.fn(),
  openRecycle: vi.fn(),
  closeRecycle: vi.fn(),
  restoreDocument: vi.fn(),
  previewOrganization: vi.fn(),
  confirmOrganization: vi.fn(),
  closeOrganization: vi.fn(),
  reset: vi.fn()
}))

const chatState = vi.hoisted(() => ({
  conversations: [{ id: 1, title: 'Chat', mode: 'file', created_at: '2026-01-01' }],
  currentConvId: 1 as number | null,
  messages: [] as any[],
  isStreaming: false,
  suggestedQuestions: [] as string[],
  selectedFile: null as string | null,
  loadConversations: vi.fn(),
  selectConversation: vi.fn(),
  createConversation: vi.fn(),
  sendMessage: vi.fn(),
  selectFile: vi.fn()
}))

vi.mock('../../stores/files', () => ({ useFilesStore: () => filesState }))
vi.mock('../../stores/chat', () => ({ useChatStore: () => chatState }))
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 1, username: 'admin', is_admin: true } })
}))
vi.mock('../../composables/useAnalysis', async () => {
  const { ref } = await import('vue')
  return {
    useAnalysis: () => ({
      addToAnalysis: vi.fn(),
      dialogVisible: ref(false),
      dialogQuestion: ref(''),
      dialogAnswer: ref(''),
      handleConfirm: vi.fn()
    })
  }
})
vi.mock('../../utils/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
}))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  ElMessageBox: { prompt: vi.fn(), confirm: vi.fn() }
}))

import { ElMessageBox } from 'element-plus'
import FileModeView from '../FileModeView.vue'

const BUTTON_STUB = { template: '<button v-bind="$attrs"><slot /></button>' }

function mountView() {
  return mount(FileModeView, {
    global: {
      stubs: {
        ConversationSidebar: true,
        MessageBubble: true,
        ChatInput: true,
        AddToAnalysisDialog: true,
        ArchiveManifestDialog: true,
        OrganizationPreviewDialog: true,
        DocumentCompareDialog: true,
        'el-button': BUTTON_STUB,
        'el-icon': { template: '<i><slot /></i>' },
        'el-upload': { template: '<div><slot /></div>' },
        'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
        'el-dropdown-menu': { template: '<div><slot /></div>' },
        'el-dropdown-item': { template: '<div><slot /></div>' },
        'el-message-box': true,
        Upload: true,
        FolderOpened: true,
        Plus: true,
        ChatDotRound: true
      }
    }
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  filesState.files = []
  filesState.recycleVisible = false
  filesState.recycleItems = []
  filesState.archiveVisible = false
  filesState.organizationVisible = false
  chatState.conversations = [{ id: 1, title: 'Chat', mode: 'file', created_at: '2026-01-01' }]
  chatState.messages = []
  chatState.currentConvId = 1
})

describe('FileModeView material lifecycle entries (E-07)', () => {
  it('exposes a recycle-bin entry that loads the recycle list', async () => {
    const wrapper = mountView()

    const button = wrapper.get('[data-test="open-recycle"]')
    expect(button.text()).toContain('回收站')

    await button.trigger('click')
    await flushPromises()

    expect(filesState.openRecycle).toHaveBeenCalledTimes(1)
  })

  it('exposes an organization entry that previews the entered instruction', async () => {
    vi.mocked(ElMessageBox.prompt).mockResolvedValue({
      value: ' 把财务发票归档到 财务/发票 ',
      action: 'confirm'
    } as any)
    const wrapper = mountView()

    await wrapper.get('[data-test="open-organization"]').trigger('click')
    await flushPromises()

    expect(filesState.previewOrganization).toHaveBeenCalledWith('把财务发票归档到 财务/发票')
  })

  it('does not preview organization when the instruction prompt is cancelled', async () => {
    vi.mocked(ElMessageBox.prompt).mockRejectedValue('cancel')
    const wrapper = mountView()

    await wrapper.get('[data-test="open-organization"]').trigger('click')
    await flushPromises()

    expect(filesState.previewOrganization).not.toHaveBeenCalled()
  })

  it('restores a recycled document from the mounted recycle dialog', async () => {
    filesState.recycleVisible = true
    filesState.recycleItems = [{
      document_id: 9,
      filename: 'deleted.pdf',
      deleted_at: Date.now() / 1000 - 60,
      deleted_by: 1,
      restore_until: Date.now() / 1000 + 3600
    }]
    const wrapper = mountView()

    await wrapper.get('[data-test="recycle-restore"]').trigger('click')
    await flushPromises()

    expect(filesState.restoreDocument).toHaveBeenCalledWith(9)
  })
})
