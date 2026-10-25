import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const { mockRequestInterceptorUse, mockInterceptorUse, mockAxiosInstance } = vi.hoisted(() => {
  const mockRequestInterceptorUse = vi.fn()
  const mockInterceptorUse = vi.fn()
  const mockAxiosInstance = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    interceptors: {
      request: { use: mockRequestInterceptorUse },
      response: { use: mockInterceptorUse }
    }
  }
  return { mockRequestInterceptorUse, mockInterceptorUse, mockAxiosInstance }
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

function stubWindowLocation(pathname: string) {
  const location = { pathname, href: pathname }
  vi.stubGlobal('window', { location })
  return location
}

describe('api base URL', () => {
  it('uses same-origin root for production builds', async () => {
    const { resolveApiBase } = await import('../api')

    expect(resolveApiBase(undefined, false)).toBe('')
  })

  it('honors an explicitly configured base URL', async () => {
    const { resolveApiBase } = await import('../api')

    expect(resolveApiBase('https://api.example.test', false)).toBe('https://api.example.test')
  })

  it('uses the /api dev-proxy prefix by default', async () => {
    vi.resetModules()
    await import('../api')

    expect(axios.create).toHaveBeenCalledWith({
      baseURL: '/api',
      withCredentials: true,
      timeout: 30_000,
    })
  })

  it('bounds every non-streaming request with a 30s timeout', async () => {
    const { REQUEST_TIMEOUT_MS } = await import('../api')

    expect(REQUEST_TIMEOUT_MS).toBe(30_000)
  })

  it('routes streaming fetches through the same base URL', async () => {
    const response = { ok: true }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
    const { streamRequest } = await import('../api')

    await streamRequest('/query/stream', { method: 'POST' })

    const [url, init] = vi.mocked(fetch).mock.calls[0]
    expect(url).toBe('/api/query/stream')
    expect(init?.method).toBe('POST')
    expect(new Headers(init?.headers).get('Authorization')).toBeNull()
    expect(init?.credentials).toBe('same-origin')
  })
})

describe('api interceptor', () => {
  let responseErrorInterceptor: (error: any) => Promise<never>

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    localStorage.clear()
    await import('../api')
    const calls = mockInterceptorUse.mock.calls
    responseErrorInterceptor = calls[0]?.[1]
  })

  it('captures session ownership without installing a local-storage bearer token', async () => {
    localStorage.setItem('rag_token', 'current-token')
    const { registerSessionResetHandler } = await import('../api')
    registerSessionResetHandler(() => {}, () => 7)
    const requestInterceptor = mockRequestInterceptorUse.mock.calls[0]?.[0]
    const config = requestInterceptor({ headers: {} })

    expect(config.authSessionGeneration).toBe(7)
    expect(config.headers.Authorization).toBeUndefined()
  })

  it('401 response rejects without relying on browser token storage', async () => {
    const location = stubWindowLocation('/files')
    const error = { response: { status: 401 } }

    await expect(responseErrorInterceptor(error)).rejects.toBeDefined()
    expect(mockRequestInterceptorUse).toHaveBeenCalledTimes(1)
    expect(location.href).toBe('/login')
  })

  it('does not redirect an unauthenticated login page back to itself', async () => {
    const { shouldRedirectToLogin } = await import('../api')

    expect(shouldRedirectToLogin('/login')).toBe(false)
    expect(shouldRedirectToLogin('/files')).toBe(true)
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

  it('500 response falls back to data.error when detail is missing', async () => {
    const error = { response: { status: 500, data: { error: 'LLM 暂不可用' } } }

    await expect(responseErrorInterceptor(error)).rejects.toBeDefined()

    expect(ElMessage.error).toHaveBeenCalledWith('LLM 暂不可用')
  })

  it('notifies the registered session reset handler on 401 and 403', async () => {
    const location = stubWindowLocation('/files')
    const { registerSessionResetHandler } = await import('../api')
    const onSessionReset = vi.fn()
    registerSessionResetHandler(onSessionReset)

    try {
      await expect(responseErrorInterceptor({ response: { status: 401 } })).rejects.toBeDefined()
      expect(onSessionReset).toHaveBeenCalledTimes(1)
      expect(location.href).toBe('/login')

      await expect(responseErrorInterceptor({ response: { status: 403 } })).rejects.toBeDefined()
      expect(onSessionReset).toHaveBeenCalledTimes(2)
      expect(location.href).toBe('/login')
    } finally {
      registerSessionResetHandler(null)
    }
  })

  // ── E-05: timeout and abort handling ────────────────────
  it('shows a timeout message for ECONNABORTED', async () => {
    const error = { code: 'ECONNABORTED', message: 'timeout of 30000ms exceeded' }

    await expect(responseErrorInterceptor(error)).rejects.toBe(error)

    expect(ElMessage.error).toHaveBeenCalledWith('请求超时，请稍后重试')
  })

  it('never reports a deliberate abort as a network failure', async () => {
    const error = { name: 'CanceledError', code: 'ERR_CANCELED' }

    await expect(responseErrorInterceptor(error)).rejects.toBe(error)

    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('recognises fetch aborts and axios cancellations', async () => {
    const { isAbortError } = await import('../api')

    expect(isAbortError({ name: 'AbortError' })).toBe(true)
    expect(isAbortError({ name: 'CanceledError' })).toBe(true)
    expect(isAbortError({ code: 'ERR_CANCELED' })).toBe(true)
    expect(isAbortError({ code: 'ERR_NETWORK' })).toBe(false)
    expect(isAbortError(null)).toBe(false)
  })
})

describe('streamRequest errors', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('surfaces data.error from a non-ok streaming response', async () => {
    const payload = { error: '对话已失效，请重新登录' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      clone: () => ({ json: vi.fn().mockResolvedValue(payload) }),
      json: vi.fn().mockResolvedValue(payload)
    }))

    const { streamRequest } = await import('../api')

    await expect(streamRequest('/query/stream', { method: 'POST' })).rejects.toThrow('对话已失效，请重新登录')
  })

  it('still prefers data.detail over data.error', async () => {
    const payload = { detail: '详细原因', error: '泛化错误' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      clone: () => ({ json: vi.fn().mockResolvedValue(payload) }),
      json: vi.fn().mockResolvedValue(payload)
    }))

    const { streamRequest } = await import('../api')

    await expect(streamRequest('/query/stream', { method: 'POST' })).rejects.toThrow('详细原因')
  })

  it('rethrows an aborted stream without converting it into a network error', async () => {
    const abortError = Object.assign(new Error('aborted'), { name: 'AbortError' })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abortError))

    const { streamRequest } = await import('../api')

    await expect(streamRequest('/query/stream', { method: 'POST' })).rejects.toBe(abortError)
    expect(abortError).not.toHaveProperty('code')
    expect(ElMessage.error).not.toHaveBeenCalled()
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
