import api from '../utils/api'

export interface SpreadsheetRow { row_number: number; values: unknown[] }
export interface SpreadsheetColumn {
  column_key: string
  ordinal: number
  display_name: string
  value_type: 'text' | 'integer' | 'decimal' | 'boolean' | 'date' | 'datetime'
}
export interface SpreadsheetRowsResponse {
  rows: SpreadsheetRow[]
  columns: SpreadsheetColumn[]
  can_edit: boolean
  current_version_id: number
  non_editable_cells: string[]
}
export type SpreadsheetCellValueType = 'text' | 'number' | 'date' | 'boolean' | 'blank'
export interface SpreadsheetCellPatch {
  sheet_key: string
  cell: string
  value_type: SpreadsheetCellValueType
  value: string | number | boolean | null
}
export interface SpreadsheetEditRequest {
  base_version_id: number
  changes: SpreadsheetCellPatch[]
  reason?: string
}
export type SpreadsheetStructureAction =
  | 'insert_rows'
  | 'delete_rows'
  | 'insert_columns'
  | 'delete_columns'
  | 'add_sheet'
export interface SpreadsheetStructureEditRequest {
  base_version_id: number
  action: SpreadsheetStructureAction
  sheet_key?: string
  index?: number
  count?: number
  new_sheet_name?: string
  confirm_nonempty?: boolean
  reason?: string
}
export interface SpreadsheetEditAccepted {
  status: 'pending'
  session_id: number
  version_id: number
}
export interface SpreadsheetEditUnchanged { status: 'unchanged' }
export interface SpreadsheetEditStatus {
  session_id: number
  version_id: number
  status: 'pending' | 'active' | 'failed' | 'cancelled' | 'rejected'
  error_code: string
}

export async function getSpreadsheetRows(documentId: number, versionId: number, sheetKey: string, afterRow = 0) {
  const response = await api.get<SpreadsheetRowsResponse>(`/documents/${documentId}/versions/${versionId}/sheets/${encodeURIComponent(sheetKey)}/rows`, { params: { after_row: afterRow, limit: 100 } })
  return response.data
}

export async function saveSpreadsheetEdits(documentId: number, request: SpreadsheetEditRequest) {
  const response = await api.post<SpreadsheetEditAccepted | SpreadsheetEditUnchanged>(
    `/documents/${documentId}/spreadsheet-edits`, request,
  )
  return response.data
}

export async function saveSpreadsheetStructureEdit(
  documentId: number,
  request: SpreadsheetStructureEditRequest,
) {
  const response = await api.post<SpreadsheetEditAccepted>(
    `/documents/${documentId}/spreadsheet-structure-edits`, request,
  )
  return response.data
}

export async function getSpreadsheetEditStatus(documentId: number, sessionId: number) {
  const response = await api.get<SpreadsheetEditStatus>(
    `/documents/${documentId}/spreadsheet-edits/${sessionId}`,
  )
  return response.data
}

export async function restoreSpreadsheetVersion(
  documentId: number,
  sourceVersionId: number,
  request: { base_version_id: number; reason?: string },
) {
  const response = await api.post<SpreadsheetEditAccepted>(
    `/documents/${documentId}/versions/${sourceVersionId}/restore`, request,
  )
  return response.data
}

export interface SpreadsheetChangeAudit {
  session_id: number
  document_id: number
  document_name: string
  kb_id: string
  base_version_id: number
  new_version_id: number | null
  actor_user_id: number
  actor_name: string
  operation: 'edit' | 'rollback'
  status: string
  change_count: number
  restored_from_version_id: number | null
  reason: string
  risk_flags: string[]
  created_at: number
  activated_at: number | null
  error_code: string
  structure_action?: SpreadsheetStructureAction | null
}

export interface SpreadsheetChangeDetail extends SpreadsheetChangeAudit {
  sheets: Array<{
    sheet_key: string
    sheet_name: string
    changes: Array<{
      cell: string
      old_value_type: string
      old_value?: unknown
      new_value_type: string
      new_value?: unknown
      redacted?: boolean
    }>
  }>
  structure_change: {
    action: SpreadsheetStructureAction
    sheet_key: string | null
    sheet_name: string | null
    index: number | null
    count: number
    new_sheet_name: string | null
    affected_nonempty_cells: number
    confirm_nonempty: boolean
  } | null
}

export async function getSpreadsheetChanges(params: Record<string, unknown> = {}) {
  const response = await api.get<{ items: SpreadsheetChangeAudit[]; next_cursor: string }>(
    '/admin/spreadsheet-changes', { params },
  )
  return response.data
}

export async function getSpreadsheetChangeDetail(sessionId: number) {
  const response = await api.get<SpreadsheetChangeDetail>(
    `/admin/spreadsheet-changes/${sessionId}`,
  )
  return response.data
}
