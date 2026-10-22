import { describe, expect, it } from 'vitest'
import {
  ALLOWED_UPLOAD_EXTENSIONS,
  MAX_UPLOAD_BYTES,
  isSupportedUploadName,
  uploadExtension,
  validateUploadFile
} from '../upload'

describe('upload validation', () => {
  it('accepts every backend-supported extension regardless of case', () => {
    for (const ext of ALLOWED_UPLOAD_EXTENSIONS) {
      expect(isSupportedUploadName(`report${ext.toUpperCase()}`)).toBe(true)
      expect(isSupportedUploadName(`report${ext}`)).toBe(true)
    }
  })

  it('reads the extension from the basename', () => {
    expect(uploadExtension('C:\\Users\\a\\folder.with.dot\\notes.MD')).toBe('.md')
    expect(uploadExtension('archive.tar.gz')).toBe('.gz')
    expect(uploadExtension('README')).toBe('')
    expect(uploadExtension('')).toBe('')
  })

  it('rejects unsupported and extensionless names', () => {
    expect(validateUploadFile({ name: 'malware.exe', size: 10 })).toEqual({
      code: 'unsupported_type',
      message: '不支持的文件格式: malware.exe'
    })
    expect(validateUploadFile({ name: 'README', size: 10 })?.code).toBe('unsupported_type')
    expect(validateUploadFile({ name: '', size: 10 })?.code).toBe('unsupported_type')
  })

  it('rejects files above the backend 10 MB limit', () => {
    expect(validateUploadFile({ name: 'big.pdf', size: MAX_UPLOAD_BYTES + 1 })).toEqual({
      code: 'too_large',
      message: '文件超过 10 MB 限制'
    })
    expect(validateUploadFile({ name: 'ok.pdf', size: MAX_UPLOAD_BYTES })).toBeNull()
  })

  it('accepts a supported file within the limit', () => {
    expect(validateUploadFile({ name: 'policy.pdf', size: 1024 })).toBeNull()
  })
})
