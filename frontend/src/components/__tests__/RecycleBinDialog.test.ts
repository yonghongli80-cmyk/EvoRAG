import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RecycleBinDialog from '../RecycleBinDialog.vue'

describe('RecycleBinDialog', () => {
  it('shows the restore deadline and emits restore', async () => {
    const wrapper = mount(RecycleBinDialog, {
      props: {
        visible: true,
        items: [{ document_id: 5, filename: 'old.pdf', restore_until: Date.now() / 1000 + 86400 }]
      }
    })
    expect(wrapper.get('[data-test=recycle-dialog]').text()).toContain('old.pdf')
    expect(wrapper.find('[data-test=recycle-deadline]').exists()).toBe(true)
    await wrapper.get('[data-test=recycle-restore]').trigger('click')
    expect(wrapper.emitted('restore')?.[0]).toEqual([5])
  })

  it('marks expired items and disables restore', () => {
    const wrapper = mount(RecycleBinDialog, {
      props: {
        visible: true,
        items: [{ document_id: 6, filename: 'gone.pdf', restore_until: Date.now() / 1000 - 1 }]
      }
    })
    expect(wrapper.get('[data-test=recycle-expired]').text()).toContain('已过期')
    expect((wrapper.get('[data-test=recycle-restore]').element as HTMLButtonElement).disabled).toBe(true)
  })
})
