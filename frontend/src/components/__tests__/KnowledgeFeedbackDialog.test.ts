import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import KnowledgeFeedbackDialog from '../KnowledgeFeedbackDialog.vue'

function citation(id: number) {
  return {
    citation_id: id,
    doc_name: `doc${id}.pdf`,
    kb_id: 'rag_docs',
    section_title: 'Section',
    version_no: 1,
    updated_at: 0,
    is_historical: false,
    text_preview: 'preview',
    download_url: `/documents/1/versions/4/download`
  }
}

describe('KnowledgeFeedbackDialog', () => {
  it('requires a citation for outdated feedback with multiple sources', async () => {
    const wrapper = mount(KnowledgeFeedbackDialog, {
      props: {
        visible: true,
        assistantMessageId: 7,
        kind: 'outdated',
        citations: [citation(1), citation(2)]
      }
    })

    await wrapper.get('[data-test=submit-feedback]').trigger('click')

    expect(wrapper.text()).toContain('请选择内容过期的来源')
    expect(wrapper.emitted('submit')).toBeUndefined()
  })

  it('emits the feedback payload after selecting a citation', async () => {
    const wrapper = mount(KnowledgeFeedbackDialog, {
      props: {
        visible: true,
        assistantMessageId: 7,
        kind: 'outdated',
        citations: [citation(1), citation(2)]
      }
    })

    await wrapper.get('[data-test=citation-select]').setValue('1')
    await wrapper.get('[data-test=submit-feedback]').trigger('click')

    const payload = wrapper.emitted('submit')?.[0]?.[0] as any
    expect(payload.assistant_message_id).toBe(7)
    expect(payload.kind).toBe('outdated')
    expect(payload.citation_id).toBe(1)
  })
})
