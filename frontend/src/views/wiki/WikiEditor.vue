<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  Edit,
  Delete,
  Plus,
  Clock,
  Check,
  Close
} from '@element-plus/icons-vue'
import type { WikiPageDetail } from '../../api/wiki'
import { formatTime, renderContentWithWikilinks } from './helpers'
import WikiDualLinks from './WikiDualLinks.vue'

const props = defineProps<{
  pageDetail: WikiPageDetail | null
  loading?: boolean
  canEdit?: boolean
  saving?: boolean
}>()

const isEditing = defineModel<boolean>('isEditing', { default: false })

const emit = defineEmits<{
  (e: 'save-edit', payload: { title: string; slug?: string; content: string; tags: string[]; comment: string }): void
  (e: 'delete-page'): void
  (e: 'open-history'): void
  (e: 'select-page', id: number): void
  (e: 'wikilink-click', targetTitle: string): void
  (e: 'open-create', defaultTitle?: string): void
}>()

// Edit fields
const editTitle = ref('')
const editSlug = ref('')
const editContent = ref('')
const editTags = ref<string[]>([])
const editComment = ref('更新内容')
const tagInputVisible = ref(false)
const tagInputValue = ref('')

watch(() => props.pageDetail, (newVal) => {
  if (newVal) {
    // If not editing, sync current values
    if (!isEditing.value) {
      editTitle.value = newVal.page.title
      editSlug.value = newVal.page.slug || ''
      editContent.value = newVal.page.content
      editTags.value = [...(newVal.page.tags || [])]
      editComment.value = '更新内容'
    }
  } else {
    isEditing.value = false
  }
})

function startEdit() {
  if (!props.pageDetail) return
  const p = props.pageDetail.page
  editTitle.value = p.title
  editSlug.value = p.slug || ''
  editContent.value = p.content
  editTags.value = [...(p.tags || [])]
  editComment.value = '更新内容'
  isEditing.value = true
}

function cancelEdit() {
  isEditing.value = false
}

function handleSave() {
  const title = editTitle.value.trim()
  if (!title) {
    ElMessage.warning('请输入标题')
    return
  }
  emit('save-edit', {
    title,
    slug: editSlug.value.trim() || undefined,
    content: editContent.value,
    tags: editTags.value,
    comment: editComment.value.trim() || '更新页面'
  })
}

function removeTag(tag: string) {
  editTags.value = editTags.value.filter(t => t !== tag)
}

function handleInputTagConfirm() {
  if (tagInputValue.value.trim() && !editTags.value.includes(tagInputValue.value.trim())) {
    editTags.value.push(tagInputValue.value.trim())
  }
  tagInputVisible.value = false
  tagInputValue.value = ''
}

function onContentClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  const badge = target.closest('.wikilink-badge') as HTMLElement
  if (badge) {
    const targetTitle = badge.getAttribute('data-target')
    if (targetTitle) {
      emit('wikilink-click', targetTitle)
    }
  }
}
</script>

<template>
  <main v-loading="props.loading" class="wiki-workspace">
    <template v-if="props.pageDetail">
      <!-- View / Edit Top Bar -->
      <div class="workspace-header">
        <div class="title-meta-block">
          <div class="page-title-row">
            <h2 v-if="!isEditing" class="active-title">{{ props.pageDetail.page.title }}</h2>
            <el-input
              v-else
              v-model="editTitle"
              class="edit-title-input"
              placeholder="词条标题"
              size="large"
            />
          </div>

          <div class="page-meta-row">
            <span class="meta-item">
              <el-icon><Clock /></el-icon>
              更新于 {{ formatTime(props.pageDetail.page.updated_at) }}
            </span>
            <span class="meta-item">
              版本: <strong>#{{ props.pageDetail.revision_count }}</strong>
            </span>
            <span v-if="props.pageDetail.page.slug" class="meta-item slug-item">
              Slug: <code>{{ props.pageDetail.page.slug }}</code>
            </span>
          </div>

          <!-- Tags display or edit -->
          <div class="tags-row">
            <template v-if="!isEditing">
              <span v-for="t in props.pageDetail.page.tags || []" :key="t" class="content-tag">
                #{{ t }}
              </span>
            </template>
            <template v-else>
              <el-tag
                v-for="t in editTags"
                :key="t"
                closable
                size="small"
                class="editable-tag"
                @close="removeTag(t)"
              >
                {{ t }}
              </el-tag>
              <el-input
                v-if="tagInputVisible"
                v-model="tagInputValue"
                size="small"
                class="tag-input"
                @keyup.enter="handleInputTagConfirm"
                @blur="handleInputTagConfirm"
              />
              <el-button v-else size="small" :icon="Plus" @click="tagInputVisible = true">
                添加标签
              </el-button>
            </template>
          </div>
        </div>

        <!-- Workspace Action Buttons -->
        <div class="workspace-actions">
          <template v-if="!isEditing">
            <el-button
              v-if="props.canEdit"
              type="primary"
              :icon="Edit"
              data-test="edit-wiki-btn"
              @click="startEdit"
            >
              编辑
            </el-button>
            <el-button
              :icon="Clock"
              data-test="history-wiki-btn"
              @click="emit('open-history')"
            >
              历史版本
            </el-button>
            <el-button
              v-if="props.canEdit"
              type="danger"
              plain
              :icon="Delete"
              data-test="delete-wiki-btn"
              @click="emit('delete-page')"
            >
              删除
            </el-button>
          </template>
          <template v-else>
            <el-button :icon="Close" @click="cancelEdit">取消</el-button>
            <el-button
              type="primary"
              :icon="Check"
              :loading="props.saving"
              data-test="save-wiki-btn"
              @click="handleSave"
            >
              保存并同步索引
            </el-button>
          </template>
        </div>
      </div>

      <!-- Content Area: View Mode vs Edit Mode -->
      <div class="workspace-content-body">
        <!-- VIEW MODE -->
        <div v-if="!isEditing" class="view-mode-container">
          <!-- Rendered Markdown with Wikilinks -->
          <div
            class="markdown-render-area"
            @click="onContentClick"
            v-html="renderContentWithWikilinks(props.pageDetail.page.content)"
          />

          <!-- Hypertext Dual Links Graph Panel -->
          <WikiDualLinks
            :backlinks="props.pageDetail.backlinks"
            :outgoing-links="props.pageDetail.outgoing_links"
            @select-page="emit('select-page', $event)"
            @wikilink-click="emit('wikilink-click', $event)"
          />
        </div>

        <!-- EDIT MODE -->
        <div v-else class="edit-mode-container">
          <div class="edit-notice-bar">
            <span>💡 提示：使用 <code>[[目标页面标题]]</code> 即可与目标词条建立双向语义链接关系。</span>
          </div>
          <el-input
            v-model="editContent"
            type="textarea"
            :rows="18"
            class="markdown-editor"
            placeholder="支持 Markdown 格式与 [[双向链接]] 语法..."
          />
          <div class="edit-footer-row">
            <el-input
              v-model="editComment"
              placeholder="修订备注说明 (例如: 补充架构细节与更新双链)..."
              size="small"
              class="comment-input"
            />
          </div>
        </div>
      </div>
    </template>

    <!-- Empty Workspace State -->
    <div v-else class="empty-workspace">
      <div class="empty-box">
        <span class="big-icon">🌐</span>
        <h3>选择或创建一个 Wiki 词条</h3>
        <p>基于 Markdown 的语义超文本知识底座，支持网状双链引用与语义分块</p>
        <el-button
          v-if="props.canEdit"
          type="primary"
          :icon="Plus"
          @click="emit('open-create')"
        >
          新建 Wiki 词条
        </el-button>
      </div>
    </div>
  </main>
</template>

<style scoped>
.wiki-workspace {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  background: var(--color-surface);
}

.workspace-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 20px 28px 16px;
  border-bottom: 1px solid var(--color-border);
}

.active-title {
  margin: 0;
  font-size: 24px;
  font-weight: 700;
}

.edit-title-input {
  width: 400px;
}

.page-meta-row {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 8px;
  font-size: 13px;
  color: var(--color-secondary);
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.slug-item code {
  background: var(--color-surface-2);
  padding: 2px 6px;
  border-radius: 4px;
}

.tags-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
}

.content-tag {
  font-size: 12px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  padding: 2px 8px;
  border-radius: 12px;
  font-weight: 500;
}

.tag-input {
  width: 90px;
}

.workspace-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.workspace-content-body {
  padding: 24px 28px;
  flex: 1;
}

.view-mode-container {
  display: flex;
  flex-direction: column;
  gap: 28px;
}

.markdown-render-area {
  line-height: 1.7;
  font-size: 15px;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--color-surface);
  min-height: 200px;
}

:deep(.wikilink-badge) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid rgba(59, 130, 246, 0.2);
  transition: all 0.15s ease;
}

:deep(.wikilink-badge:hover) {
  background: var(--color-primary);
  color: #fff;
  border-color: var(--color-primary);
}

.edit-mode-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.edit-notice-bar {
  padding: 8px 12px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  border-radius: 6px;
  font-size: 13px;
}

.markdown-editor {
  font-family: var(--font-mono);
  font-size: 14px;
}

.edit-footer-row {
  display: flex;
  gap: 12px;
}

.comment-input {
  max-width: 400px;
}

.empty-workspace {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
}

.empty-box {
  text-align: center;
  max-width: 380px;
  color: var(--color-secondary);
}

.big-icon {
  font-size: 48px;
  display: block;
  margin-bottom: 12px;
}

.empty-box h3 {
  margin: 0 0 6px;
  color: var(--color-foreground);
}

.empty-box p {
  font-size: 13px;
  margin: 0 0 16px;
}
</style>
