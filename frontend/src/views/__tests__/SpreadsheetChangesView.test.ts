import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/spreadsheets', () => ({
  getSpreadsheetChanges: vi.fn(),
  getSpreadsheetChangeDetail: vi.fn(),
}))

import {
  getSpreadsheetChangeDetail,
  getSpreadsheetChanges,
} from '../../api/spreadsheets'
import SpreadsheetChangesView from '../SpreadsheetChangesView.vue'
import type { SpreadsheetChangeAudit } from '../../api/spreadsheets'

const item: SpreadsheetChangeAudit = {
  session_id: 9,
  document_id: 12,
  document_name: 'prices.xlsx',
  kb_id: 'rag_docs',
  base_version_id: 7,
  new_version_id: 8,
  actor_user_id: 3,
  actor_name: 'Alice',
  operation: 'edit',
  status: 'active',
  change_count: 1,
  restored_from_version_id: null,
  reason: 'price correction',
  risk_flags: ['public_document_edit'],
  created_at: 1,
  activated_at: 2,
  error_code: '',
}

describe('SpreadsheetChangesView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getSpreadsheetChanges).mockResolvedValue({ items: [item], next_cursor: '' })
  })

  it('shows actor, status and risk, then groups detail diffs', async () => {
    vi.mocked(getSpreadsheetChangeDetail).mockResolvedValue({
      ...item,
      sheets: [{
        sheet_key: 'sheet_0001',
        sheet_name: 'Prices',
        changes: [{ cell: 'B2', old_value_type: 'number', old_value: 6800, new_value_type: 'number', new_value: 6500 }],
      }],
      structure_change: null,
    })
    const wrapper = mount(SpreadsheetChangesView)
    await flushPromises()

    expect(wrapper.text()).toContain('Alice')
    expect(wrapper.text()).toContain('public_document_edit')
    await wrapper.get('[data-test="audit-detail"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Prices')
    expect(wrapper.text()).toContain('B2')
    expect(wrapper.text()).toContain('6800')
    expect(wrapper.text()).toContain('6500')
  })

  it('renders an explicit empty state', async () => {
    vi.mocked(getSpreadsheetChanges).mockResolvedValue({ items: [], next_cursor: '' })
    const wrapper = mount(SpreadsheetChangesView)
    await flushPromises()
    expect(wrapper.get('[data-test="audit-empty"]').text()).toContain('暂无表格变更')
  })

  it('shows structural operations in the list and detail', async () => {
    const structureItem = { ...item, structure_action: 'insert_rows' as const }
    vi.mocked(getSpreadsheetChanges).mockResolvedValue({
      items: [structureItem], next_cursor: '',
    })
    vi.mocked(getSpreadsheetChangeDetail).mockResolvedValue({
      ...structureItem,
      sheets: [],
      structure_change: {
        action: 'insert_rows',
        sheet_key: 'sheet_0001',
        sheet_name: 'Prices',
        index: 2,
        count: 3,
        new_sheet_name: null,
        affected_nonempty_cells: 0,
        confirm_nonempty: false,
      },
    })

    const wrapper = mount(SpreadsheetChangesView)
    await flushPromises()
    expect(wrapper.text()).toContain('插入整行')
    await wrapper.get('[data-test="audit-detail"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="structure-detail"]').text()).toContain('Prices')
    expect(wrapper.get('[data-test="structure-detail"]').text()).toContain('2')
    expect(wrapper.get('[data-test="structure-detail"]').text()).toContain('3')
  })
})
