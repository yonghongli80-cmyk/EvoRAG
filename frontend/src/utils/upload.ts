/**
 * Upload validation shared by every entry point that accepts user files.
 *
 * Before this module the file panel only checked the extension while dropping
 * files, the upload button relied on the `accept` attribute, and the KB
 * association flow did no check at all — so the same bad file produced three
 * different behaviours (E-09).
 */

/** Extensions the backend document pipeline understands. */
export const ALLOWED_UPLOAD_EXTENSIONS = [
  '.txt',
  '.md',
  '.pdf',
  '.docx',
  '.xlsx',
  '.csv'
] as const

/** Mirrors the backend's `MAX_FILE_SIZE` for document uploads. */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024

export type UploadRejectionCode = 'unsupported_type' | 'too_large'

export interface UploadRejection {
  code: UploadRejectionCode
  message: string
}

/** Lower-case extension of the basename, or '' when there is none. */
export function uploadExtension(name: string): string {
  const base = (name || '').trim().split(/[\\/]/).pop() || ''
  const dot = base.lastIndexOf('.')
  if (dot <= 0 || dot === base.length - 1) return ''
  return base.slice(dot).toLowerCase()
}

export function isSupportedUploadName(name: string): boolean {
  return (ALLOWED_UPLOAD_EXTENSIONS as readonly string[]).includes(uploadExtension(name))
}

/**
 * Return a rejection describing why the file may not be uploaded, or null when
 * it is acceptable.
 */
export function validateUploadFile(file: { name?: string; size?: number }): UploadRejection | null {
  const name = file?.name || ''
  if (!isSupportedUploadName(name)) {
    return { code: 'unsupported_type', message: `不支持的文件格式: ${name || '(未命名)'}` }
  }
  if (typeof file.size === 'number' && file.size > MAX_UPLOAD_BYTES) {
    return { code: 'too_large', message: '文件超过 10 MB 限制' }
  }
  return null
}
