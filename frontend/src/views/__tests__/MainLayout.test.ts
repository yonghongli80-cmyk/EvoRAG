import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import MainLayout from '../MainLayout.vue'

const mockPush = vi.fn()
let mockPath = '/files'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  useRoute: () => ({ path: mockPath })
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    user: { username: 'admin', is_admin: true },
    fetchUser: vi.fn(),
    logout: vi.fn()
  })
}))

beforeEach(() => {
  vi.clearAllMocks()
})

describe('MainLayout', () => {
  it('adds spreadsheet audit after the three main admin navigation items', () => {
    mockPath = '/operations'

    const wrapper = mount(MainLayout)

    const labels = wrapper.findAll('.nav-label').map((node) => node.text())
    expect(labels).toEqual(['文件', '知识库', 'Wiki双链', '知识运营', '表格变更', '进化中心', '组织权限'])
    expect(wrapper.get('[aria-label="知识运营"]').classes()).toContain('active')
  })

  it('adds the evolution center for admins only', () => {
    mockPath = '/operations/evolution'

    const wrapper = mount(MainLayout)

    expect(wrapper.get('[data-test="evolution-nav"]').classes()).toContain('active')
  })

  it('marks files active on the files route', () => {
    mockPath = '/files'

    const wrapper = mount(MainLayout)

    expect(wrapper.get('[aria-label="文件管理"]').classes()).toContain('active')
  })

  it('marks wiki active on the wiki route', () => {
    mockPath = '/wiki'

    const wrapper = mount(MainLayout)

    expect(wrapper.get('[data-test="wiki-nav"]').classes()).toContain('active')
  })
})
