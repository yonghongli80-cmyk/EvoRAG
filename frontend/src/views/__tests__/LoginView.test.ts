import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { describe, expect, it, vi } from 'vitest'
import LoginView from '../LoginView.vue'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

describe('LoginView', () => {
  it('shows only direct organization account login', () => {
    const wrapper = mount(LoginView, {
      global: { plugins: [createTestingPinia({ createSpy: vi.fn })] },
    })

    expect(wrapper.text()).toContain('登录')
    expect(wrapper.text()).not.toContain('注册')
    expect(wrapper.text()).not.toContain('选择员工')
    expect(wrapper.find('input[autocomplete="username"]').exists()).toBe(true)
  })
})
