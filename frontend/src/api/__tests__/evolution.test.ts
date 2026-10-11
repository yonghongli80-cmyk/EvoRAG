import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  applyCodePatch,
  fetchDiagnosis,
  submitDecision,
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  fetchEvolutionObservations,
  fetchEvolutionStateMachine,
  fetchGapTickets
} from '../evolution'

vi.mock('../../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))

import api from '../../utils/api'

beforeEach(() => vi.clearAllMocks())

describe('fetchEvolutionObservations', () => {
  it('requests the observation page with snake_case query parameters', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { items: [], total: 0, page: 2, page_size: 50 }
    } as any)

    const page = await fetchEvolutionObservations({
      status: 'observed',
      kbId: 'kb1',
      page: 2,
      pageSize: 50
    })

    expect(api.get).toHaveBeenCalledWith('/knowledge-operations/evolution/observations', {
      params: { status: 'observed', kb_id: 'kb1', page: 2, page_size: 50 }
    })
    expect(page.page_size).toBe(50)
  })

  it('omits unset filters instead of sending empty strings', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { items: [], total: 0, page: 1, page_size: 20 }
    } as any)

    await fetchEvolutionObservations()

    expect(api.get).toHaveBeenCalledWith('/knowledge-operations/evolution/observations', {
      params: { page: 1, page_size: 20 }
    })
  })
})

describe('fetchEvolutionClusters', () => {
  it('requests the cluster page with pool and status filters', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { items: [], total: 0, page: 1, page_size: 20 }
    } as any)

    await fetchEvolutionClusters({ pool: 'candidate_pool', status: 'awaiting_diagnosis_review' })

    expect(api.get).toHaveBeenCalledWith('/knowledge-operations/evolution/clusters', {
      params: { pool: 'candidate_pool', status: 'awaiting_diagnosis_review', page: 1, page_size: 20 }
    })
  })
})

describe('fetchEvolutionCluster', () => {
  it('requests one cluster with its members', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { id: 7, members: [] } } as any)

    await fetchEvolutionCluster(7)

    expect(api.get).toHaveBeenCalledWith('/knowledge-operations/evolution/clusters/7')
  })
})

describe('fetchEvolutionStateMachine', () => {
  it('requests the read-only state-machine reference', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { transitions: {}, endpoints: [] } } as any)

    await fetchEvolutionStateMachine()

    expect(api.get).toHaveBeenCalledWith('/knowledge-operations/evolution/state-machine')
  })
})

describe('fetchDiagnosis', () => {
  it('requests the cluster diagnosis read-out', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { cluster_id: 7, cluster_draft: null, member_drafts: [], audit: [] }
    } as any)

    const diagnosis = await fetchDiagnosis(7)

    expect(api.get).toHaveBeenCalledWith('/knowledge-operations/evolution/clusters/7/diagnosis')
    expect(diagnosis.member_drafts).toEqual([])
  })
})

describe('fetchGapTickets', () => {
  it('requests the ticket page and maps the query to snake_case', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { items: [], total: 0, page: 3, page_size: 10 }
    } as any)

    await fetchGapTickets({ status: 'open', page: 3, pageSize: 10 })

    expect(api.get).toHaveBeenCalledWith('/knowledge-operations/evolution/gap-tickets', {
      params: { status: 'open', page: 3, page_size: 10 }
    })
  })

  it('omits an unset status instead of sending an empty filter', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { items: [], total: 0, page: 1, page_size: 20 }
    } as any)

    await fetchGapTickets()

    expect(api.get).toHaveBeenCalledWith('/knowledge-operations/evolution/gap-tickets', {
      params: { page: 1, page_size: 20 }
    })
  })
})

describe('submitDecision with a confirmed layer', () => {
  it('posts confirmed_layer only when one is given', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { id: 7 } } as any)

    await submitDecision(7, 'confirm_layer', '确认层级', 'prompt')

    expect(api.post).toHaveBeenCalledWith(
      '/knowledge-operations/evolution/clusters/7/decision',
      { action: 'confirm_layer', note: '确认层级', confirmed_layer: 'prompt' }
    )
  })

  it('omits the key for the ordinary decisions', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { id: 7 } } as any)

    await submitDecision(7, 'reject', '证据不足')

    expect(api.post).toHaveBeenCalledWith(
      '/knowledge-operations/evolution/clusters/7/decision',
      { action: 'reject', note: '证据不足' }
    )
  })
})

describe('applyCodePatch', () => {
  it('posts the apply request and returns the guard steps', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { applied: true, applied_now: true, steps: { import_smoke: true }, restart_helper: 'h.py' }
    } as any)

    const result = await applyCodePatch(20, true)

    expect(api.post).toHaveBeenCalledWith(
      '/knowledge-operations/evolution/code-candidates/20/apply',
      { restart: true }
    )
    expect(result.applied).toBe(true)
    expect(result.steps.import_smoke).toBe(true)
  })
})
