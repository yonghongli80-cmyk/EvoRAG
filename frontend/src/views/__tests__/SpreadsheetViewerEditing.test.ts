import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/spreadsheets', () => ({
  getSpreadsheetRows: vi.fn(),
  saveSpreadsheetEdits: vi.fn(),
  saveSpreadsheetStructureEdit: vi.fn(),
  getSpreadsheetEditStatus: vi.fn(),
  restoreSpreadsheetVersion: vi.fn(),
}))
vi.mock('../../api/documents', () => ({ fetchDocumentVersions: vi.fn(() => Promise.resolve([])) }))

import {
  getSpreadsheetEditStatus,
  getSpreadsheetRows,
  saveSpreadsheetEdits,
  saveSpreadsheetStructureEdit,
} from '../../api/spreadsheets'
import SpreadsheetViewer from '../SpreadsheetViewer.vue'
import type { SpreadsheetRowsResponse } from '../../api/spreadsheets'

const sheetResponse: SpreadsheetRowsResponse = {
  rows: [{ row_number: 2, values: ['Phone', 6800] }],
  columns: [
    { column_key: 'c_0001', ordinal: 1, display_name: 'product', value_type: 'text' },
    { column_key: 'c_0002', ordinal: 2, display_name: 'price', value_type: 'integer' },
  ],
  can_edit: true,
  current_version_id: 7,
  non_editable_cells: [],
}

async function render() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{
      path: '/documents/:documentId/versions/:versionId/spreadsheet',
      component: SpreadsheetViewer,
    }],
  })
  await router.push('/documents/12/versions/7/spreadsheet?sheet=sheet_0001')
  await router.isReady()
  const wrapper = mount(SpreadsheetViewer, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

describe('SpreadsheetViewer editing', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getSpreadsheetRows).mockResolvedValue(sheetResponse)
  })

  it('tracks typed local changes and can discard them', async () => {
    const { wrapper } = await render()
    await wrapper.get('[data-test="edit-spreadsheet"]').trigger('click')
    const price = wrapper.get('[data-cell="B2"]')
    await price.setValue('6500')

    expect(price.classes()).toContain('is-dirty')
    expect(wrapper.get('[data-test="save-spreadsheet"]').attributes('disabled')).toBeUndefined()

    await wrapper.get('[data-test="discard-spreadsheet"]').trigger('click')
    expect((wrapper.get('[data-cell="B2"]').element as HTMLInputElement).value).toBe('6800')
    expect(wrapper.get('[data-test="save-spreadsheet"]').attributes('disabled')).toBeDefined()
  })

  it('saves typed patches and reloads the activated version', async () => {
    vi.mocked(saveSpreadsheetEdits).mockResolvedValue({
      status: 'pending', session_id: 4, version_id: 8,
    })
    vi.mocked(getSpreadsheetEditStatus).mockResolvedValue({
      session_id: 4, version_id: 8, status: 'active', error_code: '',
    })
    const { wrapper, router } = await render()
    await wrapper.get('[data-test="edit-spreadsheet"]').trigger('click')
    await wrapper.get('[data-cell="B2"]').setValue('6500')
    await wrapper.get('[data-test="save-spreadsheet"]').trigger('click')
    await flushPromises()

    expect(saveSpreadsheetEdits).toHaveBeenCalledWith(12, expect.objectContaining({
      base_version_id: 7,
      changes: [{
        sheet_key: 'sheet_0001', cell: 'B2', value_type: 'number', value: 6500,
      }],
    }))
    expect(router.currentRoute.value.params.versionId).toBe('8')
  })

  it('keeps local edits when the base version conflicts', async () => {
    vi.mocked(saveSpreadsheetEdits).mockRejectedValue({ response: { status: 409 } })
    const { wrapper } = await render()
    await wrapper.get('[data-test="edit-spreadsheet"]').trigger('click')
    await wrapper.get('[data-cell="B2"]').setValue('6500')
    await wrapper.get('[data-test="save-spreadsheet"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('已被其他人更新')
    expect((wrapper.get('[data-cell="B2"]').element as HTMLInputElement).value).toBe('6500')
  })

  it('submits one row structure operation and loads the activated version', async () => {
    vi.mocked(saveSpreadsheetStructureEdit).mockResolvedValue({
      status: 'pending', session_id: 5, version_id: 9,
    })
    vi.mocked(getSpreadsheetEditStatus).mockResolvedValue({
      session_id: 5, version_id: 9, status: 'active', error_code: '',
    })
    const { wrapper, router } = await render()

    await wrapper.get('[data-test="structure-spreadsheet"]').trigger('click')
    await wrapper.get('[data-test="structure-index"]').setValue(2)
    await wrapper.get('[data-test="structure-count"]').setValue(3)
    await wrapper.get('[data-test="apply-structure"]').trigger('click')
    await flushPromises()

    expect(saveSpreadsheetStructureEdit).toHaveBeenCalledWith(12, {
      base_version_id: 7,
      action: 'insert_rows',
      sheet_key: 'sheet_0001',
      index: 2,
      count: 3,
      confirm_nonempty: false,
      reason: '',
    })
    expect(router.currentRoute.value.params.versionId).toBe('9')
  })

  it('adds a named blank worksheet as a structural version', async () => {
    vi.mocked(saveSpreadsheetStructureEdit).mockResolvedValue({
      status: 'pending', session_id: 6, version_id: 10,
    })
    vi.mocked(getSpreadsheetEditStatus).mockResolvedValue({
      session_id: 6, version_id: 10, status: 'active', error_code: '',
    })
    const { wrapper } = await render()

    await wrapper.get('[data-test="structure-spreadsheet"]').trigger('click')
    await wrapper.get('[data-test="structure-action"]').setValue('add_sheet')
    await wrapper.get('[data-test="new-sheet-name"]').setValue('August')
    await wrapper.get('[data-test="apply-structure"]').trigger('click')
    await flushPromises()

    expect(saveSpreadsheetStructureEdit).toHaveBeenCalledWith(12, {
      base_version_id: 7,
      action: 'add_sheet',
      new_sheet_name: 'August',
      reason: '',
    })
  })
})
