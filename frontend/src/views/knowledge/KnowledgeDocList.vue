<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import {
  type KBDocument,
  getStatusType,
  getStatusLabel,
  isSpreadsheetDocument,
  parseToc
} from './helpers'

const props = defineProps<{
  documents: KBDocument[]
  loading?: boolean
  canManage?: boolean
}>()

const emit = defineEmits<{
  (e: 'open-add-dialog'): void
  (e: 'remove-doc', doc: KBDocument): void
  (e: 'open-spreadsheet', doc: { document_id: number; document_version_id: number }): void
}>()
</script>

<template>
  <div class="kb-section">
    <div class="kb-section-header">
      <h3 class="kb-section-title">文档</h3>
      <el-button v-if="props.canManage" type="primary" size="small" text @click="emit('open-add-dialog')">
        <el-icon style="margin-right: 4px;"><Plus /></el-icon>
        添加
      </el-button>
    </div>

    <div v-if="props.loading" class="kb-docs-list">
      <div v-for="i in 3" :key="i" class="skeleton-line" style="height: 32px; margin-bottom: 8px;"></div>
    </div>
    <div v-else-if="props.documents.length === 0" class="kb-docs-empty">
      <p>暂无文档</p>
    </div>
    <div v-else class="kb-docs-list">
      <div v-for="doc in props.documents" :key="doc.filename" class="kb-doc-item">
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
            <el-button
              v-if="isSpreadsheetDocument(doc)"
              text
              size="small"
              :data-test="`open-spreadsheet-${doc.document_id}-${doc.document_version_id}`"
              @click.stop="emit('open-spreadsheet', doc as any)"
            >
              打开表格
            </el-button>
            <el-button
              v-if="props.canManage"
              text
              size="small"
              title="从知识库移除"
              @click.stop="emit('remove-doc', doc)"
            >
              移除
            </el-button>
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
                <li v-for="(sub, j) in section.subsections" :key="j" class="toc-subsection">
                  {{ typeof sub === 'object' && sub !== null && 'title' in sub ? (sub as any).title : sub }}
                </li>
              </ul>
            </li>
          </ul>
          <p v-else class="toc-empty">无目录结构</p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
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

.kb-docs-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.kb-doc-item {
  border-radius: var(--radius);
  transition: background var(--duration-fast);
}

.kb-doc-item:hover {
  background: var(--color-surface-2);
}

.kb-doc-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
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

.skeleton-line {
  border-radius: var(--radius-sm);
  background: var(--color-muted);
  animation: shimmer 1.5s infinite;
  background-size: 200% 100%;
  background-image: linear-gradient(90deg, var(--color-muted) 25%, var(--color-border) 50%, var(--color-muted) 75%);
}

@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
