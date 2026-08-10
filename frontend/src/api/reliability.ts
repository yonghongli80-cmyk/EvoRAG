import api from '../utils/api'

export interface ComparisonRequest {
  question: string
  document_ids: number[]
  dimensions: string[]
}

export interface ComparisonCell {
  status: 'supported' | 'insufficient_evidence'
  text: string
  citation_ids: number[]
}

export interface ComparisonRow {
  dimension: string
  values: Record<number, ComparisonCell>
}

export interface ComparisonResult {
  comparison_id: number
  rows: ComparisonRow[]
  partial: boolean
  failures: string[]
}

export async function compareDocuments(
  payload: ComparisonRequest
): Promise<ComparisonResult> {
  const response = await api.post('/reliability/compare', payload)
  return response.data
}
