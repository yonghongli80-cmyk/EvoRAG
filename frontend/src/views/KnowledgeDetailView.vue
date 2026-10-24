<script setup lang="ts">
import { ref, unref, isRef, onMounted, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../utils/api'
import { downloadLibraryFile } from '../api/files'
import { linkDocumentToKb } from '../api/documents'
import { useChatStore } from '../stores/chat'
import { useAnalysis } from '../composables/useAnalysis'
import ConversationSidebar from '../components/ConversationSidebar.vue'
import AddToAnalysisDialog from '../components/AddToAnalysisDialog.vue'
import type { KBDocument } from './knowledge/helpers'
import KnowledgeInfoPanel from './knowledge/KnowledgeInfoPanel.vue'
import KnowledgeChatArea from './knowledge/KnowledgeChatArea.vue'
import KnowledgeRenameDialog from './knowledge/KnowledgeRenameDialog.vue'
import KnowledgeAddFileDialog from './knowledge/KnowledgeAddFileDialog.vue'

const route = useRoute()
const router = useRouter()
const chatStore = useChatStore()
const { addToAnalysis, dialogVisible, dialogQuestion, dialogAnswer, handleConfirm } = useAnalysis()

const isDialogVisible = computed({
  get: () => {
    const v = unref(dialogVisible)
    return typeof v === 'object' && v !== null && 'value' in v ? (v as any).value : Boolean(v)
  },
  set: (val: boolean) => {
    if (isRef(dialogVisible)) {
      dialogVisible.value = val
    } else if (dialogVisible && typeof dialogVisible === 'object' && 'value' in dialogVisible) {
      ;(dialogVisible as any).value = val
    }
  }
})
const currentDialogQuestion = computed(() => {
  const q = unref(dialogQuestion)
  return typeof q === 'object' && q !== null && 'value' in q ? String((q as any).value) : String(q || '')
})
const currentDialogAnswer = computed(() => {
  const a = unref(dialogAnswer)
  return typeof a === 'object' && a !== null && 'value' in a ? String((a as any).value) : String(a || '')
})

const getKbId = () => route.params.id as string
let knowledgeBaseGeneration = 0
const isCurrentKnowledgeBase = (kbId: string, generation: number) =>
  getKbId() === kbId && knowledgeBaseGeneration === generation
const loading = ref(true)
const convSidebarCollapsed = ref(false)
const kbInfoCollapsed = ref(false)

// KB detail data
const kbName = ref('')
const kbDescription = ref('')
const kbOverview = ref('')
const documents = ref<KBDocument[]>([])
const effectiveRole = ref<'read' | 'manage' | 'administer'>('read')
const canManage = computed(() => effectiveRole.value === 'manage' || effectiveRole.value === 'administer')
const canAdminister = computed(() => effectiveRole.value === 'administer')

// Overview editing
const savingOverview = ref(false)

// Rename KB
const showRenameDialog = ref(false)
const renaming = ref(false)

// Add files dialog
const showAddDialog = ref(false)
const availableFiles = ref<any[]>([])
const loadingFiles = ref(false)
const addingFile = ref(false)

// TOC / Overview generation
const generatingToc = ref(false)
const generatingOverview = ref(false)

/**
 * Rebuild every conversation-scoped piece of state for the KB in the URL.
 *
 * The route component is reused when only `:id` changes, so without the reset
 * the previous knowledge base's conversations, streamed answer and selected
 * file stayed on screen and leaked into the next KB (E-08).
 */
function clearKnowledgeBaseDetail() {
  kbName.value = ''
  kbDescription.value = ''
  kbOverview.value = ''
  documents.value = []
  effectiveRole.value = 'read'
  availableFiles.value = []
}

async function enterKnowledgeBase(kbId = getKbId()) {
  const generation = ++knowledgeBaseGeneration
  clearKnowledgeBaseDetail()
  showRenameDialog.value = false
  showAddDialog.value = false
  renaming.value = false
  savingOverview.value = false
  loadingFiles.value = false
  addingFile.value = false
  generatingToc.value = false
  generatingOverview.value = false
  loading.value = true
  chatStore.reset()
  await chatStore.loadConversations('kb')
  if (!isCurrentKnowledgeBase(kbId, generation)) return
  // Auto-select the most recent conversation if exists
  if (chatStore.conversations.length > 0) {
    await chatStore.selectConversation(chatStore.conversations[0].id)
    if (!isCurrentKnowledgeBase(kbId, generation)) return
  }
  await loadDetail(kbId, generation)
}

watch(() => route.params.id, (nextId, previousId) => {
  if (nextId === previousId) return
  enterKnowledgeBase()
})

onMounted(enterKnowledgeBase)

async function loadDetail(kbId = getKbId(), generation = knowledgeBaseGeneration) {
  loading.value = true
  try {
    const res = await api.get(`/knowledge-bases/${kbId}`)
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    const data = res.data
    kbName.value = data.name || ''
    kbDescription.value = data.description || ''
    kbOverview.value = data.overview || ''
    documents.value = data.documents || []
    effectiveRole.value = data.effective_role || 'read'
  } catch (err: any) {
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    if (err.response?.status === 404) {
      ElMessage.error('知识库不存在')
      router.push('/kb')
      return
    }
    ElMessage.error('加载知识库详情失败')
  } finally {
    if (isCurrentKnowledgeBase(kbId, generation)) loading.value = false
  }
}

function goBack() {
  router.push('/kb')
}

// ── Settings actions ──
function openRenameDialog() {
  showRenameDialog.value = true
}

async function renameKB(name: string) {
  const kbId = getKbId()
  const generation = knowledgeBaseGeneration
  renaming.value = true
  try {
    await api.put(`/knowledge-bases/${kbId}/name`, { name })
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    kbName.value = name
    showRenameDialog.value = false
    ElMessage.success('已重命名')
  } catch {
    if (isCurrentKnowledgeBase(kbId, generation)) ElMessage.error('重命名失败')
  } finally {
    if (isCurrentKnowledgeBase(kbId, generation)) renaming.value = false
  }
}

async function generateTOC() {
  const kbId = getKbId()
  const generation = knowledgeBaseGeneration
  generatingToc.value = true
  try {
    const res = await api.post(`/knowledge-bases/${kbId}/toc/generate`, {})
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    // Update local document state with generated TOC
    const tocData = res.data.toc || {}
    for (const doc of documents.value) {
      if (tocData[doc.filename]) {
        doc.toc = JSON.stringify(tocData[doc.filename])
      }
    }
    ElMessage.success('目录生成完成')
  } catch (err: any) {
    if (isCurrentKnowledgeBase(kbId, generation)) ElMessage.error(err.response?.data?.detail || '目录生成失败')
  } finally {
    if (isCurrentKnowledgeBase(kbId, generation)) generatingToc.value = false
  }
}

async function generateOverview() {
  const kbId = getKbId()
  const generation = knowledgeBaseGeneration
  generatingOverview.value = true
  try {
    const res = await api.post(`/knowledge-bases/${kbId}/overview/generate`, {})
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    kbOverview.value = res.data.overview || ''
    ElMessage.success('概述生成完成')
  } catch (err: any) {
    if (isCurrentKnowledgeBase(kbId, generation)) ElMessage.error(err.response?.data?.detail || '概述生成失败')
  } finally {
    if (isCurrentKnowledgeBase(kbId, generation)) generatingOverview.value = false
  }
}

async function deleteKB() {
  const kbId = getKbId()
  const generation = knowledgeBaseGeneration
  const name = kbName.value
  try {
    await ElMessageBox.confirm(
      `确定删除知识库「${name}」吗？此操作不可恢复。`,
      '删除知识库',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'error' }
    )
    await api.delete(`/knowledge-bases/${kbId}`)
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    ElMessage.success('知识库已删除')
    router.push('/kb')
  } catch (err: any) {
    if (isCurrentKnowledgeBase(kbId, generation) && err !== 'cancel' && err?.message !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

// ── Overview editing ──
async function saveOverview(newOverview: string) {
  const kbId = getKbId()
  const generation = knowledgeBaseGeneration
  savingOverview.value = true
  try {
    await api.put(`/knowledge-bases/${kbId}/overview`, { overview: newOverview })
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    kbOverview.value = newOverview
    ElMessage.success('概述已更新')
  } catch {
    if (isCurrentKnowledgeBase(kbId, generation)) ElMessage.error('保存概述失败')
  } finally {
    if (isCurrentKnowledgeBase(kbId, generation)) savingOverview.value = false
  }
}

// ── Document management ──
async function removeFromKB(doc: KBDocument) {
  const kbId = getKbId()
  const generation = knowledgeBaseGeneration
  try {
    await ElMessageBox.confirm(
      `确定将「${doc.filename}」从知识库移除？`,
      '从知识库移除',
      { confirmButtonText: '移除', cancelButtonText: '取消', type: 'warning' }
    )
    await api.delete(`/knowledge-bases/${kbId}/documents/${encodeURIComponent(doc.filename)}`)
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    ElMessage.success('已从知识库移除')
    await loadDetail(kbId, generation)
  } catch (err: any) {
    if (isCurrentKnowledgeBase(kbId, generation) && err !== 'cancel' && err?.message !== 'cancel') {
      ElMessage.error('移除失败')
    }
  }
}

// ── Add files to KB ──
function openAddDialog() {
  showAddDialog.value = true
}

async function loadAvailableFiles() {
  const kbId = getKbId()
  const generation = knowledgeBaseGeneration
  const kbFileNames = new Set(documents.value.map(d => d.filename))
  loadingFiles.value = true
  try {
    const res = await api.get('/files')
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    availableFiles.value = (res.data.files || []).filter((f: any) => !kbFileNames.has(f.name))
  } catch {
    if (isCurrentKnowledgeBase(kbId, generation)) ElMessage.error('加载文件列表失败')
  } finally {
    if (isCurrentKnowledgeBase(kbId, generation)) loadingFiles.value = false
  }
}

async function addFileToKB(file: any) {
  if (!file) return
  const kbId = getKbId()
  const generation = knowledgeBaseGeneration
  addingFile.value = true
  try {
    if (file.document_id) {
      // Server-side reference: no browser download/re-upload, no HTML-as-file.
      await linkDocumentToKb(Number(file.document_id), kbId)
    } else {
      // Upload-directory-only entries have no permission row yet; fall back to bytes.
      const blob = await downloadLibraryFile(file.name, file.version_id)
      const formData = new FormData()
      formData.append('file', blob, file.name)
      await api.post(`/knowledge-bases/${kbId}/documents`, formData)
    }
    if (!isCurrentKnowledgeBase(kbId, generation)) return
    ElMessage.success('文件已添加到知识库')
    showAddDialog.value = false
    await loadDetail(kbId, generation)
  } catch {
    if (isCurrentKnowledgeBase(kbId, generation)) ElMessage.error('添加文件失败')
  } finally {
    if (isCurrentKnowledgeBase(kbId, generation)) addingFile.value = false
  }
}

function openSpreadsheet(doc: { document_id: number; document_version_id: number }) {
  router.push({
    name: 'spreadsheet-viewer',
    params: { documentId: doc.document_id, versionId: doc.document_version_id },
  })
}
</script>

<template>
  <div class="kb-detail-layout">
    <!-- Reusable Conversation Sidebar (left) -->
    <ConversationSidebar mode="kb" v-model:collapsed="convSidebarCollapsed" />

    <!-- KB info panel (middle) -->
    <KnowledgeInfoPanel
      v-model:collapsed="kbInfoCollapsed"
      :kb-id="getKbId()"
      :kb-name="kbName"
      :kb-overview="kbOverview"
      :documents="documents"
      :loading="loading"
      :can-manage="canManage"
      :can-administer="canAdminister"
      :generating-toc="generatingToc"
      :generating-overview="generatingOverview"
      :saving-overview="savingOverview"
      @go-back="goBack"
      @open-rename="openRenameDialog"
      @generate-toc="generateTOC"
      @generate-overview="generateOverview"
      @open-add-dialog="openAddDialog"
      @delete-kb="deleteKB"
      @save-overview="saveOverview"
      @remove-doc="removeFromKB"
      @open-spreadsheet="openSpreadsheet"
    />

    <!-- Chat area (right) -->
    <KnowledgeChatArea
      :kb-id="getKbId()"
      @add-to-analysis="addToAnalysis"
    />

    <!-- Dialogs -->
    <KnowledgeRenameDialog
      v-model:visible="showRenameDialog"
      :name="kbName"
      :renaming="renaming"
      @rename="renameKB"
    />

    <KnowledgeAddFileDialog
      v-model:visible="showAddDialog"
      :available-files="availableFiles"
      :loading-files="loadingFiles"
      :adding-file="addingFile"
      @open="loadAvailableFiles"
      @add="addFileToKB"
    />

    <AddToAnalysisDialog
      v-model:visible="isDialogVisible"
      :question="currentDialogQuestion"
      :answer="currentDialogAnswer"
      @confirm="handleConfirm"
    />
  </div>
</template>

<style scoped>
.kb-detail-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}
</style>
