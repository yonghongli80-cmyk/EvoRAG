<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import api from '../utils/api'
import AnalysisCard from '../components/AnalysisCard.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, DataAnalysis, Document } from '@element-plus/icons-vue'

interface Question {
  id: number
  question: string
}

interface CardGroup {
  id: number
  name: string
  questions: Question[]
  summary?: string
}

const cards = ref<CardGroup[]>([])
const loading = ref(false)
const activeCardId = ref<number | null>(null)

onMounted(async () => {
  await loadCards()
})

async function loadCards() {
  loading.value = true
  try {
    const res = await api.get('/analysis/cards')
    const cardList: CardGroup[] = res.data || []
    // Fetch all questions and summaries in parallel to avoid N+1
    await Promise.all(
      cardList.map(async (card) => {
        const [qRes, sRes] = await Promise.all([
          api.get(`/analysis/cards/${card.id}/questions`).catch(() => ({ data: [] })),
          api.get(`/analysis/cards/${card.id}/summary`).catch(() => ({ data: { summary: '' } })),
        ])
        card.questions = qRes.data || []
        card.summary = sRes.data?.summary || ''
      })
    )
    cards.value = cardList
  } catch {
    cards.value = []
  } finally {
    loading.value = false
  }
}

async function createCard() {
  try {
    const res = await api.post('/analysis/cards', { name: '新建卡片组' })
    cards.value.push({ ...res.data, questions: [], summary: '' })
    ElMessage.success('卡片组已创建')
  } catch {
    ElMessage.error('创建失败')
  }
}

async function updateCardTitle(card: CardGroup, newTitle: string) {
  card.name = newTitle
  try {
    await api.put(`/analysis/cards/${card.id}/name`, { name: card.name })
  } catch {
    ElMessage.error('保存失败')
  }
}

async function addQuestion(card: CardGroup, question: string) {
  try {
    const res = await api.post(`/analysis/cards/${card.id}/questions`, { question })
    card.questions.push({ id: res.data.id, question })
  } catch {
    ElMessage.error('保存失败')
  }
}

async function removeQuestion(card: CardGroup, questionId: number) {
  try {
    await api.delete(`/analysis/cards/${card.id}/questions/${questionId}`)
    card.questions = card.questions.filter(q => q.id !== questionId)
  } catch {
    ElMessage.error('保存失败')
  }
}

async function deleteCard(card: CardGroup) {
  try {
    await ElMessageBox.confirm(`确定删除卡片组「${card.name}」吗？`, '删除卡片组', { type: 'warning' })
    await api.delete(`/analysis/cards/${card.id}`)
    cards.value = cards.value.filter(c => c.id !== card.id)
    ElMessage.success('已删除')
  } catch {}
}

async function generateSummary(card: CardGroup) {
  try {
    const res = await api.post(`/analysis/cards/${card.id}/summary/generate`, {})
    card.summary = res.data.summary
    ElMessage.success('摘要已生成')
  } catch (err: any) {
    const detail = err?.response?.data?.detail
    ElMessage.error(detail || '生成摘要失败，请稍后重试')
  }
}

async function updateSummary(card: CardGroup, summary: string) {
  try {
    await api.put(`/analysis/cards/${card.id}/summary`, { summary })
    card.summary = summary
  } catch {
    ElMessage.error('保存摘要失败')
  }
}

async function deleteSummary(card: CardGroup) {
  try {
    await api.put(`/analysis/cards/${card.id}/summary`, { summary: '' })
    card.summary = ''
    ElMessage.success('摘要已删除')
  } catch {
    ElMessage.error('删除摘要失败')
  }
}

async function exportCard(card: CardGroup) {
  try {
    const resp = await api.get(`/analysis/cards/${card.id}/export?format=markdown`, {
      responseType: 'blob',
    })
    const blob = new Blob([resp.data], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${card.name}.md`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('导出成功')
  } catch {
    ElMessage.error('导出失败')
  }
}

function scrollToCard(cardId: number) {
  activeCardId.value = cardId
  nextTick(() => {
    const el = document.getElementById(`card-${cardId}`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  })
}
</script>

<template>
  <div class="analysis-layout">
    <!-- Sidebar: card group list -->
    <aside class="analysis-sidebar">
      <div class="sidebar-header">
        <span class="sidebar-title">卡片组</span>
      </div>
      <div v-if="cards.length" class="sidebar-list">
        <button
          v-for="card in cards"
          :key="card.id"
          class="sidebar-item"
          :class="{ active: activeCardId === card.id }"
          @click="scrollToCard(card.id)"
        >
          <el-icon class="sidebar-item-icon"><Document /></el-icon>
          <span class="sidebar-item-name">{{ card.name }}</span>
          <span class="sidebar-item-count">{{ card.questions.length }}</span>
        </button>
      </div>
      <div v-else class="sidebar-empty">暂无卡片组</div>
    </aside>

    <!-- Main content -->
    <main class="analysis-main">
      <div class="analysis-header">
        <div>
          <h1 class="analysis-title">分析</h1>
          <p class="analysis-subtitle">管理问题卡片组，追踪未解答的问题</p>
        </div>
        <el-button type="primary" @click="createCard">
          <el-icon style="margin-right: 6px;"><Plus /></el-icon>
          新建卡片组
        </el-button>
      </div>

      <div v-if="loading" class="analysis-loading">加载中...</div>

      <div v-else-if="cards.length" class="cards-grid">
        <div v-for="card in cards" :key="card.id" :id="`card-${card.id}`" class="card-wrapper">
          <AnalysisCard
            :card-id="card.id"
            :title="card.name"
            :questions="card.questions"
            :summary="card.summary"
            @update:title="(v: string) => updateCardTitle(card, v)"
            @add-question="(q: string) => addQuestion(card, q)"
            @remove-question="(qid: number) => removeQuestion(card, qid)"
            @generate-summary="generateSummary(card)"
            @update-summary="(v: string) => updateSummary(card, v)"
            @delete-summary="deleteSummary(card)"
            @delete-card="deleteCard(card)"
            @export="exportCard(card)"
          />
        </div>
      </div>

      <div v-else class="analysis-empty">
        <div class="analysis-empty-icon">
          <el-icon :size="48"><DataAnalysis /></el-icon>
        </div>
        <h3 class="analysis-empty-title">暂无卡片组</h3>
        <p class="analysis-empty-desc">创建卡片组，组织和追踪问题</p>
        <el-button type="primary" @click="createCard">
          <el-icon style="margin-right: 6px;"><Plus /></el-icon>
          新建卡片组
        </el-button>
      </div>
    </main>
  </div>
</template>

<style scoped>
.analysis-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}

/* ── Sidebar ── */
.analysis-sidebar {
  width: 220px;
  min-width: 220px;
  border-right: 1px solid var(--color-border);
  background: var(--color-surface);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.sidebar-header {
  padding: var(--space-4) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.sidebar-title {
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
}

.sidebar-list {
  display: flex;
  flex-direction: column;
  padding: var(--space-2);
  gap: 2px;
}

.sidebar-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  border: none;
  background: none;
  cursor: pointer;
  text-align: left;
  font-family: var(--font-body);
  font-size: var(--text-sm);
  color: var(--color-foreground);
  transition: background var(--duration-fast);
}

.sidebar-item:hover {
  background: var(--color-muted);
}

.sidebar-item.active {
  background: var(--color-accent-light);
  color: var(--color-accent);
}

.sidebar-item-icon {
  flex-shrink: 0;
  font-size: 14px;
}

.sidebar-item-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-item-count {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  background: var(--color-muted);
  padding: 1px 6px;
  border-radius: 10px;
}

.sidebar-empty {
  padding: var(--space-8) var(--space-4);
  text-align: center;
  color: var(--color-secondary);
  font-size: var(--text-sm);
}

/* ── Main content ── */
.analysis-main {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-6) var(--space-8);
}

.analysis-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: var(--space-8);
}

.analysis-title {
  font-family: var(--font-heading);
  font-size: var(--text-2xl);
  font-weight: var(--font-bold);
  color: var(--color-foreground);
  margin: 0;
}

.analysis-subtitle {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  margin-top: var(--space-1);
}

.analysis-loading {
  text-align: center;
  color: var(--color-secondary);
  padding: var(--space-16) 0;
}

.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: var(--space-5);
}

.card-wrapper {
  position: relative;
}

/* ── Empty state ── */
.analysis-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--space-16) var(--space-8);
}

.analysis-empty-icon {
  width: 80px;
  height: 80px;
  border-radius: var(--radius-xl);
  background: var(--color-accent-light);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-accent);
  margin-bottom: var(--space-6);
}

.analysis-empty-title {
  font-family: var(--font-heading);
  font-size: var(--text-xl);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
  margin-bottom: var(--space-2);
}

.analysis-empty-desc {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  margin-bottom: var(--space-6);
}
</style>
