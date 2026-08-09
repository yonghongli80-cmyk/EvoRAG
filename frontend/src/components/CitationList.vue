<template>
  <div v-if="citations.length" class="citation-list">
    <div
      v-for="citation in citations"
      :key="citation.citation_id"
      class="citation-item"
      :class="{ historical: citation.is_historical }"
    >
      <a
        :href="citation.download_url"
        class="citation-download"
        @click.prevent="download(citation)"
      >
        📎 {{ citation.doc_name }}
        <span v-if="citation.section_title" class="section">· {{ citation.section_title }}</span>
        <el-tag v-if="citation.is_historical" size="small" type="warning">历史</el-tag>
      </a>
      <div class="preview">{{ citation.text_preview }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Citation } from '../api/documents'
import { downloadLibraryFile } from '../api/files'
import { useAuthStore } from '../stores/auth'

defineProps<{ citations: Citation[] }>()

async function download(citation: Citation) {
  const auth = useAuthStore()
  try {
    const blob = await downloadLibraryFile(citation.doc_name, auth.getAuthHeaders())
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = citation.doc_name
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    window.open(citation.download_url, '_blank')
  }
}
</script>
