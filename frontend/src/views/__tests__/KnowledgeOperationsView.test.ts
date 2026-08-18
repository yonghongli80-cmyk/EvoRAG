import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import KnowledgeOperationsView from '../KnowledgeOperationsView.vue'

vi.mock('../../api/knowledgeOperations', () => ({
  fetchMyFeedback: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 }),
  fetchMyTasks: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 }),
  fetchOperationsSummary: vi.fn().mockResolvedValue({
    open: 3,
    overdue: 1,
    outdated: 2,
    not_useful: 1,
    stale_knowledge_bases: 1
  }),
  transitionTask: vi.fn()
}))

const mockAuth = vi.fn()
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => mockAuth()
}))

beforeEach(() => {
  vi.clearAllMocks()
})

function mountView(isAdmin: boolean) {
  mockAuth.mockReturnValue({ user: { username: isAdmin ? 'admin' : 'alice', is_admin: isAdmin } })
  const wrapper = mount(KnowledgeOperationsView)
  return { wrapper }
}

describe('KnowledgeOperationsView', () => {
  it('shows admin overview only to administrators', async () => {
    const { wrapper: admin } = mountView(true)
    await flushPromises()
    expect(admin.text()).toContain('运营总览')
    expect(admin.findAll('el-tab-pane').map((tab) => tab.attributes('label'))).toEqual([
      '我的反馈',
      '待我处理',
      '运营总览'
    ])

    const { wrapper: employee } = mountView(false)
    await flushPromises()
    expect(employee.text()).not.toContain('运营总览')
    expect(employee.text()).toContain('我的反馈')
  })

  it('shows an empty state for feedback when there are none', async () => {
    const { wrapper } = mountView(false)
    await flushPromises()
    expect(wrapper.get('[data-test=feedback-empty]').text()).toContain('暂无反馈')
  })
})
