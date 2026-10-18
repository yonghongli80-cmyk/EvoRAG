export interface KBDocument {
  filename: string
  chunk_count?: number
  status?: string
  toc?: string
  document_id?: number
  document_version_id?: number
  _showToc?: boolean
}

export function isSpreadsheetDocument(doc: { filename?: string; document_id?: number; document_version_id?: number }) {
  const extension = doc.filename?.split('.').pop()?.toLowerCase()
  return (extension === 'xlsx' || extension === 'csv')
    && typeof doc.document_id === 'number'
    && typeof doc.document_version_id === 'number'
}

export function parseToc(toc: string | object | undefined): any {
  if (!toc) return {}
  if (typeof toc === 'string') {
    try { return JSON.parse(toc) } catch { return {} }
  }
  return toc
}

export function getStatusType(status: string) {
  switch (status) {
    case 'indexed': return 'success'
    case 'pending': return 'warning'
    case 'error': return 'danger'
    default: return 'info'
  }
}

export function getStatusLabel(status: string) {
  switch (status) {
    case 'indexed': return '已索引'
    case 'pending': return '处理中'
    case 'error': return '错误'
    default: return status || '未知'
  }
}
