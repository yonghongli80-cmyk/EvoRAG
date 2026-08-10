import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import MessageBubble from '../MessageBubble.vue'

const mockSendFeedback = vi.fn()
const mockRegenerate = vi.fn()

// Shared mutable state so tests can toggle isStreaming
let mockIsStreaming = false

vi.mock('../../stores/chat', () => ({
  useChatStore: () => ({
    sendFeedback: mockSendFeedback,
    get isStreaming() { return mockIsStreaming },
    regenerate: mockRegenerate
  })
}))

vi.mock('dompurify', () => ({
  default: {
    sanitize: vi.fn((html: string) => html)
  }
}))

beforeEach(() => {
  vi.clearAllMocks()
})

describe('MessageBubble', () => {
  it('renders persisted reliability without a percentage', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: {
          role: 'assistant',
          content: 'Answer',
          confidence_level: 'medium',
          confidence_reasons: ['单一当前来源']
        },
        index: 1
      }
    })

    expect(wrapper.text()).toContain('置信度：中')
    expect(wrapper.text()).toContain('单一当前来源')
    expect(wrapper.text()).not.toContain('%')
  })

  // ── User message rendering ──────────────────────────────
  it('renders user message with user class for right alignment', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'user', content: 'Hello' },
        index: 0
      }
    })

    const msg = wrapper.find('.msg')
    expect(msg.classes()).toContain('user')
  })

  it('renders user bubble with content', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'user', content: 'What is RAG?' },
        index: 0
      }
    })

    expect(wrapper.find('.bubble').exists()).toBe(true)
    expect(wrapper.find('.bubble').text()).toContain('What is RAG?')
  })

  it('shows user avatar for user messages', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'user', content: 'Hello' },
        index: 0
      }
    })

    expect(wrapper.find('.user-avatar').exists()).toBe(true)
  })

  // ── AI message rendering ────────────────────────────────
  it('renders AI message with assistant class for left alignment', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'I am an AI' },
        index: 1
      }
    })

    const msg = wrapper.find('.msg')
    expect(msg.classes()).toContain('assistant')
  })

  it('renders AI bubble with content', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'The answer is 42' },
        index: 1
      }
    })

    expect(wrapper.find('.bubble').text()).toContain('The answer is 42')
  })

  it('shows AI avatar for assistant messages', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'Hello' },
        index: 1
      }
    })

    expect(wrapper.find('.ai-avatar').exists()).toBe(true)
  })

  // ── Sources ─────────────────────────────────────────────
  it('shows source references when sources are present', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: {
          role: 'assistant',
          content: 'Answer',
          sources: [
            { doc_name: 'paper.pdf' },
            { doc_name: 'notes.md' }
          ]
        },
        index: 1
      }
    })

    const chips = wrapper.findAll('.source-chip')
    expect(chips).toHaveLength(2)
    expect(chips[0].text()).toBe('1. paper.pdf')
    expect(chips[1].text()).toBe('2. notes.md')
  })

  it('does not show sources when sources array is empty', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: {
          role: 'assistant',
          content: 'Answer',
          sources: []
        },
        index: 1
      }
    })

    expect(wrapper.find('.sources').exists()).toBe(false)
  })

  it('does not show sources when sources is undefined', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: {
          role: 'assistant',
          content: 'Answer'
        },
        index: 1
      }
    })

    expect(wrapper.find('.sources').exists()).toBe(false)
  })

  // ── Action buttons ──────────────────────────────────────
  it('shows action buttons for assistant messages with content', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'Answer' },
        index: 1
      }
    })

    expect(wrapper.find('.actions').exists()).toBe(true)
    const buttons = wrapper.findAll('.action-btn')
    expect(buttons.length).toBeGreaterThanOrEqual(3)
  })

  it('does not show action buttons for user messages', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'user', content: 'Question' },
        index: 0
      }
    })

    expect(wrapper.find('.actions').exists()).toBe(false)
  })

  it('does not show action buttons when assistant content is empty', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: '' },
        index: 1
      }
    })

    expect(wrapper.find('.actions').exists()).toBe(false)
  })

  it('shows add-to-analysis button with label', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'Answer' },
        index: 1
      }
    })

    const addBtn = wrapper.find('.action-btn--accent')
    expect(addBtn.exists()).toBe(true)
    expect(addBtn.text()).toContain('添加到分析')
  })

  // ── Feedback button interactions ────────────────────────
  it('calls sendFeedback with positive when thumbs-up clicked', async () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { id: 5, role: 'assistant', content: 'Answer' },
        index: 1
      }
    })

    const buttons = wrapper.findAll('.action-btn')
    // First button is thumbs up
    await buttons[0].trigger('click')

    expect(mockSendFeedback).toHaveBeenCalledWith(1, 'positive')
  })

  it('calls sendFeedback with negative when thumbs-down clicked', async () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { id: 5, role: 'assistant', content: 'Answer' },
        index: 1
      }
    })

    const buttons = wrapper.findAll('.action-btn')
    // Second button is thumbs down
    await buttons[1].trigger('click')

    expect(mockSendFeedback).toHaveBeenCalledWith(1, 'negative')
  })

  // ── Add to analysis emit ────────────────────────────────
  it('emits add-to-analysis with question and answer', async () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'RAG is great' },
        index: 1,
        question: 'What is RAG?'
      }
    })

    const addBtn = wrapper.find('.action-btn--accent')
    await addBtn.trigger('click')

    expect(wrapper.emitted('add-to-analysis')).toBeTruthy()
    expect(wrapper.emitted('add-to-analysis')![0]).toEqual(['What is RAG?', 'RAG is great'])
  })

  it('emits add-to-analysis with empty question when no question prop', async () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'Answer' },
        index: 1
      }
    })

    const addBtn = wrapper.find('.action-btn--accent')
    await addBtn.trigger('click')

    expect(wrapper.emitted('add-to-analysis')![0]).toEqual(['', 'Answer'])
  })

  // ── Active feedback styling ─────────────────────────────
  it('applies active class when feedback is positive', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { id: 5, role: 'assistant', content: 'Answer', feedback: 'positive' },
        index: 1
      }
    })

    const buttons = wrapper.findAll('.action-btn')
    expect(buttons[0].classes()).toContain('active')
  })

  it('applies active class when feedback is negative', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { id: 5, role: 'assistant', content: 'Answer', feedback: 'negative' },
        index: 1
      }
    })

    const buttons = wrapper.findAll('.action-btn')
    expect(buttons[1].classes()).toContain('active')
  })

  // ── formatContent ────────────────────────────────────────
  it('escapes HTML tags in content', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: '<script>alert("xss")</script>' },
        index: 1
      }
    })

    expect(wrapper.find('.bubble').html()).not.toContain('<script>')
    expect(wrapper.find('.bubble').html()).toContain('&lt;script&gt;')
  })

  it('converts newlines to <br> tags', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'Line 1\nLine 2' },
        index: 1
      }
    })

    expect(wrapper.find('.bubble').html()).toContain('Line 1<br>Line 2')
  })

  it('converts [ref] markers to <span class="ref">', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'See [Source 1] for details' },
        index: 1
      }
    })

    expect(wrapper.find('.bubble').html()).toContain('<span class="ref">Source 1</span>')
  })

  it('calls DOMPurify.sanitize on formatted content', async () => {
    const DOMPurify = await import('dompurify')
    const spy = vi.mocked(DOMPurify.default.sanitize)

    mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'Hello' },
        index: 1
      }
    })

    expect(spy).toHaveBeenCalled()
    spy.mockClear()
  })

  // ── Regenerate disabled during streaming ──────────────────
  it('disables regenerate button when isStreaming is true', () => {
    mockIsStreaming = true

    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'Answer' },
        index: 1
      }
    })

    // Third action button is regenerate (index 2)
    const buttons = wrapper.findAll('.action-btn')
    const regenerateBtn = buttons[2]
    expect(regenerateBtn.attributes('disabled')).toBeDefined()

    mockIsStreaming = false
  })

  it('enables regenerate button when isStreaming is false', () => {
    mockIsStreaming = false

    const wrapper = mount(MessageBubble, {
      props: {
        message: { role: 'assistant', content: 'Answer' },
        index: 1
      }
    })

    const buttons = wrapper.findAll('.action-btn')
    const regenerateBtn = buttons[2]
    expect(regenerateBtn.attributes('disabled')).toBeUndefined()
  })
})
