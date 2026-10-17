<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Edit,
  Delete,
  Plus,
  Clock,
  Search,
  Check,
  Close
} from '@element-plus/icons-vue'
import api from '../utils/api'
import {
  getWikiPages,
  getWikiPage,
  createWikiPage,
  updateWikiPage,
  deleteWikiPage,
  getWikiHistory,
  rollbackWikiPage,
  getWikiRevisionDiff,
  type WikiPage,
  type WikiPageDetail,
  type WikiRevision,
  type WikiRevisionDiff
} from '../api/wiki'

const route = useRoute()

// Knowledge Bases
interface KBItem {
  kb_id: string
  name: string
  doc_count: number
  effective_role: string
}
const knowledgeBases = ref<KBItem[]>([])
const selectedKbId = ref<string>('')
const loadingKbs = ref(false)

// Wiki Pages List
const pages = ref<WikiPage[]>([])
const loadingPages = ref(false)
const searchQuery = ref('')
const selectedTag = ref('')
const sidebarCollapsed = ref(false)

// Selected Page Detail
const activePageId = ref<number | null>(null)
const pageDetail = ref<WikiPageDetail | null>(null)
const loadingDetail = ref(false)

// Edit State
const isEditing = ref(false)
const editTitle = ref('')
const editSlug = ref('')
const editContent = ref('')
const editTags = ref<string[]>([])
const editComment = ref('更新内容')
const tagInputVisible = ref(false)
const tagInputValue = ref('')
const saving = ref(false)

// Create Page Dialog
const showCreateDialog = ref(false)
const newTitle = ref('')
const newSlug = ref('')
const newContent = ref('# 页面标题\n\n在此输入 Markdown 内容。使用 `[[页面名称]]` 创建双向链接。')
const newTags = ref<string[]>([])
const creating = ref(false)

// Revision History Drawer & Diff
const showHistoryDrawer = ref(false)
const historyList = ref<WikiRevision[]>([])
const loadingHistory = ref(false)
const showDiffDialog = ref(false)
const currentDiff = ref<WikiRevisionDiff | null>(null)
const loadingDiff = ref(false)
const rollingBack = ref(false)

// Filtered Pages
const filteredPages = computed(() => {
  let list = pages.value
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.trim().toLowerCase()
    list = list.filter(p => p.title.toLowerCase().includes(q) || p.slug.toLowerCase().includes(q))
  }
  if (selectedTag.value) {
    list = list.filter(p => (p.tags || []).includes(selectedTag.value))
  }
  return list
})

// Unique Tags
const allTags = computed(() => {
  const set = new Set<string>()
  for (const p of pages.value) {
    if (p.tags) {
      for (const t of p.tags) set.add(t)
    }
  }
  return Array.from(set)
})

// Current KB object
const currentKb = computed(() => {
  return knowledgeBases.value.find(k => k.kb_id === selectedKbId.value) || null
})

// Can edit current KB
const canEditCurrentKb = computed(() => {
  const role = currentKb.value?.effective_role
  return role === 'manage' || role === 'administer' || !role
})

onMounted(async () => {
  await loadKnowledgeBases()
  if (route.query.kb_id && typeof route.query.kb_id === 'string') {
    selectedKbId.value = route.query.kb_id
  }
  if (selectedKbId.value) {
    await loadPages()
  }
  if (route.params.id) {
    const targetId = Number(route.params.id)
    if (!Number.isNaN(targetId) && targetId > 0) {
      await selectPage(targetId)
    }
  }
})

watch(() => selectedKbId.value, async (newKbId) => {
  if (newKbId) {
    activePageId.value = null
    pageDetail.value = null
    isEditing.value = false
    await loadPages()
  }
})

async function loadKnowledgeBases() {
  loadingKbs.value = true
  try {
    const res = await api.get<KBItem[]>('/knowledge-bases')
    knowledgeBases.value = res.data || []
    if (knowledgeBases.value.length > 0 && !selectedKbId.value) {
      selectedKbId.value = knowledgeBases.value[0].kb_id
    }
  } catch {
    ElMessage.error('加载知识库列表失败')
  } finally {
    loadingKbs.value = false
  }
}

async function loadPages() {
  if (!selectedKbId.value) return
  loadingPages.value = true
  try {
    const res = await getWikiPages({ kb_id: selectedKbId.value })
    pages.value = res || []
    // If activePageId is no longer in list, auto-select first or clear
    if (activePageId.value && !pages.value.some(p => p.id === activePageId.value)) {
      if (pages.value.length > 0) {
        await selectPage(pages.value[0].id)
      } else {
        activePageId.value = null
        pageDetail.value = null
      }
    } else if (!activePageId.value && pages.value.length > 0) {
      await selectPage(pages.value[0].id)
    }
  } catch {
    ElMessage.error('加载 Wiki 页面列表失败')
  } finally {
    loadingPages.value = false
  }
}

async function selectPage(pageId: number) {
  activePageId.value = pageId
  isEditing.value = false
  loadingDetail.value = true
  try {
    const detail = await getWikiPage(pageId)
    pageDetail.value = detail
    // Sync kb selection if different
    if (detail.page.kb_id && detail.page.kb_id !== selectedKbId.value) {
      selectedKbId.value = detail.page.kb_id
    }
  } catch {
    ElMessage.error('加载页面详情失败')
    pageDetail.value = null
  } finally {
    loadingDetail.value = false
  }
}

function startEdit() {
  if (!pageDetail.value) return
  const p = pageDetail.value.page
  editTitle.value = p.title
  editSlug.value = p.slug
  editContent.value = p.content
  editTags.value = [...(p.tags || [])]
  editComment.value = '更新内容'
  isEditing.value = true
}

function cancelEdit() {
  isEditing.value = false
}

async function saveEdit() {
  if (!pageDetail.value || !activePageId.value) return
  const title = editTitle.value.trim()
  if (!title) return ElMessage.warning('请输入标题')
  saving.value = true
  try {
    await updateWikiPage(activePageId.value, {
      title,
      content: editContent.value,
      tags: editTags.value,
      comment: editComment.value.trim() || '更新页面'
    })
    ElMessage.success('保存成功，分块向量索引已同步')
    isEditing.value = false
    await loadPages()
    await selectPage(activePageId.value)
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '更新 Wiki 页面失败')
  } finally {
    saving.value = false
  }
}

async function handleDeletePage() {
  if (!pageDetail.value || !activePageId.value) return
  try {
    await ElMessageBox.confirm(
      `确定删除 Wiki 页面「${pageDetail.value.page.title}」吗？关联的双向链接与分块索引将被移除。`,
      '删除 Wiki 页面',
      { type: 'warning' }
    )
    await deleteWikiPage(activePageId.value)
    ElMessage.success('已删除 Wiki 页面')
    activePageId.value = null
    pageDetail.value = null
    await loadPages()
  } catch {
    // cancelled
  }
}

function openCreateDialog(defaultTitle = '') {
  newTitle.value = defaultTitle
  newSlug.value = defaultTitle ? defaultTitle.toLowerCase().replace(/\s+/g, '-') : ''
  newContent.value = `# ${defaultTitle || '新页面'}\n\n在此输入 Markdown 内容。使用 \`[[页面名称]]\` 创建双向链接。`
  newTags.value = []
  showCreateDialog.value = true
}

async function handleCreatePage() {
  const title = newTitle.value.trim()
  if (!title) return ElMessage.warning('请输入词条标题')
  if (!selectedKbId.value) return ElMessage.warning('请先选择知识库')

  creating.value = true
  try {
    const res = await createWikiPage({
      kb_id: selectedKbId.value,
      title,
      slug: newSlug.value.trim() || undefined,
      content: newContent.value,
      tags: newTags.value
    })
    ElMessage.success(`创建成功，已索引 ${res.chunks_indexed} 个语义分块`)
    showCreateDialog.value = false
    await loadPages()
    await selectPage(res.page.id)
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '创建 Wiki 页面失败')
  } finally {
    creating.value = false
  }
}

async function openHistory() {
  if (!activePageId.value) return
  showHistoryDrawer.value = true
  loadingHistory.value = true
  try {
    historyList.value = await getWikiHistory(activePageId.value)
  } catch {
    ElMessage.error('加载历史版本失败')
  } finally {
    loadingHistory.value = false
  }
}

async function viewDiff(revisionNum: number) {
  if (!activePageId.value) return
  loadingDiff.value = true
  showDiffDialog.value = true
  try {
    currentDiff.value = await getWikiRevisionDiff(activePageId.value, revisionNum)
  } catch {
    ElMessage.error('获取版本 Diff 失败')
  } finally {
    loadingDiff.value = false
  }
}

async function handleRollback(revisionNum: number) {
  if (!activePageId.value) return
  try {
    await ElMessageBox.confirm(
      `确定回滚到版本 #${revisionNum} 吗？这将生成新的修订记录并即时同步索引。`,
      '确认回滚',
      { type: 'warning' }
    )
    rollingBack.value = true
    await rollbackWikiPage(activePageId.value, revisionNum)
    ElMessage.success(`已成功回滚至版本 #${revisionNum}`)
    showHistoryDrawer.value = false
    await loadPages()
    await selectPage(activePageId.value)
  } catch {
    // cancelled
  } finally {
    rollingBack.value = false
  }
}

function handleWikilinkClick(targetTitle: string) {
  if (!pageDetail.value) return
  // Search if target title matches any existing page in current KB
  const match = pages.value.find(p => p.title.toLowerCase() === targetTitle.toLowerCase())
  if (match) {
    selectPage(match.id)
  } else {
    // Check if target title exists in outgoing links
    const out = pageDetail.value.outgoing_links.find(
      o => o.target_title.toLowerCase() === targetTitle.toLowerCase()
    )
    if (out && out.target_page_id) {
      selectPage(out.target_page_id)
    } else {
      ElMessageBox.confirm(
        `未找到词条「${targetTitle}」，是否现在创建该 Wiki 词条？`,
        '创建词条',
        { confirmButtonText: '立即创建', cancelButtonText: '取消', type: 'info' }
      ).then(() => {
        openCreateDialog(targetTitle)
      }).catch(() => {})
    }
  }
}

function formatTime(timestamp: number | string): string {
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

// Convert wikilink syntax [[Target]] into clickable tags
function renderContentWithWikilinks(content: string): string {
  if (!content) return ''
  // Replace [[Target]] or [[Target|Display]]
  const escaped = content
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')

  return escaped.replace(/\[\[(.*?)\]\]/g, (_match, p1) => {
    const parts = p1.split('|')
    const target = parts[0].trim()
    const label = parts[1] ? parts[1].trim() : target
    return `<span class="wikilink-badge" data-target="${target}">🔗 ${label}</span>`
  })
}

function onContentClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  const badge = target.closest('.wikilink-badge') as HTMLElement
  if (badge) {
    const targetTitle = badge.getAttribute('data-target')
    if (targetTitle) {
      handleWikilinkClick(targetTitle)
    }
  }
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
</script>

<template>
  <div class="wiki-view">
    <!-- Header -->
    <header class="wiki-header">
      <div class="header-left">
        <h1 class="page-title">超文本 Wiki (Hypertext Wiki)</h1>
        <p class="page-subtitle">语义 Markdown 编辑、[[双向链接]] 关联图谱与增量修订秒级回滚</p>
      </div>

      <div class="header-right">
        <!-- KB Selector -->
        <div class="kb-selector-wrap">
          <span class="label">当前知识库:</span>
          <el-select
            v-model="selectedKbId"
            placeholder="选择知识库"
            size="default"
            class="kb-select"
            :loading="loadingKbs"
          >
            <el-option
              v-for="kb in knowledgeBases"
              :key="kb.kb_id"
              :label="kb.name"
              :value="kb.kb_id"
            />
          </el-select>
        </div>

        <el-button
          type="primary"
          :icon="Plus"
          data-test="create-wiki-btn"
          :disabled="!canEditCurrentKb || !selectedKbId"
          @click="openCreateDialog()"
        >
          新建 Wiki 词条
        </el-button>
      </div>
    </header>

    <!-- Main Body Layout -->
    <div class="wiki-body">
      <!-- Left Sidebar: Page List & Search -->
      <aside :class="['wiki-sidebar', { collapsed: sidebarCollapsed }]">
        <!-- Collapsed dock -->
        <div v-if="sidebarCollapsed" class="wiki-sidebar-collapsed-dock">
          <button class="expand-rail-btn" @click="sidebarCollapsed = false" title="展开词条列表">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
            </svg>
          </button>
          <span class="collapsed-count">{{ filteredPages.length }}</span>
        </div>

        <!-- Expanded content -->
        <div v-else class="wiki-sidebar-content">
          <div class="sidebar-search">
            <el-input
              v-model="searchQuery"
              placeholder="检索 Wiki 词条..."
              :prefix-icon="Search"
              clearable
              size="small"
            />
            <button class="collapse-icon-btn" @click="sidebarCollapsed = true" title="折叠词条列表">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="15 18 9 12 15 6"/>
              </svg>
            </button>
          </div>

          <!-- Tag filter pills -->
          <div v-if="allTags.length > 0" class="tags-filter-bar">
            <span
              :class="['tag-pill', { active: selectedTag === '' }]"
              @click="selectedTag = ''"
            >
              全部
            </span>
            <span
              v-for="tag in allTags"
              :key="tag"
              :class="['tag-pill', { active: selectedTag === tag }]"
              @click="selectedTag = selectedTag === tag ? '' : tag"
            >
              #{{ tag }}
            </span>
          </div>

          <!-- Pages List -->
          <div v-loading="loadingPages" class="pages-list">
            <div
              v-for="p in filteredPages"
              :key="p.id"
              :class="['page-item', { active: activePageId === p.id }]"
              @click="selectPage(p.id)"
            >
              <div class="item-title-row">
                <span class="doc-icon">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                    <polyline points="14 2 14 8 20 8"/>
                  </svg>
                </span>
                <span class="item-title" :title="p.title">{{ p.title }}</span>
              </div>
              <div class="item-meta-row">
                <span class="item-time">{{ formatTime(p.updated_at) }}</span>
                <div v-if="p.tags && p.tags.length > 0" class="item-tags">
                  <span v-for="t in p.tags.slice(0, 2)" :key="t" class="mini-tag">#{{ t }}</span>
                </div>
              </div>
            </div>

            <div v-if="!loadingPages && filteredPages.length === 0" class="empty-list">
              <span class="empty-icon">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                </svg>
              </span>
              <p>{{ searchQuery ? '未匹配到相关词条' : '暂无 Wiki 词条' }}</p>
              <el-button
                v-if="!searchQuery && canEditCurrentKb"
                size="small"
                type="primary"
                link
                @click="openCreateDialog()"
              >
                点击创建第一个词条
              </el-button>
            </div>
          </div>
        </div>
      </aside>

      <!-- Right Main Workspace -->
      <main v-loading="loadingDetail" class="wiki-workspace">
        <template v-if="pageDetail">
          <!-- View / Edit Top Bar -->
          <div class="workspace-header">
            <div class="title-meta-block">
              <div class="page-title-row">
                <h2 v-if="!isEditing" class="active-title">{{ pageDetail.page.title }}</h2>
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
                  更新于 {{ formatTime(pageDetail.page.updated_at) }}
                </span>
                <span class="meta-item">
                  版本: <strong>#{{ pageDetail.revision_count }}</strong>
                </span>
                <span v-if="pageDetail.page.slug" class="meta-item slug-item">
                  Slug: <code>{{ pageDetail.page.slug }}</code>
                </span>
              </div>

              <!-- Tags display or edit -->
              <div class="tags-row">
                <template v-if="!isEditing">
                  <span v-for="t in pageDetail.page.tags || []" :key="t" class="content-tag">
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
                  v-if="canEditCurrentKb"
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
                  @click="openHistory"
                >
                  历史版本
                </el-button>
                <el-button
                  v-if="canEditCurrentKb"
                  type="danger"
                  plain
                  :icon="Delete"
                  data-test="delete-wiki-btn"
                  @click="handleDeletePage"
                >
                  删除
                </el-button>
              </template>
              <template v-else>
                <el-button :icon="Close" @click="cancelEdit">取消</el-button>
                <el-button
                  type="primary"
                  :icon="Check"
                  :loading="saving"
                  data-test="save-wiki-btn"
                  @click="saveEdit"
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
                v-html="renderContentWithWikilinks(pageDetail.page.content)"
              />

              <!-- Hypertext Dual Links Graph Panel -->
              <div class="dual-links-panel">
                <!-- Backlinks Card -->
                <div class="links-card">
                  <div class="links-card-title">
                    <span class="icon">↶</span>
                    <strong>反向链接 (Backlinks)</strong>
                    <span class="badge">{{ pageDetail.backlinks.length }}</span>
                  </div>
                  <p class="links-desc">引用了本页面的其他 Wiki 词条：</p>
                  <div v-if="pageDetail.backlinks.length > 0" class="links-list">
                    <button
                      v-for="b in pageDetail.backlinks"
                      :key="b.id"
                      class="link-item"
                      type="button"
                      @click="selectPage(b.id)"
                    >
                      <span class="link-icon">📄</span>
                      <span class="link-title">{{ b.title }}</span>
                      <span class="arrow">→</span>
                    </button>
                  </div>
                  <div v-else class="empty-links">暂无其他页面引用此词条</div>
                </div>

                <!-- Outgoing Links Card -->
                <div class="links-card">
                  <div class="links-card-title">
                    <span class="icon">↷</span>
                    <strong>出链引用 (Outgoing Links)</strong>
                    <span class="badge">{{ pageDetail.outgoing_links.length }}</span>
                  </div>
                  <p class="links-desc">本页面中引用的外部 Wiki 链接：</p>
                  <div v-if="pageDetail.outgoing_links.length > 0" class="links-list">
                    <button
                      v-for="out in pageDetail.outgoing_links"
                      :key="out.target_title"
                      class="link-item"
                      type="button"
                      @click="handleWikilinkClick(out.target_title)"
                    >
                      <span class="link-icon">🔗</span>
                      <span class="link-title">{{ out.target_title }}</span>
                      <el-tag
                        size="small"
                        :type="out.target_page_id ? 'success' : 'info'"
                        class="status-tag"
                      >
                        {{ out.target_page_id ? '已关联' : '待创建' }}
                      </el-tag>
                    </button>
                  </div>
                  <div v-else class="empty-links">本页暂无引出双链语法</div>
                </div>
              </div>
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
              v-if="canEditCurrentKb && selectedKbId"
              type="primary"
              :icon="Plus"
              @click="openCreateDialog()"
            >
              新建 Wiki 词条
            </el-button>
          </div>
        </div>
      </main>
    </div>

    <!-- Create Page Dialog -->
    <el-dialog
      v-model="showCreateDialog"
      title="新建 Wiki 词条"
      width="640px"
      destroy-on-close
    >
      <div class="dialog-form">
        <div class="form-item">
          <label class="form-label">词条标题 *</label>
          <el-input v-model="newTitle" placeholder="例如: 微服务架构设计规范" />
        </div>
        <div class="form-item">
          <label class="form-label">自定义 Slug (英文别名，可选)</label>
          <el-input v-model="newSlug" placeholder="例如: microservices-spec" />
        </div>
        <div class="form-item">
          <label class="form-label">Markdown 正文内容 *</label>
          <el-input
            v-model="newContent"
            type="textarea"
            :rows="10"
            placeholder="使用 [[其他词条名称]] 创建双链引用..."
          />
        </div>
      </div>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="creating"
          data-test="confirm-create-wiki-btn"
          @click="handleCreatePage"
        >
          创建并同步索引
        </el-button>
      </template>
    </el-dialog>

    <!-- Revision History Drawer -->
    <el-drawer
      v-model="showHistoryDrawer"
      title="版本修订历史 (Revision History)"
      size="480px"
    >
      <div v-loading="loadingHistory" class="history-container">
        <div v-if="historyList.length === 0" class="empty-history">
          暂无历史修订记录
        </div>
        <div v-else class="history-timeline">
          <div
            v-for="rev in historyList"
            :key="rev.id"
            class="history-card"
          >
            <div class="rev-header">
              <span class="rev-badge">#{{ rev.revision_num }}</span>
              <span class="rev-author">{{ rev.author_id || 'system' }}</span>
              <span class="rev-time">{{ formatTime(rev.created_at) }}</span>
            </div>
            <div class="rev-comment">{{ rev.comment || '无提交备注' }}</div>
            <div class="rev-actions">
              <el-button
                size="small"
                link
                type="primary"
                @click="viewDiff(rev.revision_num)"
              >
                查看增量 Diff
              </el-button>
              <el-button
                v-if="canEditCurrentKb"
                size="small"
                type="warning"
                plain
                :loading="rollingBack"
                @click="handleRollback(rev.revision_num)"
              >
                回滚至此版本
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </el-drawer>

    <!-- Revision Diff Dialog -->
    <el-dialog
      v-model="showDiffDialog"
      title="版本变更 Diff 详情"
      width="700px"
    >
      <div v-loading="loadingDiff" class="diff-dialog-body">
        <template v-if="currentDiff">
          <div class="diff-meta">
            <span>版本号: #{{ currentDiff.revision_num }}</span>
            <span>提交备注: {{ currentDiff.comment }}</span>
            <span>生成时间: {{ formatTime(currentDiff.created_at) }}</span>
          </div>
          <div class="diff-content-box">
            <pre class="diff-pre">{{ currentDiff.delta || '此版本为初始版本或无增量差异' }}</pre>
          </div>
        </template>
      </div>
      <template #footer>
        <el-button @click="showDiffDialog = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.wiki-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--color-background);
  color: var(--color-foreground);
}

.wiki-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
}

.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.page-subtitle {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--color-secondary);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.kb-selector-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
}

.kb-selector-wrap .label {
  font-size: 13px;
  color: var(--color-secondary);
}

.kb-select {
  width: 180px;
}

.wiki-body {
  display: flex;
  flex: 1;
  overflow: hidden;
}

/* Sidebar */
.wiki-sidebar {
  width: 300px;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: width var(--duration-normal) var(--ease-out);
  position: relative;
  overflow: hidden;
}

.wiki-sidebar.collapsed {
  width: 52px;
}

.wiki-sidebar-collapsed-dock {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 14px 6px;
  gap: 12px;
  height: 100%;
}

.expand-rail-btn {
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
  transition: all var(--duration-fast) var(--ease-out);
}

.expand-rail-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-primary);
  border-color: var(--color-primary-light);
}

.collapsed-count {
  font-size: 11px;
  font-weight: 700;
  color: var(--color-muted);
  background: var(--color-surface-2);
  padding: 2px 6px;
  border-radius: var(--radius-full);
}

.wiki-sidebar-content {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 300px;
}

.sidebar-search {
  padding: 12px;
  border-bottom: 1px solid var(--color-border);
  display: flex;
  align-items: center;
  gap: 8px;
}

.collapse-icon-btn {
  width: 24px;
  height: 24px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--color-muted);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all var(--duration-fast);
  padding: 0;
  flex-shrink: 0;
}

.collapse-icon-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
  border-color: var(--color-border-subtle);
}

.tags-filter-bar {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  padding: 8px 12px;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface-2);
}

.tag-pill {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  cursor: pointer;
  white-space: nowrap;
}

.tag-pill.active {
  background: var(--color-primary);
  color: #fff;
  border-color: var(--color-primary);
}

.pages-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.page-item {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  transition: all 0.15s ease;
  border: 1px solid transparent;
}

.page-item:hover {
  background: var(--color-surface-2);
}

.page-item.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.item-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.doc-icon {
  font-size: 14px;
}

.item-title {
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item-meta-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-secondary);
}

.item-tags {
  display: flex;
  gap: 4px;
}

.mini-tag {
  background: var(--color-surface-3);
  padding: 1px 4px;
  border-radius: 4px;
}

.empty-list {
  text-align: center;
  padding: 40px 16px;
  color: var(--color-secondary);
}

.empty-icon {
  font-size: 32px;
  display: block;
  margin-bottom: 8px;
}

/* Workspace */
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

/* View Mode */
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

/* Dual Links Graph Panel */
.dual-links-panel {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  padding-top: 20px;
  border-top: 1px solid var(--color-border);
}

.links-card {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 16px;
  background: var(--color-surface-2);
}

.links-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
}

.links-card-title .icon {
  font-size: 16px;
}

.links-card-title .badge {
  background: var(--color-primary);
  color: #fff;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 10px;
}

.links-desc {
  font-size: 12px;
  color: var(--color-secondary);
  margin: 6px 0 12px;
}

.links-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.link-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  cursor: pointer;
  text-align: left;
  transition: all 0.15s ease;
}

.link-item:hover {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.link-icon {
  margin-right: 8px;
}

.link-title {
  flex: 1;
  font-size: 13px;
  font-weight: 500;
}

.arrow {
  color: var(--color-secondary);
  font-size: 13px;
}

.status-tag {
  margin-left: 8px;
}

.empty-links {
  font-size: 13px;
  color: var(--color-muted);
  text-align: center;
  padding: 16px 0;
}

/* Edit Mode */
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

/* Empty Workspace */
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

/* Dialog Form */
.dialog-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-label {
  font-size: 13px;
  font-weight: 500;
}

/* History Drawer */
.history-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.history-card {
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface-2);
  margin-bottom: 10px;
}

.rev-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rev-badge {
  background: var(--color-primary);
  color: #fff;
  font-size: 12px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
}

.rev-author {
  font-size: 13px;
  font-weight: 500;
}

.rev-time {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-secondary);
}

.rev-comment {
  font-size: 13px;
  margin: 8px 0;
  color: var(--color-foreground);
}

.rev-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.diff-meta {
  display: flex;
  gap: 16px;
  font-size: 13px;
  margin-bottom: 12px;
  color: var(--color-secondary);
}

.diff-content-box {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px;
  border-radius: 6px;
  max-height: 400px;
  overflow: auto;
}

.diff-pre {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.5;
}
</style>
