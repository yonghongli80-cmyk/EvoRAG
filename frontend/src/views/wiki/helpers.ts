import DOMPurify from 'dompurify'

export interface KBItem {
  kb_id: string
  name: string
  doc_count: number
  effective_role: string
}

export function formatTime(timestamp: number | string): string {
  if (!timestamp) return ''
  const date = typeof timestamp === 'number' ? new Date(timestamp * 1000) : new Date(timestamp)
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

// Convert wikilink syntax [[Target]] into clickable tags safely
export function renderContentWithWikilinks(content: string): string {
  if (!content) return ''
  // Replace [[Target]] or [[Target|Display]]
  const escaped = content
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')

  const withLinks = escaped.replace(/\[\[(.*?)\]\]/g, (_match, p1) => {
    const parts = p1.split('|')
    const target = parts[0].trim()
    const label = parts[1] ? parts[1].trim() : target
    return `<span class="wikilink-badge" data-target="${target}">🔗 ${label}</span>`
  })

  return DOMPurify.sanitize(withLinks, {
    ALLOWED_TAGS: ['span'],
    ALLOWED_ATTR: ['class', 'data-target']
  })
}
