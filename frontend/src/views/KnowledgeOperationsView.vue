<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import {
  fetchMyFeedback,
  fetchMyTasks,
  fetchOperationsSummary,
  transitionTask
} from '../api/knowledgeOperations'
import type { KnowledgeTask, TaskSummary } from '../api/knowledgeOperations'

const authStore = useAuthStore()
const activeTab = ref<'feedback' | 'tasks' | 'overview'>('feedback')
const feedbackItems = ref<any[]>([])
const feedbackTotal = ref(0)
const tasks = ref<KnowledgeTask[]>([])
const tasksTotal = ref(0)
const summary = ref<TaskSummary | null>(null)
const loading = ref(false)
const error = ref('')

const OVERDUE_MS = 0

function isOverdue(task?: KnowledgeTask) {
  if (!task) return false
  return task.status !== 'resolved' && task.status !== 'dismissed' && task.due_at < Date.now() / 1000 - OVERDUE_MS
}

function taskTypeLabel(type: string) {
  if (type === 'outdated') return '内容过期'
  if (type === 'not_useful') return '回答无帮助'
  if (type === 'insufficient_evidence') return '证据不足'
  return '证据冲突'
}

async function loadFeedback() {
  try {
    const data = await fetchMyFeedback(1, 50)
    feedbackItems.value = data.items
    feedbackTotal.value = data.total
  } catch {
    error.value = '加载反馈失败'
  }
}

async function loadTasks() {
  try {
    const data = await fetchMyTasks(1, 50)
    tasks.value = data.items
    tasksTotal.value = data.total
  } catch {
    error.value = '加载任务失败'
  }
}

async function loadSummary() {
  try {
    summary.value = await fetchOperationsSummary()
  } catch {
    error.value = '加载运营总览失败'
  }
}

async function resolveTask(task: KnowledgeTask) {
  try {
    await transitionTask(task.id, 'resolved', task.updated_at)
    await loadTasks()
    if (authStore.user?.is_admin) await loadSummary()
  } catch {
    error.value = '任务处理失败'
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    await loadFeedback()
    await loadTasks()
    if (authStore.user?.is_admin) await loadSummary()
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="operations" data-test="operations-view">
    <div class="page-header">
      <div class="header-badge">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
        </svg>
        <span>OPERATIONS & GOVERNANCE</span>
      </div>
      <h2>知识运营</h2>
      <p>集中查看用户反馈、处理过期与冲突知识任务，保障组织数据资产的时效性与准确率。</p>
    </div>

    <div class="operations-panel">
    <el-tabs v-model="activeTab">
      <el-tab-pane name="feedback" label="我的反馈">
        <div class="tab-content">
          <h3 class="tab-title">我的反馈</h3>
          <p class="muted" data-test="feedback-empty" v-if="!loading && feedbackItems.length === 0">
            暂无反馈
          </p>
          <el-empty v-else-if="feedbackItems.length === 0" description="暂无反馈" />
          <el-table :data="feedbackItems" v-else size="small">
            <el-table-column prop="question_text" label="问题" />
            <el-table-column prop="kind" label="类型" width="120" />
            <el-table-column prop="comment" label="说明" />
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane name="tasks" label="待我处理">
        <div class="tab-content">
          <h3 class="tab-title">待我处理</h3>
          <p class="muted" data-test="tasks-empty" v-if="!loading && tasks.length === 0">
            暂无待处理任务
          </p>
          <el-table :data="tasks" v-else size="small">
            <el-table-column label="类型" width="120">
              <template #default="scope">{{ taskTypeLabel(scope?.row?.task_type) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="scope">
                <el-tag
                  size="small"
                  :type="scope?.row?.status === 'open' ? 'warning' : 'info'"
                >
                  {{ scope?.row?.status }}
                </el-tag>
                <el-tag v-if="isOverdue(scope?.row)" size="small" type="danger">超期</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="举报数" prop="report_count" width="90" />
            <el-table-column label="举报人" width="160">
              <template #default="scope">
                {{ ((scope?.row?.reporters) || []).join('、') }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="110">
              <template #default="scope">
                <el-button
                  v-if="scope?.row?.status === 'open' || scope?.row?.status === 'in_progress'"
                  size="small"
                  data-test="resolve-task"
                  @click="resolveTask(scope?.row)"
                >
                  解决
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane
        v-if="authStore.user?.is_admin"
        name="overview"
        label="运营总览"
      >
        <div class="tab-content" v-if="summary" data-test="operations-overview">
          <h3 class="tab-title">运营总览</h3>
          <el-row :gutter="12">
            <el-col :span="8">
              <el-card>
                <div class="stat-label">未处理任务</div>
                <div class="stat-value">{{ summary.open }}</div>
              </el-card>
            </el-col>
            <el-col :span="8">
              <el-card>
                <div class="stat-label">已超期</div>
                <div class="stat-value">{{ summary.overdue }}</div>
              </el-card>
            </el-col>
            <el-col :span="8">
              <el-card>
                <div class="stat-label">内容过期</div>
                <div class="stat-value">{{ summary.outdated }}</div>
              </el-card>
            </el-col>
            <el-col :span="8">
              <el-card>
                <div class="stat-label">回答无帮助</div>
                <div class="stat-value">{{ summary.not_useful }}</div>
              </el-card>
            </el-col>
            <el-col :span="8">
              <el-card>
                <div class="stat-label">陈旧知识库</div>
                <div class="stat-value">{{ summary.stale_knowledge_bases }}</div>
              </el-card>
            </el-col>
          </el-row>
        </div>
      </el-tab-pane>
    </el-tabs>

    <p v-if="error" class="error" data-test="operations-error">{{ error }}</p>
    </div>
  </div>
</template>

<style scoped>
.operations {
  padding: var(--space-6);
  height: 100%;
  overflow: auto;
  background: var(--color-background);
}

.page-header { margin-bottom: var(--space-6); }
.header-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: var(--radius-full);
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
  margin-bottom: 8px;
}
.page-header h2 {
  margin: 0;
  font-family: var(--font-heading);
  font-size: var(--text-2xl);
  color: var(--color-foreground);
}
.page-header p { margin: var(--space-2) 0 0; color: var(--color-secondary); font-size: var(--text-sm); }

.operations-panel { min-height: 420px; padding: var(--space-6); border: 1px solid var(--color-border); border-radius: var(--radius-xl); background: var(--color-surface); box-shadow: var(--shadow-sm); }
:deep(.el-tabs__header) { margin-bottom: var(--space-5); }
:deep(.el-tabs__item) { height: 42px; color: var(--color-secondary); font-weight: var(--font-medium); }
:deep(.el-tabs__item.is-active) { color: var(--color-primary); }
:deep(.el-tabs__active-bar) { background: var(--color-primary); }

.tab-content {
  padding: var(--space-2) 0;
}

.muted {
  color: var(--color-secondary);
  min-height: 260px;
  display: grid;
  place-items: center;
  border: 1px dashed var(--color-border);
  border-radius: var(--radius);
  background: var(--color-background);
}

.tab-title {
  margin: 0 0 var(--space-3);
  font-size: var(--text-md);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
}

.stat-label {
  font-size: var(--text-sm);
  color: var(--color-secondary);
}

.stat-value {
  font-size: var(--text-2xl);
  font-weight: var(--font-bold);
  color: var(--color-foreground);
}

.error {
  margin-top: var(--space-4);
  padding: var(--space-3) var(--space-4);
  border: 1px solid #fecaca;
  border-radius: var(--radius);
  color: var(--color-destructive);
  background: var(--color-destructive-light);
}
</style>
