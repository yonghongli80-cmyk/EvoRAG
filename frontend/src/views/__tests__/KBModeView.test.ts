import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import KBModeView from '../KBModeView.vue'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../utils/api', () => ({ default: { get, post: vi.fn(), delete: vi.fn() } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

describe('KBModeView organization permissions', () => {
  beforeEach(() => get.mockReset())

  it('groups visible libraries and removes personal sharing language', async () => {
    get.mockResolvedValue({ data: [
      { kb_id: 'public', name: '公共资料', doc_count: 2, kb_type: 'company_public', org_unit: { code: 'ROOT', name: '星河集团' }, effective_role: 'read', sensitivity: null },
      { kb_id: 'team', name: '一组资料', doc_count: 1, kb_type: 'organization', org_unit: { code: 'T1', name: '一组' }, effective_role: 'manage', sensitivity: null },
      { kb_id: 'admin', name: '管理资料', doc_count: 1, kb_type: 'organization', org_unit: { code: 'T1', name: '一组' }, effective_role: 'administer', sensitivity: null },
      { kb_id: 'finance', name: '财务资料', doc_count: 1, kb_type: 'sensitive', org_unit: { code: 'ROOT', name: '星河集团' }, effective_role: 'read', sensitivity: 'finance' },
    ] })
    const wrapper = mount(KBModeView, { global: { stubs: { 'el-button': true, 'el-dialog': true, 'el-input': true, 'el-select': true, 'el-option': true, 'el-icon': true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('公司公共库')
    expect(wrapper.text()).toContain('组织知识库')
    expect(wrapper.text()).toContain('敏感知识库')
    expect(wrapper.text()).toContain('可管理')
    expect(wrapper.text()).not.toContain('私有')
    expect(wrapper.text()).not.toContain('共享给')
    expect(wrapper.findAll('button.delete')).toHaveLength(1)
  })
})
