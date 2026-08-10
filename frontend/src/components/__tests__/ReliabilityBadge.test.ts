import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ReliabilityBadge from '../ReliabilityBadge.vue'

describe('ReliabilityBadge', () => {
  it('renders confidence reasons without percentages', () => {
    const wrapper = mount(ReliabilityBadge, {
      props: { level: 'medium', reasons: ['单一当前来源'] }
    })

    expect(wrapper.text()).toContain('中')
    expect(wrapper.text()).toContain('单一当前来源')
    expect(wrapper.text()).not.toContain('%')
  })
})
