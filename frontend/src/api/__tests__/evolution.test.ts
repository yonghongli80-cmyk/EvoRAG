import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchEvolutionCluster,
  fetchEvolutionClusters,
  fetchEvolutionObservations,
  fetchEvolutionStateMachine
} from '../evolution'

vi.mock('../../utils/api', () => ({ default: { get: vi.fn() } }))

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
