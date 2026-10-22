import api from '../utils/api'

/** Backend route that streams one stored version back to the browser. */
export function fileDownloadPath(filename: string): string {
  return `/files/${encodeURIComponent(filename)}/download`
}

/**
 * Download a library file through the shared axios client so the request uses
 * the configured API base, the HttpOnly session cookie and the 401/403
 * handling — the previous raw `fetch('/files/...')` ignored all three (E-09).
 *
 * @throws Error when the response is not a file (the dev proxy answers unknown
 *   routes with index.html, which must never be uploaded as a document).
 */
export async function downloadLibraryFile(filename: string, versionId?: number): Promise<Blob> {
  const response = await api.get(fileDownloadPath(filename), {
    responseType: 'blob',
    params: versionId ? { version_id: versionId } : undefined
  })
  const blob: Blob | undefined = response?.data
  if (!blob) throw new Error('无法读取文件')
  if ((blob.type || '').toLowerCase().includes('text/html')) {
    throw new Error('无法读取文件')
  }
  return blob
}
