import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import DocumentCompareDialog from '../DocumentCompareDialog.vue'

const { mockCompareDocuments } = vi.hoisted(() => ({
  mockCompareDocuments: vi.fn()
}))

vi.mock('../../api/reliability', () => ({
  compareDocuments: mockCompareDocuments
}))

describe('DocumentCompareDialog', () => {
  it('submits selected visible logical documents and renders missing cells as insufficient evidence', async () => {
    mockCompareDocuments.mockResolvedValue({
      rows: [{
        dimension: '年假',
        values: {
          7: { status: 'supported', text: '5 天', citation_ids: [11] },
          8: { status: 'insufficient_evidence', text: '', citation_ids: [] }
        }
      }],
      partial: true,
      failures: ['document 8 timed out']
    })
    const wrapper = mount(DocumentCompareDialog, {
      props: {
        visible: true,
        documents: [{ id: 7, name: 'a.pdf' }, { id: 8, name: 'b.pdf' }]
      }
    })

    const inputs = wrapper.findAll('input[type="checkbox"]')
    await inputs[0].setValue(true)
    await inputs[1].setValue(true)
    await wrapper.find('input[name="question"]').setValue('比较年假')
    await wrapper.find('form').trigger('submit.prevent')

    expect(mockCompareDocuments).toHaveBeenCalledWith({
      question: '比较年假',
      document_ids: [7, 8],
      dimensions: []
    })
    expect(wrapper.text()).toContain('资料不足')
  })

  it('prevents selecting more than five documents', async () => {
    const wrapper = mount(DocumentCompareDialog, {
      props: {
        visible: true,
        documents: Array.from({ length: 6 }, (_, index) => ({ id: index + 1, name: `${index + 1}.pdf` }))
      }
    })

    const inputs = wrapper.findAll('input[type="checkbox"]')
    for (const input of inputs.slice(0, 5)) await input.setValue(true)

    expect(inputs[5].attributes('disabled')).toBeDefined()
  })
})
