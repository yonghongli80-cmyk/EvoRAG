import axios from 'axios'
import { ElMessage } from 'element-plus'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE ?? '/api'
})

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response) {
      const { status, data } = error.response
      if (status === 401) {
        // Token expired or invalid -- clear and redirect to login
        localStorage.removeItem('rag_token')
        window.location.href = '/login'
        return Promise.reject(error)
      }
      if (status === 403) {
        ElMessage.error('权限不足，无法执行此操作')
      } else if (status >= 500) {
        const detail = data?.detail || '服务器错误，请稍后重试'
        ElMessage.error(detail)
      }
    } else if (error.message?.includes('Network Error') || error.code === 'ERR_NETWORK') {
      ElMessage.error('网络连接失败，请检查网络后重试')
    } else if (error.code === 'ECONNABORTED') {
      ElMessage.error('请求超时，请稍后重试')
    }

    return Promise.reject(error)
  }
)

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
