import { describe, it, expect, vi } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import ChatInput from '../ChatInput.vue'
import { useChatStore } from '../../stores/chat'

function mountInput() {
  const pinia = createTestingPinia({
    createSpy: vi.fn,
    initialState: { chat: {} }
  })
  const wrapper = mount(ChatInput, {
    props: { mode: 'file' },
    global: {
      plugins: [pinia],
      stubs: {
        'el-input': {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template:
            '<textarea class="el-input-stub" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
        },
        'el-select': true,
        'el-option': true
      }
    }
  })
  return { wrapper, store: useChatStore() }
}

describe('ChatInput', () => {
  it('sends the trimmed question with the current mode', async () => {
    const { wrapper, store } = mountInput()

    await wrapper.get('textarea').setValue('  question  ')
    await wrapper.get('.send-btn').trigger('click')

    expect(store.sendMessage).toHaveBeenCalledWith('question', 'file', undefined)
  })

  it('sends on Enter but not on Shift+Enter', async () => {
    const { wrapper, store } = mountInput()

    await wrapper.get('textarea').setValue('question')
    await wrapper.get('textarea').trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(store.sendMessage).not.toHaveBeenCalled()

    await wrapper.get('textarea').trigger('keydown', { key: 'Enter', shiftKey: false })
    expect(store.sendMessage).toHaveBeenCalledWith('question', 'file', undefined)
  })

  it('replaces the send button with a stop button while streaming', async () => {
    const { wrapper, store } = mountInput()
    expect(wrapper.find('.send-btn').exists()).toBe(true)
    expect(wrapper.find('[data-test="stop-streaming"]').exists()).toBe(false)

    store.activeRequestId = 4
    await nextTick()

    expect(wrapper.find('.send-btn').exists()).toBe(false)
    expect(wrapper.find('[data-test="stop-streaming"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('停止')
  })

  it('stops the active stream when the stop button is clicked', async () => {
    const { wrapper, store } = mountInput()
    store.activeRequestId = 7
    await nextTick()

    await wrapper.get('[data-test="stop-streaming"]').trigger('click')

    expect(store.stopStreaming).toHaveBeenCalledTimes(1)
  })
})
