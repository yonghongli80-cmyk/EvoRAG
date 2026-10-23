import { describe, expect, it } from 'vitest'
import type { UserConfig } from 'vite'
import { createServer } from 'vite'
import http from 'node:http'
import type { AddressInfo } from 'node:net'

import config from '../../../vite.config'

describe('Vite development proxy', () => {
  it('forwards API requests to the V3 backend', () => {
    const resolved = config as UserConfig
    const proxy = resolved.server?.proxy as Record<string, { target?: string }>

    expect(proxy['/api'].target).toBe('http://localhost:8080')
  })

  it('preserves the browser Host for same-origin writes and rejects cross-site writes', async () => {
    const received: Array<{ host?: string; origin?: string }> = []
    const backend = http.createServer((request, response) => {
      received.push({ host: request.headers.host, origin: request.headers.origin })
      const expectedOrigin = `http://${request.headers.host}`
      const rejected = Boolean(request.headers.cookie)
        && request.method !== 'GET'
        && request.headers.origin !== expectedOrigin
      response.writeHead(rejected ? 403 : 200)
      response.end()
    })
    await new Promise<void>(resolve => backend.listen(0, '127.0.0.1', resolve))
    const backendPort = (backend.address() as AddressInfo).port
    const proxy = (config.server?.proxy as Record<string, Record<string, unknown>>)['/api']
    const vite = await createServer({
      ...config,
      configFile: false,
      logLevel: 'silent',
      server: {
        ...config.server,
        host: '127.0.0.1',
        port: 0,
        proxy: {
          '/api': { ...proxy, target: `http://127.0.0.1:${backendPort}` }
        }
      }
    } as any)

    try {
      await vite.listen()
      const vitePort = (vite.httpServer!.address() as AddressInfo).port
      const endpoint = `http://127.0.0.1:${vitePort}/api/write`
      const sameOrigin = await fetch(endpoint, {
        method: 'POST',
        headers: { origin: `http://127.0.0.1:${vitePort}`, cookie: 'session=present' }
      })
      expect(sameOrigin.status).toBe(200)
      expect(received.at(-1)).toEqual({
        host: `127.0.0.1:${vitePort}`,
        origin: `http://127.0.0.1:${vitePort}`
      })

      const crossSite = await fetch(endpoint, {
        method: 'POST',
        headers: { origin: 'https://attacker.example', cookie: 'session=present' }
      })
      expect(crossSite.status).toBe(403)
    } finally {
      await vite.close()
      await new Promise<void>((resolve, reject) => backend.close(error => error ? reject(error) : resolve()))
    }
  })
})
