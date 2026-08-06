import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import MaterialJobStatus from '../MaterialJobStatus.vue'

describe('MaterialJobStatus', () => {
  it('renders queued status', () => {
    const wrapper = mount(MaterialJobStatus, { props: { status: 'queued' } })
    expect(wrapper.get('[data-test=job-queued]').text()).toContain('排队中')
  })

  it('renders archive-only status', () => {
    const wrapper = mount(MaterialJobStatus, { props: { status: 'archive_only' } })
    expect(wrapper.get('[data-test=job-archive-only]').text()).toContain('仅归档')
  })

  it('renders failure with error code', () => {
    const wrapper = mount(MaterialJobStatus, { props: { status: 'failed', errorCode: 'stale_version' } })
    expect(wrapper.get('[data-test=job-failed]').text()).toContain('stale_version')
  })
})
