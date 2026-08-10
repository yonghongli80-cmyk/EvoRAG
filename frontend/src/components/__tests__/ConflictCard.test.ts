import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ConflictCard from '../ConflictCard.vue'

const { mockResolveConflict } = vi.hoisted(() => ({
  mockResolveConflict: vi.fn()
}))

vi.mock('../../api/knowledgeOperations', () => ({
  resolveConflict: mockResolveConflict
}))

describe('ConflictCard', () => {
  it('resolves a conflict through the controlled endpoint without displaying document details', async () => {
    mockResolveConflict.mockResolvedValue({ id: 7, status: 'resolved' })
    const wrapper = mount(ConflictCard, {
      props: { conflictId: 7, status: 'open' }
    })

    await wrapper.find('button').trigger('click')

    expect(mockResolveConflict).toHaveBeenCalledWith(7, 'resolved', '')
    expect(wrapper.text()).toContain('已解决')
    expect(wrapper.emitted('resolved')).toEqual([[{ id: 7, status: 'resolved' }]])
    expect(wrapper.text()).not.toContain('.pdf')
  })
})
