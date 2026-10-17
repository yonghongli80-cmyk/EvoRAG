<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../utils/api'

interface KnowledgeBase {
  kb_id: string
  name: string
  doc_count: number
  kb_type: 'company_public' | 'organization' | 'sensitive' | 'unassigned'
  org_unit: { code: string; name: string } | null
  effective_role: 'read' | 'manage' | 'administer'
  sensitivity: string | null
}

interface OrgUnit {
  code: string
  name: string
  kind: string
}

const router = useRouter()
const knowledgeBases = ref<KnowledgeBase[]>([])
const manageableUnits = ref<OrgUnit[]>([])
const loading = ref(false)
const showCreate = ref(false)
const newName = ref('')
const newOrgCode = ref('')
const searchQuery = ref('')

const sections = computed(() => [
  { key: 'company_public', title: '公司公共库', icon: '🏛️', items: filteredKbs.value.filter(k => k.kb_type === 'company_public') },
  { key: 'organization', title: '组织知识库', icon: '👥', items: filteredKbs.value.filter(k => k.kb_type === 'organization') },
  { key: 'sensitive', title: '敏感知识库', icon: '🔒', items: filteredKbs.value.filter(k => k.kb_type === 'sensitive') },
].filter(s => s.items.length))

const filteredKbs = computed(() => {
  if (!searchQuery.value.trim()) return knowledgeBases.value
  const q = searchQuery.value.toLowerCase()
  return knowledgeBases.value.filter(k =>
    k.name.toLowerCase().includes(q) || (k.org_unit?.name || '').toLowerCase().includes(q)
  )
})

async function load() {
  loading.value = true
  try {
    const [k, o] = await Promise.all([
      api.get('/knowledge-bases'),
      api.get('/organization/tree').catch(() => ({ data: [] }))
    ])
    knowledgeBases.value = k.data
    manageableUnits.value = o.data
  } catch {
    ElMessage.error('加载知识库列表失败')
  } finally {
    loading.value = false
  }
}

async function createKB() {
  if (!newName.value.trim() || !newOrgCode.value) return ElMessage.warning('请填写名称并选择所属组织')
  await api.post('/knowledge-bases', { name: newName.value.trim(), org_unit_code: newOrgCode.value })
  showCreate.value = false
  newName.value = ''
  newOrgCode.value = ''
  await load()
}

async function deleteKB(kb: KnowledgeBase) {
  await ElMessageBox.confirm(`确定删除知识库「${kb.name}」吗？删除后内容不可恢复。`, '删除知识库', { type: 'warning' })
  await api.delete(`/knowledge-bases/${kb.kb_id}`)
  ElMessage.success('知识库已删除')
  await load()
}

onMounted(load)
</script>

<template>
  <div class="kb-page">
    <!-- Top Header -->
    <header class="kb-header">
      <div class="header-left">
        <h1 class="page-title">知识库</h1>
        <p class="page-desc">按组织权限浏览、维护和检索各部门权威资料与结构化知识</p>
      </div>

      <div class="header-actions">
        <el-button plain class="header-action-btn" @click="router.push('/wiki')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right: 6px;">
            <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/>
          </svg>
          Wiki 双链知识库
        </el-button>
        <el-button
          v-if="manageableUnits.length"
          type="primary"
          data-test="create-kb"
          class="create-kb-btn"
          @click="showCreate = true"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="margin-right: 4px;">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          创建知识库
        </el-button>
      </div>
    </header>

    <!-- Loading State -->
    <div v-if="loading" class="empty">
      <div class="loading-spinner" />
      <p>正在载入企业知识库...</p>
    </div>

    <!-- Main Content Sections -->
    <template v-else-if="sections.length">
      <section v-for="section in sections" :key="section.key" class="kb-section">
        <div class="section-header">
          <h2 class="section-title">{{ section.title }}</h2>
          <span class="section-count">{{ section.items.length }} 个库</span>
        </div>

        <div class="kb-grid">
          <article
            v-for="kb in section.items"
            :key="kb.kb_id"
            class="kb-card"
            @click="router.push(`/kb/${kb.kb_id}`)"
          >
            <div class="kb-card-top">
              <div class="kb-icon-box">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                  <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                </svg>
              </div>
              <div class="kb-card-titles">
                <h3 class="kb-name" :title="kb.name">{{ kb.name }}</h3>
                <span class="kb-org">{{ kb.org_unit?.name || '待归属组织' }}</span>
              </div>

              <button
                v-if="kb.effective_role === 'administer'"
                class="delete"
                type="button"
                title="删除知识库"
                @click.stop="deleteKB(kb)"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
              </button>
            </div>

            <div class="kb-card-meta">
              <span class="doc-count">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                {{ kb.doc_count }} 个文档
              </span>
              <span :class="['role-pill', kb.effective_role]">
                {{ kb.effective_role === 'read' ? '可查看' : kb.effective_role === 'manage' ? '可管理' : '可管理全部' }}
              </span>
            </div>
          </article>
        </div>
      </section>
    </template>

    <!-- Empty State -->
    <div v-else class="empty">
      <div class="empty-icon-wrap">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
      </div>
      <h2>暂无可访问的知识库</h2>
      <p>请联系组织部门负责人或管理员分配授权权限</p>
    </div>

    <!-- Create Dialog -->
    <el-dialog v-model="showCreate" title="新建组织知识库" width="460px" destroy-on-close>
      <div class="dialog-body">
        <label class="field-label">知识库名称</label>
        <el-input v-model="newName" placeholder="例如：2026 技术研发架构库" />

        <label class="field-label" style="margin-top: 14px;">所属组织部门</label>
        <el-select v-model="newOrgCode" placeholder="请选择归属组织单元" style="width: 100%;">
          <el-option v-for="unit in manageableUnits" :key="unit.code" :label="unit.name" :value="unit.code" />
        </el-select>
      </div>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" @click="createKB">确认创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.kb-page {
  padding: 28px 36px;
  max-width: 1360px;
  margin: 0 auto;
  overflow-y: auto;
  height: 100%;
}

/* ── Header ───────────────────────────────────────────── */
.kb-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--color-border);
}

.page-title {
  font-family: var(--font-heading);
  font-size: 24px;
  font-weight: 700;
  color: var(--color-foreground);
  margin: 0;
  letter-spacing: -0.5px;
}

.page-desc {
  font-size: 13.5px;
  color: var(--color-secondary);
  margin-top: 6px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* ── Section & Grid ───────────────────────────────────── */
.kb-section {
  margin-bottom: 32px;
}

.section-header {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 14px;
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-foreground);
  margin: 0;
}

.section-count {
  font-size: 12px;
  color: var(--color-muted);
}

.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(310px, 1fr));
  gap: 16px;
}

/* ── KB Card (Modern Elevation & Lift) ────────────────── */
.kb-card {
  position: relative;
  padding: 18px 20px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-card);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.kb-card:hover {
  transform: translateY(-2px);
  border-color: rgba(99, 102, 241, 0.4);
  box-shadow: 0 10px 24px -4px rgba(15, 23, 42, 0.08), 0 0 0 1px rgba(99, 102, 241, 0.2);
}

.kb-card-top {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

.kb-icon-box {
  width: 40px;
  height: 40px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, #eef2ff 0%, #e0e7ff 100%);
  color: var(--color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  box-shadow: 0 2px 6px rgba(79, 70, 229, 0.15);
}

.kb-card-titles {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.kb-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-foreground);
  margin: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kb-org {
  font-size: 12px;
  color: var(--color-secondary);
}

.delete {
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  border: none;
  background: transparent;
  color: var(--color-muted);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all var(--duration-fast);
}

.delete:hover {
  background: var(--color-destructive-light);
  color: var(--color-destructive);
}

.kb-card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 10px;
  border-top: 1px solid var(--color-border-subtle);
  font-size: 12px;
}

.doc-count {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--color-secondary);
}

.role-pill {
  padding: 2px 8px;
  border-radius: var(--radius-full);
  font-size: 11px;
  font-weight: 600;
  background: var(--color-surface-2);
  color: var(--color-secondary);
}

.role-pill.read {
  background: var(--color-surface-2);
  color: var(--color-secondary);
}

.role-pill.manage, .role-pill.administer {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

/* ── Empty State ──────────────────────────────────────── */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 100px 20px;
  color: var(--color-secondary);
  text-align: center;
  gap: 8px;
}

.empty-icon-wrap {
  width: 56px;
  height: 56px;
  border-radius: var(--radius-full);
  background: var(--color-surface-2);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-muted);
  margin-bottom: 8px;
}

.empty h2 {
  font-size: 17px;
  font-weight: 600;
  color: var(--color-foreground);
  margin: 0;
}

.loading-spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--color-surface-3);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-bottom: 8px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.dialog-body {
  padding: 8px 0;
}

.field-label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-secondary);
  margin-bottom: 6px;
}
</style>
