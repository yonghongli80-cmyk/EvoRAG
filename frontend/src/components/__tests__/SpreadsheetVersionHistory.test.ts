import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SpreadsheetVersionHistory from '../SpreadsheetVersionHistory.vue'

const versions = [
  { id: 4, document_id: 12, version_no: 4, filename: 'prices.xlsx', status: 'current', validity_status: 'valid', created_at: 4, superseded_at: null, content_hash: '4', restored_from_version_id: 1 },
  { id: 3, document_id: 12, version_no: 3, filename: 'prices.xlsx', status: 'historical', validity_status: 'valid', created_at: 3, superseded_at: 4, content_hash: '3', restored_from_version_id: null },
  { id: 2, document_id: 12, version_no: 2, filename: 'prices.xlsx', status: 'historical', validity_status: 'valid', created_at: 2, superseded_at: 3, content_hash: '2', restored_from_version_id: null },
  { id: 1, document_id: 12, version_no: 1, filename: 'prices.xlsx', status: 'historical', validity_status: 'valid', created_at: 1, superseded_at: 2, content_hash: '1', restored_from_version_id: null },
]

describe('SpreadsheetVersionHistory', () => {
  it('retains every version and shows restore provenance', () => {
    const wrapper = mount(SpreadsheetVersionHistory, {
      props: { versions, canEdit: true, currentVersionId: 4 },
    })

    expect(wrapper.findAll('[data-version]').map(node => node.attributes('data-version'))).toEqual(['4', '3', '2', '1'])
    expect(wrapper.text()).toContain('由 v1 回退生成')
    expect(wrapper.findAll('[data-test="restore-version"]')).toHaveLength(3)
  })

  it('emits the historical source version and never offers restore to viewers', async () => {
    const editor = mount(SpreadsheetVersionHistory, {
      props: { versions, canEdit: true, currentVersionId: 4 },
    })
    await editor.findAll('[data-test="restore-version"]')[2].trigger('click')
    expect(editor.emitted('restore')?.[0]).toEqual([1])

    const viewer = mount(SpreadsheetVersionHistory, {
      props: { versions, canEdit: false, currentVersionId: 4 },
    })
    expect(viewer.find('[data-test="restore-version"]').exists()).toBe(false)
  })
})
