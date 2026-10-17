<script setup lang="ts">
import { ref, nextTick, watch, onMounted, computed } from 'vue'
import { useChatStore } from '../stores/chat'
import { useFilesStore } from '../stores/files'
import { useAuthStore } from '../stores/auth'
import { useAnalysis } from '../composables/useAnalysis'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, FolderOpened, Plus, ChatDotRound } from '@element-plus/icons-vue'
import MessageBubble from '../components/MessageBubble.vue'
import ChatInput from '../components/ChatInput.vue'
import ConversationSidebar from '../components/ConversationSidebar.vue'
import AddToAnalysisDialog from '../components/AddToAnalysisDialog.vue'
import ArchiveManifestDialog from '../components/ArchiveManifestDialog.vue'
import OrganizationPreviewDialog from '../components/OrganizationPreviewDialog.vue'
import RecycleBinDialog from '../components/RecycleBinDialog.vue'
import DocumentCompareDialog from '../components/DocumentCompareDialog.vue'
import api from '../utils/api'

const chatStore = useChatStore()
const filesStore = useFilesStore()
const authStore = useAuthStore()
const { addToAnalysis, dialogVisible, dialogQuestion, dialogAnswer, handleConfirm } = useAnalysis()
const messagesContainer = ref<HTMLElement>()
const dragover = ref(false)

const convSidebarCollapsed = ref(false)
const filePanelCollapsed = ref(false)

// Reload files when user info loads (handles async fetchUser after page refresh)
watch(() => authStore.user, (newUser) => {
  if (newUser) {
    filesStore.loadFiles(true)
  }
})

// File filter
const fileFilter = ref<'all' | 'default' | 'private' | 'public'>('all')
const comparisonVisible = ref(false)

const filteredFiles = computed(() => {
  if (fileFilter.value === 'all') return filesStore.files
  return filesStore.files.filter(f => {
    if (fileFilter.value === 'default') return f.protected
    if (fileFilter.value === 'private') return !f.protected && !f.is_public
    if (fileFilter.value === 'public') return !f.protected && f.is_public
    return true
  })
})

const comparisonDocuments = computed(() => filesStore.files.flatMap((file) => (
  typeof file.document_id === 'number'
    ? [{ id: file.document_id, name: file.name }]
    : []
)))

// File action handler (dropdown menu)
async function handleFileAction(command: string, fileName: string) {
  if (command === 'delete') {
    await handleDelete(fileName)
  } else if (command === 'public' || command === 'private') {
    await toggleVisibility(fileName)
  } else if (command === 'download') {
    await downloadFile(fileName)
  }
}

// Visibility toggle
async function toggleVisibility(fileName: string) {
  try {
    const resp = await api.put(`/files/${encodeURIComponent(fileName)}/visibility`)
    const data = resp.data
    const file = filesStore.files.find(f => f.name === fileName)
    if (file) file.is_public = data.is_public
    ElMessage.success(data.is_public ? '已切换为共享' : '已切换为私有')
  } catch (e: any) {
    ElMessage.error(e.message || '切换失败')
  }
}

function getFileTypeClass(ext: string): string {
  const map: Record<string, string> = {
    '.pdf': 'badge-pdf',
    '.docx': 'badge-doc',
    '.xlsx': 'badge-sheet',
    '.csv': 'badge-sheet',
    '.md': 'badge-md',
    '.txt': 'badge-txt'
  }
  return map[ext] || 'badge-file'
}

const ALLOWED_EXTS = ['.txt', '.md', '.pdf', '.docx', '.xlsx', '.csv']

async function handleDrop(e: DragEvent) {
  dragover.value = false
  const droppedFiles = e.dataTransfer?.files
  if (!droppedFiles) return

  for (const file of Array.from(droppedFiles)) {
    const ext = '.' + file.name.split('.').pop()?.toLowerCase()
    if (!ALLOWED_EXTS.includes(ext)) {
      ElMessage.warning(`不支持的文件格式: ${file.name}`)
      continue
    }
    try {
      await filesStore.uploadFile(file)
      ElMessage.success(`文件 ${file.name} 上传成功`)
    } catch (err: any) {
      ElMessage.error(err.response?.data?.detail || `上传失败: ${file.name}`)
    }
  }
}

async function handleDelete(name: string) {
  try {
    await ElMessageBox.confirm(`确定删除文件 "${name}"？`, '确认删除', { type: 'warning' })
    await filesStore.deleteFile(name)
    if (chatStore.selectedFile === name) {
      chatStore.selectFile(null)
    }
    ElMessage.success('文件已删除')
  } catch (e: any) {
    if (e !== 'cancel' && e?.message !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

async function downloadFile(name: string) {
  try {
    const response = await api.get(`/files/${encodeURIComponent(name)}/download`, {
      responseType: 'blob'
    })
    const blob = response.data
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    window.URL.revokeObjectURL(url)
  } catch (e: any) {
    ElMessage.error(e.message || '下载失败')
  }
}

// ── Material lifecycle handlers (archives, recycle, organization) ──────────
async function onArchiveConfirm(selectedIds: number[]) {
  try {
    await filesStore.confirmArchiveImport(selectedIds)
    ElMessage.success('压缩包内容已导入')
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || '压缩包导入失败')
  }
}

async function onOrganizationConfirm(selectedIds: number[]) {
  try {
    await filesStore.confirmOrganization(selectedIds)
    ElMessage.success('资料整理完成')
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || '整理失败')
  }
}

async function onRestoreDocument(documentId: number) {
  try {
    await filesStore.restoreDocument(documentId)
    ElMessage.success('已恢复')
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || '恢复失败')
  }
}

onMounted(async () => {
  await chatStore.loadConversations('file')
  if (chatStore.conversations.length > 0 && !chatStore.currentConvId) {
    await chatStore.selectConversation(chatStore.conversations[0].id)
  }
  filesStore.loadFiles()
})

watch(() => chatStore.messages.length, () => {
  nextTick(() => { messagesContainer.value?.scrollTo({ top: messagesContainer.value.scrollHeight, behavior: 'smooth' }) })
})

watch(() => chatStore.messages[chatStore.messages.length - 1]?.content, () => {
  nextTick(() => { messagesContainer.value?.scrollTo({ top: messagesContainer.value.scrollHeight, behavior: 'smooth' }) })
})

async function newConversation() {
  await chatStore.createConversation('file')
}

function askSuggested(q: string) {
  chatStore.sendMessage(q)
}
</script>

<template>
  <div class="file-mode">
    <!-- Reusable Conversation Sidebar (left) -->
    <ConversationSidebar mode="file" v-model:collapsed="convSidebarCollapsed" />

    <!-- File Management Panel (middle) -->
    <div :class="['file-panel', { collapsed: filePanelCollapsed }]">
      <div v-if="filePanelCollapsed" class="file-panel-collapsed-dock">
        <button class="expand-panel-btn" @click="filePanelCollapsed = false" title="展开文件管理面板">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
        </button>
        <span class="collapsed-count">{{ filteredFiles.length }}</span>
      </div>

      <div v-else class="file-panel-content">
        <div class="file-panel-header">
          <div class="header-title-box">
            <h1 class="file-panel-title">文件管理</h1>
            <span class="file-count-pill">{{ filteredFiles.length }}</span>
          </div>

          <div class="header-btns">
            <el-button size="small" :disabled="!comparisonDocuments.length" @click="comparisonVisible = true">
              资料对比
            </el-button>
            <el-upload
              :show-file-list="false"
              :before-upload="() => false"
              :on-change="async (f: any) => { try { await filesStore.uploadFile(f.raw!); ElMessage.success('上传成功') } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '上传失败') } }"
              accept=".txt,.md,.pdf,.docx,.xlsx,.csv"
            >
              <el-button size="small" type="primary">
                <el-icon><Upload /></el-icon>
                上传
              </el-button>
            </el-upload>
            <button class="collapse-icon-btn" @click="filePanelCollapsed = true" title="折叠文件面板">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="15 18 9 12 15 6"/>
              </svg>
            </button>
          </div>
        </div>

        <!-- Drop zone -->
        <div
          class="drop-zone"
          :class="{ active: dragover }"
          @dragover.prevent="dragover = true"
          @dragleave="dragover = false"
          @drop.prevent="handleDrop($event)"
        >
          <div class="drop-icon-wrap">
            <el-icon><Upload /></el-icon>
          </div>
          <p class="drop-text">拖拽文件到此处上传</p>
          <p class="drop-hint">支持 pdf, docx, xlsx, csv, md, txt</p>
        </div>

        <!-- Filter tabs -->
        <div class="file-filter-tabs">
          <button :class="['filter-tab', { active: fileFilter === 'all' }]" @click="fileFilter = 'all'">全部</button>
          <button :class="['filter-tab', { active: fileFilter === 'default' }]" @click="fileFilter = 'default'">默认</button>
          <button :class="['filter-tab', { active: fileFilter === 'private' }]" @click="fileFilter = 'private'">私有</button>
          <button :class="['filter-tab', { active: fileFilter === 'public' }]" @click="fileFilter = 'public'">共享</button>
        </div>

        <!-- File list -->
        <div class="file-list">
          <div :class="['file-item', { selected: !chatStore.selectedFile }]" @click="chatStore.selectFile(null)">
            <div class="file-item-badge badge-all">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              </svg>
            </div>
            <div class="file-item-info">
              <div class="file-item-name">全部文件</div>
              <div class="file-item-size">跨全部文档联合检索</div>
            </div>
          </div>

          <div
            v-for="file in filteredFiles"
            :key="file.name"
            :class="['file-item', { selected: chatStore.selectedFile === file.name }]"
            @click="chatStore.selectFile(file.name)"
          >
            <div :class="['file-item-badge', getFileTypeClass(file.ext)]">
              {{ (file.ext || '').replace('.', '').toUpperCase() || 'DOC' }}
            </div>
            <div class="file-item-info">
              <div class="file-item-name" :title="file.name">{{ file.name }}</div>
              <div class="file-item-size">{{ file.size_human }}</div>
            </div>
            <div class="file-item-actions">
              <el-dropdown trigger="click" @command="(cmd: string) => handleFileAction(cmd, file.name)" @click.stop>
                <button class="file-item-more" @click.stop>⋯</button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="download">下载源文件</el-dropdown-item>
                    <el-dropdown-item v-if="!file.protected" :command="file.is_public ? 'private' : 'public'">
                      {{ file.is_public ? '切换为私有' : '切换为共享' }}
                    </el-dropdown-item>
                    <el-dropdown-item v-if="!file.protected" command="delete" divided>删除文件</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </div>

          <div v-if="!filteredFiles.length" class="empty-files">
            <el-icon class="empty-icon"><FolderOpened /></el-icon>
            <p>暂无符合筛选的文件</p>
          </div>
        </div>
      </div>
    </div>

    <!-- Chat Workspace Area (right) -->
    <div class="chat-area">
      <div ref="messagesContainer" class="messages">
        <div v-if="!chatStore.conversations.length" class="empty-chat">
          <div class="empty-hero-icon">
            <el-icon :size="40"><ChatDotRound /></el-icon>
          </div>
          <h3>开启您的智能文档研读</h3>
          <p>上传文档后即可针对内容进行精确检索与交叉提问</p>
          <el-button type="primary" class="hero-start-btn" @click="newConversation">
            <el-icon style="margin-right: 6px;"><Plus /></el-icon>
            新建对话
          </el-button>
        </div>

        <div v-else-if="!chatStore.messages.length" class="empty-chat">
          <div class="empty-hero-icon">
            <el-icon :size="40"><ChatDotRound /></el-icon>
          </div>
          <h3>准备就绪，随时提问</h3>
          <p>在下方输入您对选中文件的疑问，或勾选多模型 Arena 开启双栏竞技对比</p>
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

      <!-- Quick Suggested Questions -->
      <div v-if="chatStore.suggestedQuestions.length" class="suggestions">
        <button v-for="q in chatStore.suggestedQuestions" :key="q" class="suggest-btn" @click="askSuggested(q)">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="suggest-icon">
            <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/>
          </svg>
          <span>{{ q }}</span>
        </button>
      </div>

      <!-- Floating Chat Input -->
      <ChatInput v-if="chatStore.currentConvId" mode="file" />
    </div>

    <!-- Dialogs -->
    <AddToAnalysisDialog
      v-model:visible="dialogVisible"
      :question="dialogQuestion"
      :answer="dialogAnswer"
      @confirm="handleConfirm"
    />
    <ArchiveManifestDialog
      v-model:visible="filesStore.archiveVisible"
      :entries="filesStore.archiveEntries"
      @confirm="onArchiveConfirm"
    />
    <OrganizationPreviewDialog
      v-model:visible="filesStore.organizationVisible"
      :items="filesStore.organizationItems"
      @confirm="onOrganizationConfirm"
    />
    <RecycleBinDialog
      v-model:visible="filesStore.recycleVisible"
      :items="filesStore.recycleItems"
      @restore="onRestoreDocument"
    />
    <DocumentCompareDialog
      v-model:visible="comparisonVisible"
      :documents="comparisonDocuments"
    />
  </div>
</template>

<style scoped>
.file-mode {
  display: flex;
  height: 100%;
  overflow: hidden;
  position: relative;
}

/* ── File Panel (middle) ───────────────────────────────── */
.file-panel {
  width: 300px;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
  transition: width var(--duration-normal) var(--ease-out);
}

.file-panel.collapsed {
  width: 52px;
}

.file-panel-collapsed-dock {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 14px 6px;
  gap: 10px;
  height: 100%;
}

.expand-panel-btn {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border-subtle);
  background: var(--color-surface);
  color: var(--color-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all var(--duration-fast);
}

.expand-panel-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

.collapsed-count {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-muted);
}

.file-panel-content {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 300px;
}

.file-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.header-title-box {
  display: flex;
  align-items: center;
  gap: 8px;
}

.file-panel-title {
  font-family: var(--font-heading);
  font-size: 15px;
  font-weight: 600;
  color: var(--color-foreground);
  margin: 0;
}

.file-count-pill {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  background: var(--color-surface-2);
  color: var(--color-secondary);
  border-radius: var(--radius-full);
}

.header-btns {
  display: flex;
  align-items: center;
  gap: 6px;
}

.collapse-icon-btn {
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--color-muted);
  border-radius: var(--radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all var(--duration-fast);
}

.collapse-icon-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

/* ── Modern Drop Zone ─────────────────────────────────── */
.drop-zone {
  border: 1.5px dashed var(--color-border);
  border-radius: var(--radius-lg);
  padding: 14px 12px;
  text-align: center;
  margin: 10px 14px 6px;
  transition: all var(--duration-normal) var(--ease-out);
  cursor: pointer;
  background: var(--color-background);
}

.drop-zone:hover, .drop-zone.active {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.drop-icon-wrap {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  background: var(--color-surface);
  color: var(--color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 4px;
  box-shadow: var(--shadow-xs);
}

.drop-text {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--color-foreground);
  margin-bottom: 1px;
}

.drop-hint {
  font-size: 11px;
  color: var(--color-muted);
}

/* ── Filter Segmented Tabs ────────────────────────────── */
.file-filter-tabs {
  display: flex;
  gap: 4px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.filter-tab {
  flex: 1;
  padding: 4px 0;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--color-secondary);
  background: none;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--duration-fast);
  text-align: center;
}

.filter-tab:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

.filter-tab.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-weight: 600;
}

/* ── File List ────────────────────────────────────────── */
.file-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--duration-fast);
  border: 1px solid transparent;
}

.file-item:hover {
  background: var(--color-surface-2);
}

.file-item.selected {
  background: var(--color-primary-light);
  border-color: rgba(99, 102, 241, 0.2);
}

.file-item-badge {
  font-size: 10px;
  font-weight: 700;
  padding: 3px 6px;
  border-radius: var(--radius-sm);
  letter-spacing: 0.5px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 34px;
}

.badge-all {
  background: var(--color-surface-2);
  color: var(--color-secondary);
}

.badge-pdf {
  background: #fef2f2;
  color: #ef4444;
  border: 1px solid rgba(239, 68, 68, 0.2);
}

.badge-sheet {
  background: #f0fdf4;
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.2);
}

.badge-doc {
  background: #eff6ff;
  color: #3b82f6;
  border: 1px solid rgba(59, 130, 246, 0.2);
}

.badge-md {
  background: #faf5ff;
  color: #8b5cf6;
  border: 1px solid rgba(139, 92, 246, 0.2);
}

.badge-txt {
  background: #f8fafc;
  color: #64748b;
  border: 1px solid rgba(100, 116, 139, 0.2);
}

.badge-file {
  background: var(--color-surface-2);
  color: var(--color-secondary);
}

.file-item-info {
  flex: 1;
  min-width: 0;
}

.file-item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-item.selected .file-item-name {
  color: var(--color-primary-focus);
  font-weight: 600;
}

.file-item-size {
  font-size: 11px;
  color: var(--color-muted);
}

.file-item-actions {
  display: flex;
  align-items: center;
}

.file-item-more {
  opacity: 0;
  width: 22px;
  height: 22px;
  border-radius: var(--radius-xs);
  border: none;
  background: none;
  color: var(--color-muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  transition: all var(--duration-fast);
}

.file-item:hover .file-item-more {
  opacity: 1;
}

.file-item-more:hover {
  background: var(--color-surface-3);
  color: var(--color-foreground);
}

.empty-files {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 16px;
  color: var(--color-muted);
  gap: 8px;
  font-size: 12.5px;
}

/* ── Chat Canvas Area ─────────────────────────────────── */
.chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-background);
  position: relative;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px 0;
}

.empty-chat {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--color-secondary);
  gap: 12px;
  text-align: center;
  padding: 20px;
}

.empty-hero-icon {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-xl);
  background: linear-gradient(135deg, #eef2ff 0%, #e0e7ff 100%);
  color: var(--color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--shadow-sm);
  margin-bottom: 4px;
}

.empty-chat h3 {
  font-family: var(--font-heading);
  font-size: 18px;
  font-weight: 700;
  color: var(--color-foreground);
  margin: 0;
}

.empty-chat p {
  font-size: 13.5px;
  color: var(--color-secondary);
  max-width: 360px;
}

.hero-start-btn {
  margin-top: 8px;
  box-shadow: 0 4px 12px rgba(79, 70, 229, 0.25);
}

/* ── Typing Indicator ─────────────────────────────────── */
.typing {
  display: flex;
  gap: 6px;
  padding: 16px;
}

.typing span {
  width: 7px;
  height: 7px;
  background: var(--color-primary);
  opacity: 0.6;
  border-radius: 50%;
  animation: bounce 1.4s infinite;
}

.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }

@keyframes bounce {
  0%, 60%, 100% { transform: translateY(0); }
  30% { transform: translateY(-6px); }
}

/* ── Suggestions ──────────────────────────────────────── */
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: 0 24px 10px;
}

.suggest-btn {
  padding: 6px 14px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  font-size: 12.5px;
  font-weight: 500;
  color: var(--color-secondary);
  cursor: pointer;
  transition: all var(--duration-fast);
  box-shadow: var(--shadow-xs);
}

.suggest-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
  transform: translateY(-1px);
  box-shadow: var(--shadow-sm);
}
</style>
