import { describe, expect, it } from 'vitest'
import type { UserConfig } from 'vite'

import config from '../../../vite.config'

describe('Vite development proxy', () => {
  it('forwards API requests to the V3 backend', () => {
    const resolved = config as UserConfig
    const proxy = resolved.server?.proxy as Record<string, { target?: string }>

    expect(proxy['/api'].target).toBe('http://localhost:8080')
  })
})
