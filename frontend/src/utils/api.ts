import axios from 'axios'
import { ElMessage } from 'element-plus'

export function resolveApiBase(configured: string | undefined, isDev: boolean): string {
  return configured ?? (isDev ? '/api' : '')
}

const API_BASE = resolveApiBase(import.meta.env.VITE_API_BASE, import.meta.env.DEV)

// Remove bearer tokens left by releases that stored browser sessions in JS.
if (typeof localStorage !== 'undefined') localStorage.removeItem('rag_token')

const api = axios.create({
  baseURL: API_BASE,
  withCredentials: true
})

export type SessionResetHandler = () => void

let sessionResetHandler: SessionResetHandler | null = null

/**
 * Register the routine that drops account-scoped client state when the session
 * becomes invalid. Auth imports this module, so the hook lives here to avoid a
 * circular dependency with the stores.
 */
export function registerSessionResetHandler(handler: SessionResetHandler | null): void {
  sessionResetHandler = handler
}

function notifySessionReset(): void {
  const handler = sessionResetHandler
  if (!handler) return
  try {
    handler()
  } catch {
    // A reset failure must never hide the original HTTP error.
  }
}

/**
 * Accept both FastAPI's `detail` and the legacy/hand-written `error` payloads.
 */
export function extractApiErrorMessage(data: any, fallback: string): string {
  const detail = data?.detail
  if (typeof detail === 'string' && detail) return detail

  const error = data?.error
  if (typeof error === 'string' && error) return error
  if (error && typeof error === 'object') {
    for (const key of ['detail', 'message', 'reason'] as const) {
      const value = error[key]
      if (typeof value === 'string' && value) return value
    }
  }

  return fallback
}

export function shouldRedirectToLogin(pathname: string): boolean {
  return pathname !== '/login'
}

async function rejectRequest(error: any): Promise<never> {
  if (error.response) {
    const { status, data } = error.response
    if (status === 401) {
      notifySessionReset()
      if (shouldRedirectToLogin(window.location.pathname)) {
        window.location.href = '/login'
      }
    } else if (status === 403) {
      notifySessionReset()
      ElMessage.error('权限不足，无法执行此操作')
    } else if (status >= 500) {
      ElMessage.error(extractApiErrorMessage(data, '服务器错误，请稍后重试'))
    }
  } else if (error.message?.includes('Network Error') || error.code === 'ERR_NETWORK') {
    ElMessage.error('网络连接失败，请检查网络后重试')
  } else if (error.code === 'ECONNABORTED') {
    ElMessage.error('请求超时，请稍后重试')
  }

  throw error
}

api.interceptors.response.use(
  (response) => response,
  rejectRequest
)

export async function streamRequest(path: string, init: RequestInit): Promise<Response> {
  const url = `${API_BASE.replace(/\/$/, '')}/${path.replace(/^\//, '')}`
  const headers = new Headers(init.headers)
  const requestInit = { ...init, headers, credentials: 'same-origin' as const }
  let response: Response
  try {
    response = await fetch(url, requestInit)
  } catch (error: any) {
    if (!error.code) error.code = 'ERR_NETWORK'
    return rejectRequest(error)
  }

  if (!response.ok) {
    let data: any = undefined
    try {
      const readable = typeof response.clone === 'function' ? response.clone() : response
      data = await readable.json()
    } catch {
      data = undefined
    }
    const error: any = new Error(extractApiErrorMessage(data, '请求失败'))
    error.response = { status: response.status, data }
    return rejectRequest(error)
  }
  return response
}

/**
 * Retry a request on network/timeout errors.
 * Retries up to `retries` times with exponential backoff.
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  retries = 2,
  delayMs = 1000,
): Promise<T> {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return await fn()
    } catch (err: any) {
      const isNetwork =
        err?.message?.includes('Network Error') ||
        err?.code === 'ERR_NETWORK' ||
        err?.code === 'ECONNABORTED'
      if (attempt < retries && isNetwork) {
        await new Promise((r) => setTimeout(r, delayMs * Math.pow(2, attempt)))
        continue
      }
      throw err
    }
  }
  throw new Error('unreachable')
}

export default api
