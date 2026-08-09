<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '../utils/api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Collection, Delete, Share } from '@element-plus/icons-vue'
import ShareDialog from '../components/ShareDialog.vue'

const router = useRouter()

interface SharedUser {
  username: string
  permission: string
}

interface KnowledgeBase {
  kb_id: string
  name: string
  doc_count: number
  scope: string
  is_owner: boolean
  shared_users: SharedUser[]
}

const knowledgeBases = ref<KnowledgeBase[]>([])
const loading = ref(false)

// Create dialog
const showCreateDialog = ref(false)
const newKBName = ref('')
const newKBScope = ref('private')
const creating = ref(false)

// Share dialog
const showShareDialog = ref(false)
const shareTargetId = ref('')
const shareTargetName = ref('')

onMounted(async () => {
  await loadKBs()
})

async function loadKBs() {
  loading.value = true
  try {
    const res = await api.get('/knowledge-bases')
    knowledgeBases.value = res.data
  } catch {
    ElMessage.error('加载知识库列表失败')
  } finally {
    loading.value = false
  }
}

function openCreateDialog() {
  newKBName.value = ''
  showCreateDialog.value = true
}

async function createKB() {
  const name = newKBName.value.trim()
  if (!name) return ElMessage.warning('请输入知识库名称')
  creating.value = true
  try {
    await api.post('/knowledge-bases', { name, scope: newKBScope.value })
    ElMessage.success('知识库创建成功')
    showCreateDialog.value = false
    await loadKBs()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '创建失败')
  } finally {
    creating.value = false
  }
}

async function deleteKB(kb: KnowledgeBase) {
  try {
    await ElMessageBox.confirm(
      `确定删除知识库「${kb.name}」吗？此操作不可恢复。`,
      '删除知识库',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
    await api.delete(`/knowledge-bases/${kb.kb_id}`)
    ElMessage.success('知识库已删除')
    await loadKBs()
  } catch (err: any) {
    if (err !== 'cancel' && err?.message !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

function openShareDialog(kb: KnowledgeBase) {
  shareTargetId.value = kb.kb_id
  shareTargetName.value = kb.name
  showShareDialog.value = true
}

async function selectKB(kb: KnowledgeBase) {
  router.push(`/kb/${kb.kb_id}`)
}
</script>

<template>
  <div class="kb-page">
    <!-- Header -->
    <div class="kb-page-header">
      <div>
        <h1 class="kb-page-title">知识库</h1>
        <p class="kb-page-subtitle">管理你的知识库，组织和检索文档</p>
      </div>
      <el-button type="primary" @click="openCreateDialog">
        <el-icon style="margin-right: 6px;"><Plus /></el-icon>
        创建知识库
      </el-button>
    </div>

    <!-- Loading skeleton -->
    <div v-if="loading" class="kb-grid">
      <div v-for="i in 3" :key="i" class="kb-card kb-card--skeleton">
        <div class="skeleton-header">
          <div class="skeleton-icon"></div>
          <div class="skeleton-text">
            <div class="skeleton-line skeleton-line--title"></div>
            <div class="skeleton-line skeleton-line--sub"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Card grid -->
    <div v-else-if="knowledgeBases.length" class="kb-grid">
      <div
        v-for="kb in knowledgeBases"
        :key="kb.kb_id"
        class="kb-card"
        @click="selectKB(kb)"
      >
        <div class="kb-card-body">
          <div class="kb-icon">
            <el-icon><Collection /></el-icon>
          </div>
          <div class="kb-card-info">
            <div class="kb-name">
              {{ kb.name }}
              <span :class="['scope-tag', kb.scope]">
                {{ kb.scope === 'private' ? '私有' : kb.scope === 'shared' ? '共享' : '公开' }}
              </span>
            </div>
            <div class="kb-meta">
              {{ kb.doc_count }} 个文档
              <span v-if="kb.scope === 'shared' && kb.shared_users?.length" class="shared-hint">
                · 共享给 {{ kb.shared_users.map(u => u.username).join('、') }}
              </span>
            </div>
          </div>
        </div>
        <div class="kb-card-footer">
          <div class="kb-stat">
            <span class="kb-stat-value">{{ kb.doc_count }}</span>
            <span class="kb-stat-label">文档</span>
          </div>
          <div class="kb-card-actions">
            <el-button
              v-if="kb.is_owner"
              type="primary"
              text
              size="small"
              @click.stop="openShareDialog(kb)"
            >
              <el-icon><Share /></el-icon>
            </el-button>
            <el-button
              type="danger"
              text
              size="small"
              @click.stop="deleteKB(kb)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- Empty state -->
    <div v-else class="kb-empty">
      <div class="kb-empty-icon">
        <el-icon><Collection /></el-icon>
      </div>
      <h3 class="kb-empty-title">暂无知识库</h3>
      <p class="kb-empty-desc">创建你的第一个知识库，开始组织文档</p>
      <el-button type="primary" @click="openCreateDialog">
        <el-icon style="margin-right: 6px;"><Plus /></el-icon>
        创建知识库
      </el-button>
    </div>

    <!-- Create dialog -->
    <el-dialog
      v-model="showCreateDialog"
      title="创建知识库"
      width="420px"
      :close-on-click-modal="false"
      @closed="newKBName = ''"
    >
      <div class="create-dialog-body">
        <label class="create-label">知识库名称</label>
        <el-input
          v-model="newKBName"
          placeholder="输入知识库名称"
          maxlength="100"
          @keyup.enter="createKB"
        />
        <label class="create-label" style="margin-top: 16px;">可见范围</label>
        <el-radio-group v-model="newKBScope">
          <el-radio value="private">私有（仅自己可见）</el-radio>
          <el-radio value="public">公开（所有人可见）</el-radio>
        </el-radio-group>
      </div>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="createKB">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- Share dialog -->
    <ShareDialog
      v-model:visible="showShareDialog"
      :item-id="shareTargetId"
      :item-name="shareTargetName"
      item-type="kb"
    />
  </div>
</template>

<style scoped>
.kb-page {
  padding: var(--space-6) var(--space-8);
  height: 100%;
  overflow-y: auto;
}

/* ── Header ── */
.kb-page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: var(--space-8);
}

.kb-page-title {
  font-family: var(--font-heading);
  font-size: var(--text-2xl);
  font-weight: var(--font-bold);
  color: var(--color-foreground);
  line-height: var(--leading-tight);
}

.kb-page-subtitle {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  margin-top: var(--space-1);
}

/* ── Grid ── */
.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--space-5);
}

/* ── Card ── */
.kb-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-6);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 160px;
}

.kb-card:hover {
  border-color: var(--color-primary);
  transform: translateY(-2px);
}

.kb-card:active {
  transform: translateY(0);
}

.kb-card-body {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.kb-icon {
  width: 48px;
  height: 48px;
  background: var(--color-primary);
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-on-primary);
  font-size: 24px;
  flex-shrink: 0;
}

.kb-card-info {
  overflow: hidden;
}

.kb-name {
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kb-meta {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  margin-top: var(--space-1);
}

.kb-card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: var(--space-4);
  margin-top: var(--space-4);
  border-top: 1px solid var(--color-border);
}

.kb-stat {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}

.kb-stat-value {
  font-size: var(--text-2xl);
  font-weight: var(--font-bold);
  font-family: var(--font-mono);
  color: var(--color-primary);
}

.kb-stat-label {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.kb-card-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

/* ── Skeleton ── */
.kb-card--skeleton {
  cursor: default;
  pointer-events: none;
}

.kb-card--skeleton:hover {
  border-color: var(--color-border);
  box-shadow: none;
  transform: none;
}

.skeleton-header {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.skeleton-icon {
  width: 48px;
  height: 48px;
  border-radius: var(--radius);
  background: var(--color-muted);
  animation: shimmer 1.5s infinite;
  background-size: 200% 100%;
  background-image: linear-gradient(90deg, var(--color-muted) 25%, var(--color-border) 50%, var(--color-muted) 75%);
}

.skeleton-text {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.skeleton-line {
  height: 12px;
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

.skeleton-line--sub {
  width: 40%;
}

/* ── Empty state ── */
.kb-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--space-16) var(--space-8);
  text-align: center;
}

.kb-empty-icon {
  width: 80px;
  height: 80px;
  border-radius: var(--radius-xl);
  background: var(--color-accent-light);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-accent);
  font-size: 36px;
  margin-bottom: var(--space-6);
}

.kb-empty-title {
  font-family: var(--font-heading);
  font-size: var(--text-xl);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
  margin-bottom: var(--space-2);
}

.kb-empty-desc {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  margin-bottom: var(--space-6);
  max-width: 320px;
}

/* ── Create dialog ── */
.create-dialog-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.create-label {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
}

/* ── Scope tags ── */
.scope-tag {
  display: inline-block;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  margin-left: 8px;
  font-weight: var(--font-medium);
  vertical-align: middle;
}

.scope-tag.private {
  background: var(--color-destructive-light);
  color: var(--color-destructive);
}

.scope-tag.public {
  background: var(--color-success-light);
  color: var(--color-success);
}

.scope-tag.shared {
  background: var(--color-accent-light);
  color: var(--color-accent);
}

.shared-hint {
  font-size: 12px;
  color: var(--color-secondary);
  margin-left: 4px;
}

@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

/* ── Responsive ───────────────────────────────────────── */
@media (max-width: 768px) {
  .kb-page {
    padding: var(--space-4);
  }

  .kb-page-header {
    flex-direction: column;
    gap: var(--space-4);
    margin-bottom: var(--space-6);
  }

  .kb-grid {
    grid-template-columns: 1fr;
  }
}
</style>
