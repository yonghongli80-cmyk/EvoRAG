export async function downloadLibraryFile(
  filename: string,
  authHeaders: Record<string, string>
): Promise<Blob> {
  const response = await fetch(`/files/${encodeURIComponent(filename)}/download`, {
    headers: authHeaders
  })
  if (!response.ok) {
    throw new Error('无法读取文件')
  }
  return response.blob()
}
