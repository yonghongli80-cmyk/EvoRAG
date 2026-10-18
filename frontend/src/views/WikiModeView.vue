<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
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
import type { KBItem } from './wiki/helpers'
import WikiHeader from './wiki/WikiHeader.vue'
import WikiSidebar from './wiki/WikiSidebar.vue'
import WikiEditor from './wiki/WikiEditor.vue'
import WikiCreateDialog from './wiki/WikiCreateDialog.vue'
import WikiHistoryDrawer from './wiki/WikiHistoryDrawer.vue'
import WikiDiffDialog from './wiki/WikiDiffDialog.vue'

const route = useRoute()

// Knowledge Bases
const knowledgeBases = ref<KBItem[]>([])
const selectedKbId = ref<string>('')
const loadingKbs = ref(false)

// Wiki Pages List
const pages = ref<WikiPage[]>([])
const loadingPages = ref(false)
const sidebarCollapsed = ref(false)

// Selected Page Detail
const activePageId = ref<number | null>(null)
const pageDetail = ref<WikiPageDetail | null>(null)
const loadingDetail = ref(false)

// Edit State
const isEditing = ref(false)
const saving = ref(false)

// Create Page Dialog
const showCreateDialog = ref(false)
const createDefaultTitle = ref('')
const creating = ref(false)

// Revision History Drawer & Diff
const showHistoryDrawer = ref(false)
const historyList = ref<WikiRevision[]>([])
const loadingHistory = ref(false)
const showDiffDialog = ref(false)
const currentDiff = ref<WikiRevisionDiff | null>(null)
const loadingDiff = ref(false)
const rollingBack = ref(false)

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

async function saveEdit(payload: { title: string; slug?: string; content: string; tags: string[]; comment: string }) {
  if (!pageDetail.value || !activePageId.value) return
  saving.value = true
  try {
    await updateWikiPage(activePageId.value, {
      title: payload.title,
      content: payload.content,
      tags: payload.tags,
      comment: payload.comment
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
  createDefaultTitle.value = defaultTitle
  showCreateDialog.value = true
}

async function handleCreatePage(payload: { title: string; slug?: string; content: string; tags: string[] }) {
  if (!selectedKbId.value) return ElMessage.warning('请先选择知识库')

  creating.value = true
  try {
    const res = await createWikiPage({
      kb_id: selectedKbId.value,
      title: payload.title,
      slug: payload.slug,
      content: payload.content,
      tags: payload.tags
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
</script>

<template>
  <div class="wiki-view">
    <!-- Header -->
    <WikiHeader
      v-model:selected-kb-id="selectedKbId"
      :knowledge-bases="knowledgeBases"
      :loading-kbs="loadingKbs"
      :can-edit="canEditCurrentKb"
      @open-create="openCreateDialog()"
    />

    <!-- Main Body Layout -->
    <div class="wiki-body">
      <!-- Left Sidebar: Page List & Search -->
      <WikiSidebar
        v-model:collapsed="sidebarCollapsed"
        :pages="pages"
        :active-page-id="activePageId"
        :loading="loadingPages"
        :can-edit="canEditCurrentKb"
        @select-page="selectPage"
        @open-create="openCreateDialog"
      />

      <!-- Right Main Workspace -->
      <WikiEditor
        v-model:is-editing="isEditing"
        :page-detail="pageDetail"
        :loading="loadingDetail"
        :can-edit="canEditCurrentKb"
        :saving="saving"
        @save-edit="saveEdit"
        @delete-page="handleDeletePage"
        @open-history="openHistory"
        @select-page="selectPage"
        @wikilink-click="handleWikilinkClick"
        @open-create="openCreateDialog"
      />
    </div>

    <!-- Create Page Dialog -->
    <WikiCreateDialog
      v-model:visible="showCreateDialog"
      :selected-kb-id="selectedKbId"
      :default-title="createDefaultTitle"
      :creating="creating"
      @create="handleCreatePage"
    />

    <!-- Revision History Drawer -->
    <WikiHistoryDrawer
      v-model:visible="showHistoryDrawer"
      :history-list="historyList"
      :loading="loadingHistory"
      :rolling-back="rollingBack"
      :can-edit="canEditCurrentKb"
      @view-diff="viewDiff"
      @rollback="handleRollback"
    />

    <!-- Revision Diff Dialog -->
    <WikiDiffDialog
      v-model:visible="showDiffDialog"
      :diff="currentDiff"
      :loading="loadingDiff"
    />
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

.wiki-body {
  display: flex;
  flex: 1;
  overflow: hidden;
}
</style>
