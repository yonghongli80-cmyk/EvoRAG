export async function downloadLibraryFile(
  filename: string,
  authHeaders: Record<string, string>,
  versionId?: number
): Promise<Blob> {
  const versionQuery = versionId ? `?version_id=${encodeURIComponent(String(versionId))}` : ''
  const response = await fetch(`/files/${encodeURIComponent(filename)}/download${versionQuery}`, {
    headers: authHeaders
  })
  if (!response.ok) {
    throw new Error('无法读取文件')
  }
  return response.blob()
}
