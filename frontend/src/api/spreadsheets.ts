import api from '../utils/api'

export interface SpreadsheetRow { row_number: number; values: unknown[] }

export async function getSpreadsheetRows(documentId: number, versionId: number, sheetKey: string, afterRow = 0) {
  const response = await api.get<{ rows: SpreadsheetRow[] }>(`/documents/${documentId}/versions/${versionId}/sheets/${encodeURIComponent(sheetKey)}/rows`, { params: { after_row: afterRow, limit: 100 } })
  return response.data
}
