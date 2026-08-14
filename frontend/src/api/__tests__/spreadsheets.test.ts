import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../utils/api', () => ({
  default: { get: vi.fn(), post: vi.fn() }
}))

import api from '../../utils/api'
import {
  getSpreadsheetEditStatus,
  saveSpreadsheetEdits,
  saveSpreadsheetStructureEdit,
} from '../spreadsheets'

describe('spreadsheet edit API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('sends typed patches against the base version', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { status: 'pending', session_id: 3, version_id: 8 }
    } as any)

    await saveSpreadsheetEdits(12, {
      base_version_id: 7,
      changes: [
        { sheet_key: 'sheet_0001', cell: 'B12', value_type: 'number', value: 6500 }
      ],
      reason: 'price update'
    })

    expect(api.post).toHaveBeenCalledWith('/documents/12/spreadsheet-edits', {
      base_version_id: 7,
      changes: [
        { sheet_key: 'sheet_0001', cell: 'B12', value_type: 'number', value: 6500 }
      ],
      reason: 'price update'
    })
  })

  it('reads a protected edit status by document and session', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { session_id: 3, version_id: 8, status: 'active', error_code: '' }
    } as any)

    await getSpreadsheetEditStatus(12, 3)

    expect(api.get).toHaveBeenCalledWith('/documents/12/spreadsheet-edits/3')
  })

  it('sends one structural operation against the base version', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { status: 'pending', session_id: 4, version_id: 9 }
    } as any)

    await saveSpreadsheetStructureEdit(12, {
      base_version_id: 7,
      action: 'insert_rows',
      sheet_key: 'sheet_0001',
      index: 2,
      count: 3,
    })

    expect(api.post).toHaveBeenCalledWith(
      '/documents/12/spreadsheet-structure-edits',
      {
        base_version_id: 7,
        action: 'insert_rows',
        sheet_key: 'sheet_0001',
        index: 2,
        count: 3,
      },
    )
  })
})
