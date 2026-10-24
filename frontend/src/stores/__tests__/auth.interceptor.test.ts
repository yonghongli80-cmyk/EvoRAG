import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AxiosAdapter, InternalAxiosRequestConfig } from 'axios'

vi.mock('element-plus', () => ({
  ElMessage: { error: vi.fn() },
}))

let api: typeof import('../../utils/api').default
let useAuthStore: typeof import('../auth').useAuthStore
let AxiosError: typeof import('axios').AxiosError

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function response(config: InternalAxiosRequestConfig, data: unknown = {}) {
  return { data, status: 200, statusText: 'OK', headers: {}, config }
}

function unauthorized(config: InternalAxiosRequestConfig) {
  const result = {
    status: 401,
    statusText: 'Unauthorized',
    data: {},
    headers: {},
    config,
  }
  return new AxiosError('Unauthorized', AxiosError.ERR_BAD_REQUEST, config, undefined, result)
}

describe('auth with the actual API response interceptor', () => {
  let previousAdapter: AxiosAdapter

  beforeEach(async () => {
    vi.resetModules()
    vi.doUnmock('../../utils/api')
    const realAxios = await vi.importActual<typeof import('axios')>('axios')
    vi.doMock('axios', () => realAxios)
    const pinia = await import('pinia')
    pinia.setActivePinia(pinia.createPinia())
    window.history.replaceState({}, '', '/login')
    AxiosError = (await import('axios')).AxiosError
    api = (await import('../../utils/api')).default
    useAuthStore = (await import('../auth')).useAuthStore
    previousAdapter = api.defaults.adapter as AxiosAdapter
  })

  afterEach(() => {
    api.defaults.adapter = previousAdapter
  })

  it('clears the session when the actual /me interceptor handles a 401', async () => {
    api.defaults.adapter = async (config) => { throw unauthorized(config) }
    const store = useAuthStore()
    store.user = { id: 1, username: 'alice' }

    await store.fetchUser(true)

    expect(store.user).toBeNull()
    expect(store.isAuthenticated).toBe(false)
    expect(store.sessionChecked).toBe(true)
  })

  it('does not let an old /me 401 clear the account that logged in later', async () => {
    const oldResponse = deferred<never>()
    let meCount = 0
    let oldRequestConfig: InternalAxiosRequestConfig | undefined
    api.defaults.adapter = async (config) => {
      if (config.url === '/me' && meCount++ === 0) {
        oldRequestConfig = config
        return oldResponse.promise
      }
      if (config.url === '/login') return response(config, { username: 'bob' })
      if (config.url === '/me') return response(config, { id: 2, username: 'bob' })
      throw new Error(`unexpected request: ${config.url}`)
    }
    const store = useAuthStore()

    const oldProbe = store.fetchUser()
    await store.login('bob', 'password')
    oldResponse.reject(unauthorized(oldRequestConfig!))
    await oldProbe

    expect(store.user).toEqual({ id: 2, username: 'bob' })
    expect(store.isAuthenticated).toBe(true)
  })

  it('ignores an old /me 401 without resetting the new session or invalidating its probe', async () => {
    const oldResponse = deferred<never>()
    const newResponse = deferred<ReturnType<typeof response>>()
    let meCount = 0
    let oldRequestConfig: InternalAxiosRequestConfig | undefined
    let newRequestConfig: InternalAxiosRequestConfig | undefined
    let announceNewProbe!: () => void
    const newProbeStarted = new Promise<void>((resolve) => { announceNewProbe = resolve })
    api.defaults.adapter = async (config) => {
      if (config.url === '/me' && meCount++ === 0) {
        oldRequestConfig = config
        return oldResponse.promise
      }
      if (config.url === '/login') return response(config, { username: 'bob' })
      if (config.url === '/me') {
        newRequestConfig = config
        announceNewProbe()
        return newResponse.promise
      }
      throw new Error(`unexpected request: ${config.url}`)
    }
    window.history.replaceState({}, '', '/files')
    const store = useAuthStore()
    const oldProbe = store.fetchUser()
    const login = store.login('bob', 'password')
    await newProbeStarted

    const pinia = await import('pinia')
    const chat = (await import('../chat')).useChatStore(pinia.getActivePinia()!)
    const files = (await import('../files')).useFilesStore(pinia.getActivePinia()!)
    chat.messages = [{ role: 'assistant', content: 'Bob private message' }]
    files.files = [{ name: 'bob-private.pdf', size: 1, size_human: '1 B', ext: '.pdf' }]
    const navigationError = vi.spyOn(console, 'error').mockImplementation(() => {})

    oldResponse.reject(unauthorized(oldRequestConfig!))
    await oldProbe

    expect(chat.messages).toEqual([{ role: 'assistant', content: 'Bob private message' }])
    expect(files.files).toEqual([{ name: 'bob-private.pdf', size: 1, size_human: '1 B', ext: '.pdf' }])
    expect(navigationError).not.toHaveBeenCalled()
    expect(window.location.pathname).toBe('/files')

    newResponse.resolve(response(newRequestConfig!, { id: 2, username: 'bob' }))
    await login
    expect(store.user).toEqual({ id: 2, username: 'bob' })
    navigationError.mockRestore()
  })

  it('ignores a delayed ordinary GET 401 from the previous session', async () => {
    const oldResponse = deferred<never>()
    let oldRequestConfig: InternalAxiosRequestConfig | undefined
    api.defaults.adapter = async (config) => {
      if (config.url === '/conversations') {
        oldRequestConfig = config
        return oldResponse.promise
      }
      if (config.url === '/login') return response(config, { username: 'bob' })
      if (config.url === '/me') return response(config, { id: 2, username: 'bob' })
      throw new Error(`unexpected request: ${config.url}`)
    }
    window.history.replaceState({}, '', '/files')
    const store = useAuthStore()
    const oldRequest = api.get('/conversations').catch((error) => error)
    await store.login('bob', 'password')
    const pinia = await import('pinia')
    const chat = (await import('../chat')).useChatStore(pinia.getActivePinia()!)
    const files = (await import('../files')).useFilesStore(pinia.getActivePinia()!)
    chat.messages = [{ role: 'assistant', content: 'Bob private message' }]
    files.files = [{ name: 'bob-private.pdf', size: 1, size_human: '1 B', ext: '.pdf' }]
    const navigationError = vi.spyOn(console, 'error').mockImplementation(() => {})

    oldResponse.reject(unauthorized(oldRequestConfig!))
    const error = await oldRequest

    expect(error.response.status).toBe(401)
    expect(chat.messages).toEqual([{ role: 'assistant', content: 'Bob private message' }])
    expect(files.files).toEqual([{ name: 'bob-private.pdf', size: 1, size_human: '1 B', ext: '.pdf' }])
    expect(navigationError).not.toHaveBeenCalled()
    expect(window.location.pathname).toBe('/files')
    navigationError.mockRestore()
  })

  it('ignores a delayed streaming 401 from the previous session', async () => {
    const oldResponse = deferred<any>()
    const fetchMock = vi.fn(() => oldResponse.promise)
    vi.stubGlobal('fetch', fetchMock)
    api.defaults.adapter = async (config) => {
      if (config.url === '/login') return response(config, { username: 'bob' })
      if (config.url === '/me') return response(config, { id: 2, username: 'bob' })
      throw new Error(`unexpected request: ${config.url}`)
    }
    window.history.replaceState({}, '', '/files')
    const store = useAuthStore()
    const { streamRequest } = await import('../../utils/api')
    const oldRequest = streamRequest('/query/stream', { method: 'POST' }).catch((error) => error)
    await store.login('bob', 'password')
    const pinia = await import('pinia')
    const chat = (await import('../chat')).useChatStore(pinia.getActivePinia()!)
    const files = (await import('../files')).useFilesStore(pinia.getActivePinia()!)
    chat.messages = [{ role: 'assistant', content: 'Bob private message' }]
    files.files = [{ name: 'bob-private.pdf', size: 1, size_human: '1 B', ext: '.pdf' }]
    const navigationError = vi.spyOn(console, 'error').mockImplementation(() => {})

    oldResponse.resolve({
      ok: false,
      status: 401,
      clone: () => ({ json: async () => ({ detail: 'expired' }) }),
      json: async () => ({ detail: 'expired' }),
    })
    const error = await oldRequest

    expect(error.response.status).toBe(401)
    expect(chat.messages).toEqual([{ role: 'assistant', content: 'Bob private message' }])
    expect(files.files).toEqual([{ name: 'bob-private.pdf', size: 1, size_human: '1 B', ext: '.pdf' }])
    expect(navigationError).not.toHaveBeenCalled()
    expect(window.location.pathname).toBe('/files')
    navigationError.mockRestore()
    vi.unstubAllGlobals()
  })

  it('does not let a delayed old-session files response overwrite the new account', async () => {
    const oldResponse = deferred<ReturnType<typeof response>>()
    let oldRequestConfig: InternalAxiosRequestConfig | undefined
    api.defaults.adapter = async (config) => {
      if (config.url === '/files') {
        oldRequestConfig = config
        return oldResponse.promise
      }
      if (config.url === '/login') return response(config, { username: 'bob' })
      if (config.url === '/me') return response(config, { id: 2, username: 'bob' })
      throw new Error(`unexpected request: ${config.url}`)
    }
    const store = useAuthStore()
    const pinia = await import('pinia')
    const files = (await import('../files')).useFilesStore(pinia.getActivePinia()!)
    const loadOldFiles = files.loadFiles()
    await store.login('bob', 'password')
    files.files = [{ name: 'bob-private.pdf', size: 2, size_human: '2 B', ext: '.pdf' }]
    oldResponse.resolve(response(oldRequestConfig!, {
      files: [{ name: 'alice-private.pdf', size: 1, size_human: '1 B', ext: '.pdf' }],
    }))
    await loadOldFiles

    expect(files.files).toEqual([{ name: 'bob-private.pdf', size: 2, size_human: '2 B', ext: '.pdf' }])
  })

  it('does not clear auth when the actual API interceptor handles a permission 403', async () => {
    api.defaults.adapter = async (config) => {
      const result = { status: 403, statusText: 'Forbidden', data: {}, headers: {}, config }
      throw new AxiosError('Forbidden', AxiosError.ERR_BAD_REQUEST, config, undefined, result)
    }
    const store = useAuthStore()
    store.user = { id: 3, username: 'authorized' }

    await expect(api.get('/protected')).rejects.toBeDefined()

    expect(store.user).toEqual({ id: 3, username: 'authorized' })
    expect(store.isAuthenticated).toBe(true)
  })
})
