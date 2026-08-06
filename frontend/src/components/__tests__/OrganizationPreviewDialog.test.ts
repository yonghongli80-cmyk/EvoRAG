import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import OrganizationPreviewDialog from '../OrganizationPreviewDialog.vue'

describe('OrganizationPreviewDialog', () => {
  it('shows the preview and only mutates after confirm', async () => {
    const wrapper = mount(OrganizationPreviewDialog, {
      props: {
        visible: true,
        items: [{ id: 1, document_id: 10, target_path: '入职材料', status: 'executable', reason_code: '' }]
      }
    })
    expect(wrapper.get('[data-test=organization-dialog]').text()).toContain('将修改以下资料')
    expect(wrapper.get('[data-test=organization-target]').text()).toContain('入职材料')
    await wrapper.get('[data-test=organization-confirm]').trigger('click')
    expect(wrapper.emitted('confirm')?.[0]).toEqual([[1]])
  })

  it('shows rejected items without enabling confirm when none are executable', () => {
    const wrapper = mount(OrganizationPreviewDialog, {
      props: {
        visible: true,
        items: [{ id: 2, document_id: 11, target_path: '', status: 'rejected', reason_code: 'unknown_candidate' }]
      }
    })
    expect(wrapper.get('[data-test=organization-rejected]').text()).toContain('unknown_candidate')
    expect((wrapper.get('[data-test=organization-confirm]').element as HTMLButtonElement).disabled).toBe(true)
  })
})
