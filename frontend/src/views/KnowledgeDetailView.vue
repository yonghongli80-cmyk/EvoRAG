<script setup lang="ts">
import { ref, onMounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../utils/api'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import { useAnalysis } from '../composables/useAnalysis'
import MessageBubble from '../components/MessageBubble.vue'
import ChatInput from '../components/ChatInput.vue'
import AddToAnalysisDialog from '../components/AddToAnalysisDialog.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft,
  Edit,
  Check,
  Delete,
  Plus,
  ChatDotRound,
  Setting,
  Document,
  Upload,
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const chatStore = useChatStore()
const { addToAnalysis, dialogVisible, dialogQuestion, dialogAnswer, handleConfirm } = useAnalysis()

const kbId = route.params.id as string
const loading = ref(true)
const messagesContainer = ref<HTMLElement>()

// KB detail data
const kbName = ref('')
const kbDescription = ref('')
const kbOverview = ref('')
const documents = ref<any[]>([])

// Overview editing
const editingOverview = ref(false)
const overviewDraft = ref('')
const savingOverview = ref(false)

// Settings menu
const showSettingsMenu = ref(false)

// Rename KB
const showRenameDialog = ref(false)
const renameDraft = ref('')
const renaming = ref(false)

// Add files dialog
const showAddDialog = ref(false)
const availableFiles = ref<any[]>([])
const loadingFiles = ref(false)
const selectedFile = ref<any>(null)
const addingFile = ref(false)

// Remove / delete states
const removingDoc = ref('')

// TOC / Overview generation
const generatingToc = ref(false)
const generatingOverview = ref(false)

// ── Scroll handling ──
watch(() => chatStore.messages.length, () => {
  nextTick(() => { messagesContainer.value?.scrollTo({ top: messagesContainer.value.scrollHeight, behavior: 'smooth' }) })
})

watch(() => chatStore.messages[chatStore.messages.length - 1]?.content, () => {
  nextTick(() => { messagesContainer.value?.scrollTo({ top: messagesContainer.value.scrollHeight, behavior: 'smooth' }) })
})

onMounted(async () => {
  await chatStore.loadConversations('kb')
  // Auto-select the most recent conversation if exists
  if (chatStore.conversations.length > 0 && !chatStore.currentConvId) {
    await chatStore.selectConversation(chatStore.conversations[0].id)
  }
  await loadDetail()
})

async function loadDetail() {
  loading.value = true
  try {
    const res = await api.get(`/knowledge-bases/${kbId}`, {
      headers: authStore.getAuthHeaders()
    })
    const data = res.data
    kbName.value = data.name || ''
    kbDescription.value = data.description || ''
    kbOverview.value = data.overview || ''
    documents.value = data.documents || []
  } catch (err: any) {
    if (err.response?.status === 404) {
      ElMessage.error('知识库不存在')
      router.push('/kb')
      return
    }
    ElMessage.error('加载知识库详情失败')
  } finally {
    loading.value = false
  }
}

function goBack() {
  router.push('/kb')
}

// ── Conversation management ──
async function newConversation() {
  await chatStore.createConversation('kb')
}

async function selectConversation(id: number) {
  await chatStore.selectConversation(id)
}

async function deleteConversation(id: number) {
  try {
    await ElMessageBox.confirm('确定删除这个对话？删除后不可恢复。', '确认删除', { type: 'warning' })
    await chatStore.deleteConversation(id)
  } catch {
    // User cancelled
  }
}

function formatTime(ts: string | number) {
  if (!ts) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  const now = new Date()
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

function askSuggested(q: string) {
  chatStore.sendMessage(q)
}

// ── Settings menu actions ──
function openRenameDialog() {
  renameDraft.value = kbName.value
  showRenameDialog.value = true
  showSettingsMenu.value = false
}

async function renameKB() {
  const name = renameDraft.value.trim()
  if (!name) return ElMessage.warning('请输入名称')
  renaming.value = true
  try {
    await api.put(`/knowledge-bases/${kbId}/name`, { name }, {
      headers: authStore.getAuthHeaders()
    })
    kbName.value = name
    showRenameDialog.value = false
    ElMessage.success('已重命名')
  } catch {
    ElMessage.error('重命名失败')
  } finally {
    renaming.value = false
  }
}

async function generateTOC() {
  showSettingsMenu.value = false
  generatingToc.value = true
  try {
    const res = await api.post(`/knowledge-bases/${kbId}/toc/generate`, {}, {
      headers: authStore.getAuthHeaders()
    })
    // Update local document state with generated TOC
    const tocData = res.data.toc || {}
    for (const doc of documents.value) {
      if (tocData[doc.filename]) {
        doc.toc = JSON.stringify(tocData[doc.filename])
      }
    }
    ElMessage.success('目录生成完成')
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '目录生成失败')
  } finally {
    generatingToc.value = false
  }
}

async function generateOverview() {
  showSettingsMenu.value = false
  generatingOverview.value = true
  try {
    const res = await api.post(`/knowledge-bases/${kbId}/overview/generate`, {}, {
      headers: authStore.getAuthHeaders()
    })
    kbOverview.value = res.data.overview || ''
    ElMessage.success('概述生成完成')
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '概述生成失败')
  } finally {
    generatingOverview.value = false
  }
}

async function deleteKB() {
  showSettingsMenu.value = false
  try {
    await ElMessageBox.confirm(
      `确定删除知识库「${kbName.value}」吗？此操作不可恢复。`,
      '删除知识库',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'error' }
    )
    await api.delete(`/knowledge-bases/${kbId}`, {
      headers: authStore.getAuthHeaders()
    })
    ElMessage.success('知识库已删除')
    router.push('/kb')
  } catch (err: any) {
    if (err !== 'cancel' && err?.message !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

// ── Overview editing ──
function startEditOverview() {
  overviewDraft.value = kbOverview.value
  editingOverview.value = true
}

function cancelEditOverview() {
  editingOverview.value = false
  overviewDraft.value = ''
}

async function saveOverview() {
  savingOverview.value = true
  try {
    await api.put(`/knowledge-bases/${kbId}/overview`, { overview: overviewDraft.value }, {
      headers: authStore.getAuthHeaders()
    })
    kbOverview.value = overviewDraft.value
    editingOverview.value = false
    ElMessage.success('概述已更新')
  } catch {
    ElMessage.error('保存概述失败')
  } finally {
    savingOverview.value = false
  }
}

// ── Document management ──
async function removeFromKB(doc: any) {
  try {
    await ElMessageBox.confirm(
      `确定将「${doc.filename}」从知识库移除？`,
      '从知识库移除',
      { confirmButtonText: '移除', cancelButtonText: '取消', type: 'warning' }
    )
    removingDoc.value = doc.filename
    await api.delete(`/knowledge-bases/${kbId}/documents/${encodeURIComponent(doc.filename)}`, {
      headers: authStore.getAuthHeaders()
    })
    ElMessage.success('已从知识库移除')
    await loadDetail()
  } catch (err: any) {
    if (err !== 'cancel' && err?.message !== 'cancel') {
      ElMessage.error('移除失败')
    }
  } finally {
    removingDoc.value = ''
  }
}

// ── Add files to KB ──
async function openAddDialog() {
  showSettingsMenu.value = false
  showAddDialog.value = true
  selectedFile.value = null
  await loadAvailableFiles()
}

async function loadAvailableFiles() {
  loadingFiles.value = true
  try {
    const res = await api.get('/files', {
      headers: authStore.getAuthHeaders()
    })
    const kbFileNames = new Set(documents.value.map(d => d.filename))
    availableFiles.value = (res.data.files || []).filter(
      (f: any) => !f.in_kb && !kbFileNames.has(f.name)
    )
  } catch {
    ElMessage.error('加载文件列表失败')
  } finally {
    loadingFiles.value = false
  }
}

async function addFileToKB() {
  if (!selectedFile.value) return
  addingFile.value = true
  try {
    const formData = new FormData()
    const res = await fetch(`/data/upload/${encodeURIComponent(selectedFile.value.name)}`)
    if (!res.ok) throw new Error('无法读取文件')
    const blob = await res.blob()
    formData.append('file', blob, selectedFile.value.name)

    await api.post(`/knowledge-bases/${kbId}/documents`, formData, {
      headers: authStore.getAuthHeaders()
    })
    ElMessage.success('文件已添加到知识库')
    showAddDialog.value = false
    await loadDetail()
  } catch {
    ElMessage.error('添加文件失败')
  } finally {
    addingFile.value = false
  }
}

function getStatusType(status: string) {
  switch (status) {
    case 'indexed': return 'success'
    case 'pending': return 'warning'
    case 'error': return 'danger'
    default: return 'info'
  }
}

function getStatusLabel(status: string) {
  switch (status) {
    case 'indexed': return '已索引'
    case 'pending': return '处理中'
    case 'error': return '错误'
    default: return status || '未知'
  }
}

function parseToc(toc: string | object): any {
  if (!toc) return {}
  if (typeof toc === 'string') {
    try { return JSON.parse(toc) } catch { return {} }
  }
  return toc
}

</script>

<template>
  <div class="kb-detail-layout">
    <!-- Conversation sidebar (left) -->
    <aside class="conv-sidebar">
      <div class="conv-sidebar-header">
        <span class="conv-sidebar-title">对话历史</span>
        <button class="new-chat-btn" @click="newConversation">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="8" y1="2" x2="8" y2="14"/>
            <line x1="2" y1="8" x2="14" y2="8"/>
          </svg>
          新建
        </button>
      </div>

      <div class="conv-list">
        <div
          v-for="conv in chatStore.conversations"
          :key="conv.id"
          :class="['conv-item', { active: conv.id === chatStore.currentConvId }]"
          @click="selectConversation(conv.id)"
        >
          <div class="conv-icon">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M2 3h12v8H4l-2 2V3z"/>
            </svg>
          </div>
          <div class="conv-info">
            <div class="conv-title">{{ conv.title || '新对话' }}</div>
            <div class="conv-time">{{ formatTime(conv.created_at) }}</div>
          </div>
          <button class="conv-delete" @click.stop="deleteConversation(conv.id)" title="删除">
            <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
              <line x1="3" y1="3" x2="11" y2="11"/>
              <line x1="11" y1="3" x2="3" y2="11"/>
            </svg>
          </button>
        </div>

        <div v-if="!chatStore.conversations.length" class="empty-conv">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
            <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>
          </svg>
          <span>暂无对话</span>
        </div>
      </div>

      <div class="conv-sidebar-footer">
        {{ chatStore.conversations.length }} 个对话
      </div>
    </aside>

    <!-- KB info panel (middle) -->
    <div class="kb-info-panel">
      <!-- Header -->
      <div class="kb-info-header">
        <el-button text size="small" @click="goBack">
          <el-icon style="margin-right: 4px;"><ArrowLeft /></el-icon>
          返回
        </el-button>
        <div class="kb-info-title-row">
          <h1 class="kb-info-title">{{ kbName }}</h1>
          <el-dropdown trigger="click" v-model:visible="showSettingsMenu">
            <el-button text size="small" class="settings-btn">
              <el-icon><Setting /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="openRenameDialog">
                  <el-icon style="margin-right: 6px;"><Edit /></el-icon>
                  编辑名称
                </el-dropdown-item>
                <el-dropdown-item @click="generateTOC" :loading="generatingToc" :disabled="generatingToc">
                  <el-icon style="margin-right: 6px;"><Document /></el-icon>
                  {{ generatingToc ? '生成中...' : '生成目录' }}
                </el-dropdown-item>
                <el-dropdown-item @click="generateOverview" :loading="generatingOverview" :disabled="generatingOverview">
                  <el-icon style="margin-right: 6px;"><Document /></el-icon>
                  {{ generatingOverview ? '生成中...' : '生成概述' }}
                </el-dropdown-item>
                <el-dropdown-item @click="openAddDialog">
                  <el-icon style="margin-right: 6px;"><Upload /></el-icon>
                  添加文件
                </el-dropdown-item>
                <el-dropdown-item divided @click="deleteKB" class="danger-item">
                  <el-icon style="margin-right: 6px;"><Delete /></el-icon>
                  删除知识库
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <!-- Scrollable content -->
      <div class="kb-info-body">
        <!-- Overview -->
        <div class="kb-section">
          <div class="kb-section-header">
            <h3 class="kb-section-title">概述</h3>
            <el-button v-if="!editingOverview" text size="small" @click="startEditOverview">
              <el-icon style="margin-right: 4px;"><Edit /></el-icon>
              编辑
            </el-button>
          </div>
          <div v-if="loading" class="skeleton-line" style="width: 100%; height: 40px;"></div>
          <div v-else-if="editingOverview" class="kb-overview-edit">
            <el-input v-model="overviewDraft" type="textarea" :rows="3" placeholder="输入概述..." />
            <div class="kb-overview-edit-actions">
              <el-button size="small" @click="cancelEditOverview">取消</el-button>
              <el-button type="primary" size="small" :loading="savingOverview" @click="saveOverview">保存</el-button>
            </div>
          </div>
          <div v-else>
            <p v-if="kbOverview" class="kb-overview-text">{{ kbOverview }}</p>
            <p v-else class="kb-overview-empty">暂无概述</p>
          </div>
        </div>

        <!-- Documents -->
        <div class="kb-section">
          <div class="kb-section-header">
            <h3 class="kb-section-title">文档</h3>
            <el-button type="primary" size="small" text @click="openAddDialog">
              <el-icon style="margin-right: 4px;"><Plus /></el-icon>
              添加
            </el-button>
          </div>

          <div v-if="loading" class="kb-docs-list">
            <div v-for="i in 3" :key="i" class="skeleton-line" style="height: 32px; margin-bottom: 8px;"></div>
          </div>
          <div v-else-if="documents.length === 0" class="kb-docs-empty">
            <p>暂无文档</p>
          </div>
          <div v-else class="kb-docs-list">
            <div v-for="doc in documents" :key="doc.filename" class="kb-doc-item">
              <div class="kb-doc-header" @click="doc._showToc = !doc._showToc">
                <div class="kb-doc-icon">📄</div>
                <div class="kb-doc-info">
                  <div class="kb-doc-name">{{ doc.filename }}</div>
                  <div class="kb-doc-meta">
                    {{ doc.chunk_count }} 块
                    <el-tag v-if="doc.status" :type="getStatusType(doc.status)" size="small" effect="plain">
                      {{ getStatusLabel(doc.status) }}
                    </el-tag>
                  </div>
                </div>
                <div class="kb-doc-actions">
                  <el-button text size="small" @click.stop="removeFromKB(doc)" title="从知识库移除">移除</el-button>
                  <span v-if="doc.toc" class="toc-toggle">{{ doc._showToc ? '▲' : '▼' }}</span>
                </div>
              </div>
              <!-- TOC 展开区域 -->
              <div v-if="doc._showToc && doc.toc" class="kb-doc-toc">
                <div v-if="parseToc(doc.toc).title" class="toc-title">{{ parseToc(doc.toc).title }}</div>
                <ul v-if="parseToc(doc.toc).sections?.length" class="toc-list">
                  <li v-for="(section, i) in parseToc(doc.toc).sections" :key="i" class="toc-section">
                    <span class="toc-section-title">{{ section.title }}</span>
                    <ul v-if="section.subsections?.length" class="toc-subsections">
                      <li v-for="(sub, j) in section.subsections" :key="j" class="toc-subsection">{{ sub.title || sub }}</li>
                    </ul>
                  </li>
                </ul>
                <p v-else class="toc-empty">无目录结构</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Chat area (right) -->
    <div class="kb-chat-area">
      <div ref="messagesContainer" class="messages">
        <!-- No conversations at all -->
        <div v-if="!chatStore.conversations.length" class="empty-chat">
          <el-icon :size="48" style="color: var(--color-border);"><ChatDotRound /></el-icon>
          <h3>开始在知识库中提问</h3>
          <p>创建对话后，在此知识库中检索问答</p>
          <el-button type="primary" @click="newConversation" style="margin-top: 12px;">
            <el-icon style="margin-right: 6px;"><Plus /></el-icon>
            新建对话
          </el-button>
        </div>

        <!-- Conversation selected but no messages -->
        <div v-else-if="!chatStore.messages.length" class="empty-chat">
          <el-icon :size="48" style="color: var(--color-border);"><ChatDotRound /></el-icon>
          <h3>在此知识库中提问</h3>
          <p>选择左侧的文档，或直接提问</p>
        </div>

        <MessageBubble
          v-for="(msg, i) in chatStore.messages"
          :key="i"
          :message="msg"
          :index="i"
          :question="i > 0 && chatStore.messages[i - 1]?.role === 'user' ? chatStore.messages[i - 1].content : ''"
          @add-to-analysis="addToAnalysis"
        />

        <div v-if="chatStore.isStreaming && !chatStore.messages[chatStore.messages.length - 1]?.content" class="msg assistant">
          <div class="msg-avatar ai-avatar">R</div>
          <div class="msg-body">
            <div class="typing"><span></span><span></span><span></span></div>
          </div>
        </div>
      </div>

      <div v-if="chatStore.suggestedQuestions.length" class="suggestions">
        <button v-for="q in chatStore.suggestedQuestions" :key="q" class="suggest-btn" @click="askSuggested(q)">
          {{ q }}
        </button>
      </div>

      <ChatInput v-if="chatStore.currentConvId" mode="kb" />
    </div>

    <!-- Dialogs -->
    <el-dialog v-model="showRenameDialog" title="重命名知识库" width="420px" :close-on-click-modal="false">
      <el-input v-model="renameDraft" placeholder="输入知识库名称" maxlength="100" @keyup.enter="renameKB" />
      <template #footer>
        <el-button @click="showRenameDialog = false">取消</el-button>
        <el-button type="primary" :loading="renaming" @click="renameKB">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showAddDialog" title="添加文件到知识库" width="520px" :close-on-click-modal="false">
      <div v-if="loadingFiles" class="kb-add-loading"><p>加载中...</p></div>
      <div v-else-if="availableFiles.length === 0" class="kb-add-empty">
        <p>暂无可添加的文件</p>
        <p class="kb-add-hint">请先在「文件管理」页面上传文件</p>
      </div>
      <div v-else class="kb-add-list">
        <div v-for="file in availableFiles" :key="file.name" class="kb-add-item"
             :class="{ 'kb-add-item--selected': selectedFile?.name === file.name }"
             @click="selectedFile = file">
          <div class="kb-add-item-info">
            <div class="kb-add-item-name">{{ file.name }}</div>
            <div class="kb-add-item-meta">{{ file.size_human }}</div>
          </div>
          <div v-if="selectedFile?.name === file.name" class="kb-add-item-check">
            <el-icon><Check /></el-icon>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="showAddDialog = false">取消</el-button>
        <el-button type="primary" :loading="addingFile" :disabled="!selectedFile" @click="addFileToKB">添加</el-button>
      </template>
    </el-dialog>

    <AddToAnalysisDialog v-model:visible="dialogVisible" :question="dialogQuestion" :answer="dialogAnswer" @confirm="handleConfirm" />
  </div>
</template>

<style scoped>
.kb-detail-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}

/* ── Conversation Sidebar (left) ──────────────────────── */
.conv-sidebar {
  width: 220px;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.conv-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.conv-sidebar-title {
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
  color: var(--color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.new-chat-btn {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-3);
  background: var(--color-foreground);
  color: white;
  border: none;
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  font-family: var(--font-body);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.new-chat-btn:hover {
  background: var(--color-primary);
  transform: translateY(-1px);
}

.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.conv-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.conv-item:hover {
  background: var(--color-muted);
}

.conv-item.active {
  background: var(--color-accent-light);
}

.conv-icon {
  width: 28px;
  height: 28px;
  background: var(--color-muted);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-secondary);
  flex-shrink: 0;
}

.conv-item.active .conv-icon {
  background: var(--color-accent);
  color: white;
}

.conv-info {
  flex: 1;
  min-width: 0;
}

.conv-title {
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--color-foreground);
}

.conv-time {
  font-size: 10px;
  color: var(--color-secondary);
  margin-top: 1px;
}

.conv-delete {
  opacity: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  color: var(--color-secondary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--duration-fast);
  flex-shrink: 0;
}

.conv-item:hover .conv-delete {
  opacity: 1;
}

.conv-delete:hover {
  background: var(--color-destructive-light);
  color: var(--color-destructive);
}

.empty-conv {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-8) var(--space-4);
  color: var(--color-secondary);
  font-size: var(--text-xs);
}

.conv-sidebar-footer {
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
  font-size: 11px;
  color: var(--color-secondary);
}

/* ── KB Info Panel (middle) ────────────────────────────── */
.kb-info-panel {
  width: 320px;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
}

.kb-info-header {
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.kb-info-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: var(--space-2);
}

.kb-info-title {
  font-family: var(--font-heading);
  font-size: var(--text-xl);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
  margin: 0;
}

.kb-info-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5);
}

.kb-section {
  margin-bottom: var(--space-6);
}

.kb-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-3);
}

.kb-section-title {
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
  text-transform: uppercase;
  letter-spacing: var(--tracking-wider);
  color: var(--color-secondary);
  margin: 0;
}

.kb-overview-text {
  font-size: var(--text-sm);
  color: var(--color-foreground);
  line-height: var(--leading-relaxed);
}

.kb-overview-empty {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  font-style: italic;
}

.kb-overview-edit {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.kb-overview-edit-actions {
  display: flex;
  gap: var(--space-2);
  justify-content: flex-end;
}

/* ── Document List ─────────────────────────────────────── */
.kb-docs-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.kb-doc-item {
  border-radius: var(--radius);
  transition: background var(--duration-fast);
}

.kb-doc-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
}

.kb-doc-item:hover {
  background: var(--color-surface-2);
}

.toc-toggle {
  font-size: 10px;
  color: var(--color-secondary);
  margin-left: var(--space-1);
}

.kb-doc-toc {
  padding: var(--space-2) var(--space-3) var(--space-3) calc(var(--space-3) + 18px + var(--space-3));
  border-top: 1px solid var(--color-border);
  animation: fadeIn 0.2s ease-out;
}

.toc-title {
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  color: var(--color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: var(--space-2);
}

.toc-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.toc-section {
  margin-bottom: var(--space-1);
}

.toc-section-title {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.toc-section-title::before {
  content: '•';
  color: var(--color-primary);
  font-weight: bold;
}

.toc-subsections {
  list-style: none;
  padding-left: var(--space-4);
  margin: var(--space-1) 0 0 0;
}

.toc-subsection {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  padding: 2px 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.toc-subsection::before {
  content: '◦';
  color: var(--color-muted);
}

.toc-empty {
  font-size: var(--text-xs);
  color: var(--color-muted);
  font-style: italic;
}

.kb-doc-icon {
  font-size: 18px;
  flex-shrink: 0;
}

.kb-doc-info {
  flex: 1;
  min-width: 0;
}

.kb-doc-name {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kb-doc-meta {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: 2px;
}

.kb-doc-actions {
  display: flex;
  gap: var(--space-1);
  opacity: 0;
  transition: opacity var(--duration-fast);
}

.kb-doc-item:hover .kb-doc-actions {
  opacity: 1;
}

.kb-docs-empty {
  text-align: center;
  padding: var(--space-6);
  color: var(--color-secondary);
  font-size: var(--text-sm);
}

/* ── Chat Area (right) ─────────────────────────────────── */
.kb-chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-background);
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-6) 0;
}

.empty-chat {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--color-secondary);
  gap: var(--space-2);
}

.empty-chat h3 {
  font-family: var(--font-heading);
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
  margin: 0;
}

.empty-chat p {
  font-size: var(--text-sm);
  color: var(--color-secondary);
}

/* ── Suggestions ──────────────────────────────────────── */
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: 0 var(--space-6) var(--space-3);
}

.suggest-btn {
  padding: var(--space-2) var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  color: var(--color-secondary);
  cursor: pointer;
  font-family: var(--font-body);
  transition: all var(--duration-fast);
}

.suggest-btn:hover {
  border-color: var(--color-accent);
  color: var(--color-accent);
}

.kb-detail-title {
  font-family: var(--font-heading);
  font-size: var(--text-xl);
  font-weight: var(--font-bold);
  color: var(--color-foreground);
  margin: 0;
}

.settings-btn {
  font-size: 18px;
  color: var(--color-secondary);
}

.settings-btn:hover {
  color: var(--color-foreground);
}

/* ── Typing indicator ─────────────────────────────────── */
.typing {
  display: flex;
  gap: 6px;
  padding: var(--space-4);
}

.typing span {
  width: 8px;
  height: 8px;
  background: var(--color-border);
  border-radius: 50%;
  animation: bounce 1.4s infinite;
}

.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }

@keyframes bounce {
  0%, 60%, 100% { transform: translateY(0); }
  30% { transform: translateY(-8px); }
}

.msg {
  display: flex;
  gap: var(--space-4);
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: var(--space-3) var(--space-6);
  animation: slideUp 0.3s var(--ease-out);
}

.msg-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 13px;
  font-weight: var(--font-bold);
}

.ai-avatar {
  background: var(--color-foreground);
  color: white;
  font-family: var(--font-mono);
  font-size: 12px;
}

.msg-body {
  flex: 1;
  min-width: 0;
  max-width: 85%;
}

/* ── Add file dialog ── */
.kb-add-loading,
.kb-add-empty {
  text-align: center;
  padding: var(--space-8) var(--space-4);
  color: var(--color-secondary);
  font-size: var(--text-sm);
}

.kb-add-hint {
  font-size: var(--text-xs);
  color: var(--color-muted);
  margin-top: var(--space-2);
}

.kb-add-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 360px;
  overflow-y: auto;
}

.kb-add-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.kb-add-item:hover {
  border-color: var(--color-accent);
  background: var(--color-accent-light);
}

.kb-add-item--selected {
  border-color: var(--color-accent);
  background: var(--color-accent-light);
}

.kb-add-item-info {
  flex: 1;
  min-width: 0;
}

.kb-add-item-name {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kb-add-item-meta {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  margin-top: 2px;
}

.kb-add-item-check {
  color: var(--color-accent);
  font-size: 18px;
  flex-shrink: 0;
  margin-left: var(--space-3);
}

/* ── Danger item in dropdown ── */
.danger-item {
  color: var(--color-destructive) !important;
}

.danger-item:hover {
  background: var(--color-destructive-light) !important;
}

/* ── Skeleton ── */
.skeleton-line {
  border-radius: var(--radius-sm);
  background: var(--color-muted);
  animation: shimmer 1.5s infinite;
  background-size: 200% 100%;
  background-image: linear-gradient(90deg, var(--color-muted) 25%, var(--color-border) 50%, var(--color-muted) 75%);
}

.skeleton-line--title {
  width: 60%;
  height: 16px;
}

@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

@keyframes slideUp {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
