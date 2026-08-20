import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import OrganizationAdminView from '../OrganizationAdminView.vue'

const mocks = vi.hoisted(() => ({ tree: vi.fn(), unit: vi.fn(), preview: vi.fn(), audit: vi.fn() }))
vi.mock('../../api/organization', () => ({
  getOrganizationTree: mocks.tree,
  getOrganizationUnit: mocks.unit,
  previewOrganizationUser: mocks.preview,
  getOrganizationAudit: mocks.audit,
  updateOrganizationPerson: vi.fn(),
}))

it('renders organization people and a read-only permission preview', async () => {
  mocks.tree.mockResolvedValue([{ code: 'ROOT', parent_code: null, name: '星河集团', kind: 'company', active: true }])
  mocks.unit.mockResolvedValue({ unit: { code: 'ROOT', name: '星河集团' }, people: [{ person_code: 'P1', display_name: '林知远', job_title: '负责人', user_id: 1, username: 'demo001' }], knowledge_bases: [] })
  mocks.audit.mockResolvedValue([])
  mocks.preview.mockResolvedValue({ knowledge_bases: [{ kb_id: 'public', name: '公共库', effective_role: 'read' }] })
  const wrapper = mount(OrganizationAdminView, { global: { stubs: { 'el-button': true } } })
  await flushPromises()
  await wrapper.get('[data-test="unit-ROOT"]').trigger('click')
  await flushPromises()
  await wrapper.get('[data-test="preview-P1"]').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('林知远')
  expect(wrapper.text()).toContain('可查看')
  expect(wrapper.find('[data-test="login-as-person"]').exists()).toBe(false)
})
