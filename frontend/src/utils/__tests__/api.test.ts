import { describe, it, expect, vi, beforeEach } from 'vitest'

const { mockInterceptorUse, mockAxiosInstance } = vi.hoisted(() => {
  const mockInterceptorUse = vi.fn()
  const mockAxiosInstance = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    interceptors: {
      response: { use: mockInterceptorUse }
    }
  }
  return { mockInterceptorUse, mockAxiosInstance }
})

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => mockAxiosInstance),
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    interceptors: { response: { use: vi.fn() } }
  }
}))

vi.mock('element-plus', () => ({
  ElMessage: {
    error: vi.fn(),
    success: vi.fn()
  }
}))

import { ElMessage } from 'element-plus'
import axios from 'axios'

describe('api base URL', () => {
  it('uses the /api dev-proxy prefix by default', async () => {
    vi.resetModules()
    await import('../api')

    expect(axios.create).toHaveBeenCalledWith({ baseURL: '/api' })
  })
})

describe('api interceptor', () => {
  let responseErrorInterceptor: (error: any) => Promise<never>

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    localStorage.clear()
    await import('../api')
    const calls = mockInterceptorUse.mock.calls
    responseErrorInterceptor = calls[0]?.[1]
  })

  it('401 response clears token and rejects', async () => {
    localStorage.setItem('rag_token', 'expired')
    const error = { response: { status: 401 } }

    await expect(responseErrorInterceptor(error)).rejects.toBeDefined()

    expect(localStorage.getItem('rag_token')).toBeNull()
  })

  it('403 response shows permission error', async () => {
    const error = { response: { status: 403 } }

    await expect(responseErrorInterceptor(error)).rejects.toBeDefined()

    expect(ElMessage.error).toHaveBeenCalledWith('权限不足，无法执行此操作')
  })

  it('500 response shows server error with detail when available', async () => {
    const error = { response: { status: 500, data: { detail: 'LLM 生成失败' } } }

    await expect(responseErrorInterceptor(error)).rejects.toBeDefined()

    expect(ElMessage.error).toHaveBeenCalledWith('LLM 生成失败')
  })

  it('500 response shows default error when no detail', async () => {
    const error = { response: { status: 500 } }

    await expect(responseErrorInterceptor(error)).rejects.toBeDefined()

    expect(ElMessage.error).toHaveBeenCalledWith('服务器错误，请稍后重试')
  })
})

describe('withRetry', () => {
  it('returns result on first success', async () => {
    const { withRetry } = await import('../api')
    const fn = vi.fn().mockResolvedValue('ok')
    const result = await withRetry(fn, 2, 10)
    expect(result).toBe('ok')
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('retries on network error then succeeds', async () => {
    const { withRetry } = await import('../api')
    const fn = vi.fn()
      .mockRejectedValueOnce({ message: 'Network Error' })
      .mockResolvedValue('recovered')
    const result = await withRetry(fn, 2, 10)
    expect(result).toBe('recovered')
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('throws after exhausting retries', async () => {
    const { withRetry } = await import('../api')
    const fn = vi.fn().mockRejectedValue({ message: 'Network Error' })
    await expect(withRetry(fn, 2, 10)).rejects.toThrow()
    expect(fn).toHaveBeenCalledTimes(3)
  })

  it('does not retry non-network errors', async () => {
    const { withRetry } = await import('../api')
    const fn = vi.fn().mockRejectedValue({ response: { status: 500 } })
    await expect(withRetry(fn, 2, 10)).rejects.toThrow()
    expect(fn).toHaveBeenCalledTimes(1)
  })
})
