import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import WikiModeView from '../WikiModeView.vue'

const { get, post, put, deleteFn } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  deleteFn: vi.fn()
}))

vi.mock('../../utils/api', () => ({
  default: { get, post, put, delete: deleteFn }
}))

const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  useRoute: () => ({ query: {}, params: {} })
}))

describe('WikiModeView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loads knowledge bases and page list on mount', async () => {
    get.mockImplementation((url: string) => {
      if (url === '/knowledge-bases') {
        return Promise.resolve({
          data: [{ kb_id: 'kb_1', name: '技术知识库', doc_count: 5, effective_role: 'manage' }]
        })
      }
      if (url === '/wiki/pages') {
        return Promise.resolve({
          data: [
            {
              id: 1,
              kb_id: 'kb_1',
              title: '微服务架构',
              slug: 'microservices',
              content: '关于微服务规范，参见 [[数据库规范]]',
              tags: ['架构'],
              current_revision_id: 1,
              created_at: 1700000000,
              updated_at: 1700000000
            }
          ]
        })
      }
      if (url === '/wiki/pages/1') {
        return Promise.resolve({
          data: {
            page: {
              id: 1,
              kb_id: 'kb_1',
              title: '微服务架构',
              slug: 'microservices',
              content: '关于微服务规范，参见 [[数据库规范]]',
              tags: ['架构'],
              current_revision_id: 1,
              created_at: 1700000000,
              updated_at: 1700000000
            },
            outgoing_links: [{ target_title: '数据库规范', target_page_id: null }],
            backlinks: [],
            revision_count: 1
          }
        })
      }
      return Promise.resolve({ data: [] })
    })

    const wrapper = mount(WikiModeView, {
      global: {
        stubs: {
          'el-select': true,
          'el-option': true,
          'el-button': true,
          'el-input': true,
          'el-icon': true,
          'el-tag': true,
          'el-dialog': true,
          'el-drawer': true
        }
      }
    })

    await flushPromises()

    expect(wrapper.text()).toContain('超文本 Wiki')
    expect(wrapper.text()).toContain('微服务架构')
    expect(wrapper.text()).toContain('反向链接 (Backlinks)')
    expect(wrapper.text()).toContain('出链引用 (Outgoing Links)')
    expect(wrapper.text()).toContain('数据库规范')
  })

  it('switches to edit mode and triggers update API on save', async () => {
    get.mockImplementation((url: string) => {
      if (url === '/knowledge-bases') {
        return Promise.resolve({
          data: [{ kb_id: 'kb_1', name: '技术库', doc_count: 1, effective_role: 'manage' }]
        })
      }
      if (url === '/wiki/pages') {
        return Promise.resolve({
          data: [{ id: 1, kb_id: 'kb_1', title: '初始页面', content: '初始内容', tags: [] }]
        })
      }
      if (url === '/wiki/pages/1') {
        return Promise.resolve({
          data: {
            page: { id: 1, kb_id: 'kb_1', title: '初始页面', content: '初始内容', tags: [] },
            outgoing_links: [],
            backlinks: [],
            revision_count: 1
          }
        })
      }
      return Promise.resolve({ data: [] })
    })

    put.mockResolvedValue({
      data: {
        page: { id: 1, kb_id: 'kb_1', title: '新标题', content: '新内容', tags: [] },
        chunks_indexed: 2
      }
    })

    const wrapper = mount(WikiModeView, {
      global: {
        stubs: {
          'el-select': true,
          'el-option': true,
          'el-button': true,
          'el-input': true,
          'el-icon': true,
          'el-tag': true,
          'el-dialog': true,
          'el-drawer': true
        }
      }
    })

    await flushPromises()

    // Click edit button
    const editBtn = wrapper.find('[data-test="edit-wiki-btn"]')
    expect(editBtn.exists()).toBe(true)
    await editBtn.trigger('click')

    // Find save button
    const saveBtn = wrapper.find('[data-test="save-wiki-btn"]')
    expect(saveBtn.exists()).toBe(true)
  })

  it('sanitizes malicious XSS payload in wikilinks without executing attributes', async () => {
    get.mockImplementation((url: string) => {
      if (url === '/knowledge-bases') {
        return Promise.resolve({
          data: [{ kb_id: 'kb_1', name: '技术库', doc_count: 1, effective_role: 'manage' }]
        })
      }
      if (url === '/wiki/pages') {
        return Promise.resolve({
          data: [{ id: 2, kb_id: 'kb_1', title: 'XSS测试', content: '测试 [[malicious" onmouseover="alert(1)|安全标签]]', tags: [] }]
        })
      }
      if (url === '/wiki/pages/2') {
        return Promise.resolve({
          data: {
            page: {
              id: 2,
              kb_id: 'kb_1',
              title: 'XSS测试',
              content: '测试 [[malicious" onmouseover="alert(1)|安全标签]]',
              tags: []
            },
            outgoing_links: [],
            backlinks: [],
            revision_count: 1
          }
        })
      }
      return Promise.resolve({ data: [] })
    })

    const wrapper = mount(WikiModeView, {
      global: {
        stubs: {
          'el-select': true,
          'el-option': true,
          'el-button': true,
          'el-input': true,
          'el-icon': true,
          'el-tag': true,
          'el-dialog': true,
          'el-drawer': true
        }
      }
    })

    await flushPromises()

    const renderArea = wrapper.find('.markdown-render-area')
    expect(renderArea.exists()).toBe(true)
    const html = renderArea.html()
    // Verify onmouseover is not an executable attribute
    expect(html).not.toContain('onmouseover="alert(1)"')
    expect(html).toContain('wikilink-badge')
    expect(html).toContain('安全标签')
  })
})
