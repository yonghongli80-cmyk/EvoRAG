import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ArchiveManifestDialog from '../ArchiveManifestDialog.vue'

describe('ArchiveManifestDialog', () => {
  it('shows approved and rejected entries with reasons', () => {
    const wrapper = mount(ArchiveManifestDialog, {
      props: {
        visible: true,
        entries: [
          { id: 1, normalized_path: '合同.pdf', state: 'approved', reason_code: '' },
          { id: 2, normalized_path: '../secret', state: 'rejected', reason_code: 'unsafe_path' }
        ]
      }
    })
    expect(wrapper.get('[data-test=archive-dialog]').text()).toContain('确认压缩包内容')
    expect(wrapper.find('[data-test=archive-entry-approved]').exists()).toBe(true)
    expect(wrapper.get('[data-test=archive-entry-rejected]').text()).toContain('不安全路径')
  })

  it('emits confirm with selected ids only after the user confirms', async () => {
    const wrapper = mount(ArchiveManifestDialog, {
      props: {
        visible: true,
        entries: [
          { id: 3, normalized_path: 'a.pdf', state: 'approved', reason_code: '' },
          { id: 4, normalized_path: 'b.pdf', state: 'rejected', reason_code: 'encrypted_entry' }
        ]
      }
    })
    await wrapper.get('[data-test=archive-confirm]').trigger('click')
    expect(wrapper.emitted('confirm')?.[0]).toEqual([[3]])
  })
})
