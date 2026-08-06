import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import DuplicateDocumentDialog from '../DuplicateDocumentDialog.vue'

describe('DuplicateDocumentDialog', () => {
  it('offers skip overwrite and keep after duplicate conflict', async () => {
    const wrapper = mount(DuplicateDocumentDialog, {
      props: {
        visible: true,
        filename: 'policy.txt',
        currentVersion: 2
      }
    })

    expect(wrapper.get('[data-test=duplicate-dialog]').text()).toContain('覆盖')
    expect(wrapper.find('[data-test=duplicate-keep]').exists()).toBe(true)
    expect(wrapper.find('[data-test=duplicate-reject]').exists()).toBe(true)

    await wrapper.get('[data-test=duplicate-overwrite]').trigger('click')
    expect(wrapper.emitted('confirm')?.[0]).toEqual(['overwrite'])
  })
})
