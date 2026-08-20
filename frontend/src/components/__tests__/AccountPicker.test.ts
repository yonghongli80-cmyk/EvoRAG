import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AccountPicker from '../AccountPicker.vue'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../utils/api', () => ({ default: { get } }))

describe('AccountPicker', () => {
  beforeEach(() => get.mockReset())

  it('selects the fixed account without exposing a password or environment label', async () => {
    get.mockResolvedValue({ data: [{ username: 'demo001', person_code: 'P-001', display_name: '林知远', job_title: '公司管理员', department: '星河协作集团' }] })
    const wrapper = mount(AccountPicker, { props: { modelValue: '' } })
    await wrapper.get('[data-test="account-search"]').setValue('林')
    await flushPromises()
    await wrapper.get('[data-test="account-demo001"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['demo001'])
    expect(wrapper.text()).not.toContain('测试账号')
    expect(wrapper.text()).not.toContain('演示环境')
    expect(wrapper.text()).not.toContain('123456')
  })
})
