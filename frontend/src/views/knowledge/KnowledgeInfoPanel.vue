<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowLeft,
  Edit,
  Delete,
  Setting,
  Document,
  Upload,
} from '@element-plus/icons-vue'
import type { KBDocument } from './helpers'
import KnowledgeOverviewSection from './KnowledgeOverviewSection.vue'
import KnowledgeDocList from './KnowledgeDocList.vue'

const props = defineProps<{
  kbId: string
  kbName: string
  kbOverview: string
  documents: KBDocument[]
  loading?: boolean
  canManage?: boolean
  canAdminister?: boolean
  generatingToc?: boolean
  generatingOverview?: boolean
  savingOverview?: boolean
}>()

const collapsed = defineModel<boolean>('collapsed', { default: false })

const emit = defineEmits<{
  (e: 'go-back'): void
  (e: 'open-rename'): void
  (e: 'generate-toc'): void
  (e: 'generate-overview'): void
  (e: 'open-add-dialog'): void
  (e: 'delete-kb'): void
  (e: 'save-overview', overview: string): void
  (e: 'remove-doc', doc: KBDocument): void
  (e: 'open-spreadsheet', doc: { document_id: number; document_version_id: number }): void
}>()

const router = useRouter()
const showSettingsMenu = ref(false)

function onRenameClick() {
  showSettingsMenu.value = false
  emit('open-rename')
}

function onTocClick() {
  showSettingsMenu.value = false
  emit('generate-toc')
}

function onOverviewClick() {
  showSettingsMenu.value = false
  emit('generate-overview')
}

function onAddClick() {
  showSettingsMenu.value = false
  emit('open-add-dialog')
}

function onDeleteClick() {
  showSettingsMenu.value = false
  emit('delete-kb')
}
</script>

<template>
  <div :class="['kb-info-panel', { collapsed }]">
    <!-- Collapsed mini rail dock -->
    <div v-if="collapsed" class="kb-panel-collapsed-dock">
      <button class="expand-panel-btn" @click="collapsed = false" title="展开知识库信息面板">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
      </button>
      <span class="collapsed-count" :title="`${props.documents.length} 个文档`">{{ props.documents.length }}</span>
    </div>

    <!-- Expanded full panel -->
    <div v-else class="kb-info-content">
      <!-- Header -->
      <div class="kb-info-header">
        <el-button text size="small" @click="emit('go-back')">
          <el-icon style="margin-right: 4px;"><ArrowLeft /></el-icon>
          返回
        </el-button>
        <div class="kb-info-title-row">
          <h1 class="kb-info-title">{{ props.kbName }}</h1>
          <el-button
            size="small"
            type="primary"
            plain
            data-test="kb-wiki-link-btn"
            style="margin-left: auto; margin-right: 6px;"
            @click="router.push(`/wiki?kb_id=${props.kbId}`)"
          >
            Wiki 双链
          </el-button>
          <el-dropdown v-if="props.canManage" trigger="click" v-model:visible="showSettingsMenu">
            <el-button text size="small" class="settings-btn">
              <el-icon><Setting /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="router.push(`/wiki?kb_id=${props.kbId}`)">
                  <el-icon style="margin-right: 6px;"><Document /></el-icon>
                  Wiki 双链词条
                </el-dropdown-item>
                <el-dropdown-item @click="onRenameClick">
                  <el-icon style="margin-right: 6px;"><Edit /></el-icon>
                  编辑名称
                </el-dropdown-item>
                <el-dropdown-item @click="onTocClick" :loading="props.generatingToc" :disabled="props.generatingToc">
                  <el-icon style="margin-right: 6px;"><Document /></el-icon>
                  {{ props.generatingToc ? '生成中...' : '生成目录' }}
                </el-dropdown-item>
                <el-dropdown-item @click="onOverviewClick" :loading="props.generatingOverview" :disabled="props.generatingOverview">
                  <el-icon style="margin-right: 6px;"><Document /></el-icon>
                  {{ props.generatingOverview ? '生成中...' : '生成概述' }}
                </el-dropdown-item>
                <el-dropdown-item @click="onAddClick">
                  <el-icon style="margin-right: 6px;"><Upload /></el-icon>
                  添加文件
                </el-dropdown-item>
                <el-dropdown-item v-if="props.canAdminister" divided @click="onDeleteClick" class="danger-item">
                  <el-icon style="margin-right: 6px;"><Delete /></el-icon>
                  删除知识库
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <button class="collapse-icon-btn" @click="collapsed = true" title="折叠信息面板">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="15 18 9 12 15 6"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- Scrollable content -->
      <div class="kb-info-body">
        <!-- Overview -->
        <KnowledgeOverviewSection
          :overview="props.kbOverview"
          :loading="props.loading"
          :can-manage="props.canManage"
          :saving="props.savingOverview"
          @save="emit('save-overview', $event)"
        />

        <!-- Documents -->
        <KnowledgeDocList
          :documents="props.documents"
          :loading="props.loading"
          :can-manage="props.canManage"
          @open-add-dialog="emit('open-add-dialog')"
          @remove-doc="emit('remove-doc', $event)"
          @open-spreadsheet="emit('open-spreadsheet', $event)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.kb-info-panel {
  width: 320px;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
  transition: width var(--duration-normal) var(--ease-out);
  position: relative;
}

.kb-info-panel.collapsed {
  width: 52px;
}

.kb-panel-collapsed-dock {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 14px 6px;
  gap: 12px;
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
  transition: all var(--duration-fast) var(--ease-out);
}

.expand-panel-btn:hover {
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

.kb-info-content {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 320px;
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
  margin-left: 4px;
}

.collapse-icon-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
  border-color: var(--color-border-subtle);
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

.settings-btn {
  font-size: 18px;
  color: var(--color-secondary);
}

.settings-btn:hover {
  color: var(--color-foreground);
}

.kb-info-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5);
}

.danger-item {
  color: var(--color-destructive) !important;
}

.danger-item:hover {
  background: var(--color-destructive-light) !important;
}
</style>
