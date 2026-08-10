<script setup lang="ts">
import { ref, nextTick, watch, onMounted } from 'vue'
import { useChatStore } from '../stores/chat'
import { useFilesStore } from '../stores/files'
import { useAuthStore } from '../stores/auth'
import { useAnalysis } from '../composables/useAnalysis'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, FolderOpened, Plus, ChatDotRound } from '@element-plus/icons-vue'
import MessageBubble from '../components/MessageBubble.vue'
import ChatInput from '../components/ChatInput.vue'
import AddToAnalysisDialog from '../components/AddToAnalysisDialog.vue'
import ArchiveManifestDialog from '../components/ArchiveManifestDialog.vue'
import OrganizationPreviewDialog from '../components/OrganizationPreviewDialog.vue'
import RecycleBinDialog from '../components/RecycleBinDialog.vue'
import ConversationSearch from '../components/ConversationSearch.vue'
import DocumentCompareDialog from '../components/DocumentCompareDialog.vue'
import api from '../utils/api'

import { computed } from 'vue'

const chatStore = useChatStore()
const filesStore = useFilesStore()
const authStore = useAuthStore()
const { addToAnalysis, dialogVisible, dialogQuestion, dialogAnswer, handleConfirm } = useAnalysis()
const messagesContainer = ref<HTMLElement>()
const dragover = ref(false)

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

function getFileIcon(ext: string): string {
  const icons: Record<string, string> = { '.pdf': '📕', '.docx': '📘', '.xlsx': '📊', '.csv': '📊', '.md': '📝', '.txt': '📄' }
  return icons[ext] || '📄'
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

async function onSearchSelect(conversationId: number) {
  await chatStore.selectConversation(conversationId)
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
</script>

<template>
  <div class="file-mode">
    <!-- Conversation sidebar (left) -->
    <aside class="conv-sidebar">
      <div class="conv-sidebar-header">
        <span class="conv-sidebar-title">对话历史</span>
        <button class="new-chat-btn" @click="newConversation" aria-label="新建对话">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="8" y1="2" x2="8" y2="14"/>
            <line x1="2" y1="8" x2="14" y2="8"/>
          </svg>
          新建
        </button>
      </div>

      <ConversationSearch @select="onSearchSelect" />

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
          <button class="conv-delete" @click.stop="deleteConversation(conv.id)" title="删除" aria-label="删除对话">
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

    <!-- File management panel (middle) -->
    <div class="file-panel">
      <div class="file-panel-header">
        <h1 class="file-panel-title">文件管理</h1>
        <el-button size="small" :disabled="!comparisonDocuments.length" @click="comparisonVisible = true">
          资料比较
        </el-button>
        <el-upload :show-file-list="false" :before-upload="() => false" :on-change="async (f: any) => { try { await filesStore.uploadFile(f.raw!); ElMessage.success('上传成功') } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '上传失败') } }" accept=".txt,.md,.pdf,.docx,.xlsx,.csv">
          <el-button size="small" type="primary">
            <el-icon><Upload /></el-icon>
            上传
          </el-button>
        </el-upload>
      </div>

      <!-- Drop zone -->
      <div class="drop-zone" @dragover.prevent="dragover = true" @dragleave="dragover = false" @drop.prevent="handleDrop($event)" :class="{ active: dragover }">
        <el-icon class="drop-icon"><Upload /></el-icon>
        <p class="drop-text">拖拽文件到此处上传</p>
        <p class="drop-hint">支持 txt、md、pdf、docx、xlsx、csv</p>
      </div>

      <!-- File type filter tabs -->
      <div class="file-filter-tabs">
        <button :class="['filter-tab', { active: fileFilter === 'all' }]" @click="fileFilter = 'all'">全部</button>
        <button :class="['filter-tab', { active: fileFilter === 'default' }]" @click="fileFilter = 'default'">默认</button>
        <button :class="['filter-tab', { active: fileFilter === 'private' }]" @click="fileFilter = 'private'">私有</button>
        <button :class="['filter-tab', { active: fileFilter === 'public' }]" @click="fileFilter = 'public'">共享</button>
      </div>

      <!-- File list -->
      <div class="file-list">
        <div :class="['file-item', { selected: !chatStore.selectedFile }]" @click="chatStore.selectFile(null)">
          <div class="file-item-icon">📁</div>
          <div class="file-item-info">
            <div class="file-item-name">全部文件</div>
            <div class="file-item-size">搜索所有文件</div>
          </div>
        </div>
        <div v-for="file in filteredFiles" :key="file.name"
             :class="['file-item', { selected: chatStore.selectedFile === file.name }]"
             @click="chatStore.selectFile(file.name)">
          <div class="file-item-icon">{{ getFileIcon(file.ext) }}</div>
          <div class="file-item-info">
            <div class="file-item-name" :title="file.name">{{ file.name }}</div>
            <div class="file-item-size">{{ file.size_human }}</div>
          </div>
          <div class="file-item-actions">
            <el-dropdown trigger="click" @command="(cmd: string) => handleFileAction(cmd, file.name)" @click.stop>
              <button class="file-item-more" @click.stop>⋯</button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="download">下载</el-dropdown-item>
                  <el-dropdown-item v-if="!file.protected" :command="file.is_public ? 'private' : 'public'">
                    {{ file.is_public ? '切换为私有' : '切换为共享' }}
                  </el-dropdown-item>
                  <el-dropdown-item v-if="!file.protected" command="delete" divided>删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </div>
        <div v-if="!filteredFiles.length" class="empty-files">
          <el-icon class="empty-icon"><FolderOpened /></el-icon>
          <p>暂无文件</p>
        </div>
      </div>
    </div>

    <!-- Chat area (right) -->
    <div class="chat-area">
      <div ref="messagesContainer" class="messages">
        <div v-if="!chatStore.conversations.length" class="empty-chat">
          <el-icon :size="48" style="color: var(--color-border);"><ChatDotRound /></el-icon>
          <h3>开始你的第一个对话</h3>
          <p>上传文档后，向知识库提问</p>
          <el-button type="primary" @click="newConversation" style="margin-top: 12px;">
            <el-icon style="margin-right: 6px;"><Plus /></el-icon>
            新建对话
          </el-button>
        </div>

        <div v-else-if="!chatStore.messages.length" class="empty-chat">
          <el-icon :size="48" style="color: var(--color-border);"><ChatDotRound /></el-icon>
          <h3>开始对话</h3>
          <p>在下方输入你的问题</p>
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

      <ChatInput v-if="chatStore.currentConvId" mode="file" />
    </div>

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
}

/* ── Conversation Sidebar ─────────────────────────────── */
.conv-sidebar {
  width: 240px;
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
  background: var(--color-primary);
  color: var(--color-on-primary);
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  font-family: var(--font-body);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.new-chat-btn:hover {
  background: var(--color-primary-hover);
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
  background: var(--color-surface-2);
}

.conv-item.active {
  background: var(--color-accent-light);
}

.conv-icon {
  width: 28px;
  height: 28px;
  background: var(--color-surface-2);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-secondary);
  flex-shrink: 0;
}

.conv-item.active .conv-icon {
  background: var(--color-primary);
  color: var(--color-on-primary);
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

/* ── File Panel (middle) ───────────────────────────────── */
.file-panel {
  width: 320px;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
}

.file-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.file-panel-title {
  font-family: var(--font-heading);
  font-size: var(--text-xl);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
  margin: 0;
}

.drop-zone {
  border: 2px dashed var(--color-border);
  border-radius: var(--radius);
  padding: var(--space-4) var(--space-3);
  text-align: center;
  margin: var(--space-3) var(--space-4);
  transition: all var(--duration-normal) var(--ease-out);
  cursor: pointer;
}

.drop-zone:hover, .drop-zone.active {
  border-color: var(--color-primary);
  background: var(--color-accent-light);
}

.drop-icon {
  font-size: 24px;
  color: var(--color-secondary);
  margin-bottom: var(--space-1);
}

.drop-text {
  font-size: var(--text-sm);
  color: var(--color-foreground);
  margin-bottom: 2px;
}

.drop-hint {
  font-size: var(--text-xs);
  color: var(--color-secondary);
}

.file-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-2) var(--space-4);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.file-filter-tabs {
  display: flex;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
}

.filter-tab {
  padding: var(--space-1) var(--space-3);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  color: var(--color-secondary);
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  font-family: var(--font-body);
}

.filter-tab:hover {
  background: var(--color-surface-2);
  border-color: var(--color-border-hover);
}

.filter-tab.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: var(--color-on-primary);
}

.file-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.file-item:hover {
  background: var(--color-surface-2);
}

.file-item.selected {
  background: var(--color-accent-light);
}

.file-item-icon {
  font-size: 18px;
  flex-shrink: 0;
}

.file-item-info {
  flex: 1;
  min-width: 0;
}

.file-item-name {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-item-size {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  margin-top: 1px;
}

.file-item-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.file-item-more {
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
  font-size: 16px;
  font-weight: bold;
}

.file-item:hover .file-item-more {
  opacity: 1;
}

.file-item-more:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

.empty-files {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-8);
  color: var(--color-secondary);
}

.empty-icon {
  font-size: 24px;
  color: var(--color-border);
}

/* ── Chat Area (right) ─────────────────────────────────── */
.chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-background);
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-8) 0;
}

.empty-chat {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--color-secondary);
  gap: var(--space-3);
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

/* ── Typing ───────────────────────────────────────────── */
.msg {
  display: flex;
  gap: var(--space-4);
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: var(--space-3) var(--space-6);
}

.msg-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 14px;
  font-weight: var(--font-bold);
}

.ai-avatar {
  background: var(--color-primary);
  color: var(--color-on-primary);
  font-family: var(--font-mono);
  font-size: 13px;
}

.msg-body {
  flex: 1;
  min-width: 0;
  max-width: 85%;
}

.typing {
  display: flex;
  gap: 6px;
  padding: var(--space-5);
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

/* ── Suggestions ──────────────────────────────────────── */
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: 0 var(--space-6) var(--space-4);
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
  transition: all var(--duration-normal) var(--ease-out);
}

.suggest-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}
</style>
