<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  getSpreadsheetChangeDetail,
  getSpreadsheetChanges,
  type SpreadsheetChangeAudit,
  type SpreadsheetChangeDetail,
} from '../api/spreadsheets'

const items = ref<SpreadsheetChangeAudit[]>([])
const detail = ref<SpreadsheetChangeDetail | null>(null)
const status = ref('')
const operation = ref('')
const risk = ref('')
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await getSpreadsheetChanges({
      status: status.value || undefined,
      operation: operation.value || undefined,
      risk: risk.value || undefined,
      limit: 50,
    })
    items.value = response.items
  } catch {
    error.value = '无法加载表格变更记录'
  } finally {
    loading.value = false
  }
}

async function openDetail(sessionId: number) {
  try {
    detail.value = await getSpreadsheetChangeDetail(sessionId)
  } catch {
    error.value = '无法加载变更详情'
  }
}

function display(value: unknown, redacted?: boolean) {
  if (redacted) return '已脱敏'
  if (value === null || value === undefined || value === '') return '空白'
  return String(value)
}

function structureLabel(action: SpreadsheetChangeAudit['structure_action']) {
  return ({
    insert_rows: '插入整行',
    delete_rows: '删除整行',
    insert_columns: '插入整列',
    delete_columns: '删除整列',
    add_sheet: '新增工作表',
  } as Record<string, string>)[action || ''] || ''
}

onMounted(load)
</script>

<template>
  <section class="spreadsheet-changes">
    <header class="page-header">
      <div>
        <span class="eyebrow">管理后台</span>
        <h2>公共表格变更记录</h2>
        <p>查看谁在什么时间修改或回退了表格。审计记录不可删除。</p>
      </div>
      <div class="filters">
        <select v-model="status" aria-label="状态筛选" @change="load">
          <option value="">全部状态</option><option value="pending">处理中</option><option value="active">已生效</option><option value="failed">失败</option><option value="rejected">已拒绝</option>
        </select>
        <select v-model="operation" aria-label="操作筛选" @change="load">
          <option value="">全部操作</option><option value="edit">编辑</option><option value="rollback">回退</option>
        </select>
        <input v-model="risk" placeholder="风险标记" @keyup.enter="load">
        <button class="query-button" type="button" @click="load">查询</button>
      </div>
    </header>
    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <div v-else-if="!loading && !items.length" class="audit-empty" data-test="audit-empty">
      <strong>暂无表格变更记录</strong>
      <span>公共表格发生修改或回退后，会自动记录在这里。</span>
    </div>
    <div v-else class="table-card">
    <table>
      <thead><tr><th>文件</th><th>操作人</th><th>操作</th><th>状态</th><th>变更</th><th>风险</th><th></th></tr></thead>
      <tbody>
        <tr v-for="item in items" :key="item.session_id">
          <td>{{ item.document_name }}</td><td>{{ item.actor_name }}</td><td>{{ item.structure_action ? structureLabel(item.structure_action) : (item.operation === 'rollback' ? '回退' : '编辑') }}</td><td>{{ item.status }}</td><td>{{ item.structure_action ? '1 次结构调整' : `${item.change_count} 个单元格` }}</td>
          <td><span v-for="flag in item.risk_flags" :key="flag" class="risk">{{ flag }}</span></td>
          <td><button class="detail-button" data-test="audit-detail" type="button" @click="openDetail(item.session_id)">查看详情</button></td>
        </tr>
      </tbody>
    </table>
    </div>
    <div v-if="detail" class="dialog-backdrop" @click.self="detail = null">
    <dialog :open="Boolean(detail)">
      <button class="close" type="button" aria-label="关闭详情" @click="detail = null">×</button>
      <span class="eyebrow">变更详情</span>
      <h3>{{ detail.document_name }} · {{ detail.actor_name }}</h3>
      <p>{{ detail.reason || '未填写修改说明' }}</p>
      <section v-if="detail.structure_change" data-test="structure-detail" class="structure-detail">
        <h4>{{ structureLabel(detail.structure_change.action) }}</h4>
        <p v-if="detail.structure_change.action === 'add_sheet'">新工作表：{{ detail.structure_change.new_sheet_name }}</p>
        <p v-else>
          工作表：{{ detail.structure_change.sheet_name }}；起始位置：{{ detail.structure_change.index }}；数量：{{ detail.structure_change.count }}
        </p>
        <p v-if="detail.structure_change.affected_nonempty_cells">
          影响非空单元格：{{ detail.structure_change.affected_nonempty_cells }}
        </p>
      </section>
      <section v-for="sheet in detail.sheets" :key="sheet.sheet_key">
        <h4>{{ sheet.sheet_name }}</h4>
        <div v-for="change in sheet.changes" :key="change.cell" class="diff">
          <strong>{{ change.cell }}</strong>
          <span>{{ display(change.old_value, change.redacted) }}</span>
          <span>→</span>
          <span>{{ display(change.new_value, change.redacted) }}</span>
        </div>
      </section>
    </dialog>
    </div>
  </section>
</template>

<style scoped>
.spreadsheet-changes { height: 100%; padding: var(--space-6); overflow: auto; box-sizing: border-box; background: var(--color-background); }
.page-header { display: flex; justify-content: space-between; gap: var(--space-6); align-items: flex-end; margin-bottom: var(--space-5); }
.eyebrow { display: block; margin-bottom: var(--space-1); color: var(--color-primary); font-size: 10px; font-weight: var(--font-bold); letter-spacing: .1em; text-transform: uppercase; }
.page-header h2 { margin: 0; color: var(--color-foreground); font-family: var(--font-heading); font-size: var(--text-2xl); }
.page-header p { margin: var(--space-1) 0 0; color: var(--color-secondary); font-size: var(--text-sm); }
.filters { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--space-2); }
.filters select, .filters input { min-height: 38px; padding: 0 var(--space-3); border: 1px solid var(--color-border); border-radius: var(--radius); color: var(--color-foreground); background: var(--color-surface); font: inherit; font-size: var(--text-sm); }
.query-button { min-height: 38px; padding: 0 var(--space-5); border: 1px solid var(--color-primary); border-radius: var(--radius); color: var(--color-on-primary); background: var(--color-primary); font: inherit; font-size: var(--text-sm); font-weight: var(--font-medium); cursor: pointer; }
.table-card { overflow: hidden; border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: var(--color-surface); box-shadow: var(--shadow-xs); }
table { width: 100%; border-collapse: collapse; background: var(--color-surface); }
th, td { padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--color-border); text-align: left; font-size: var(--text-sm); }
th { color: var(--color-secondary); background: var(--color-surface-2); font-weight: var(--font-semibold); }
tbody tr:hover { background: #f8fbff; }
.risk { display: inline-block; padding: 3px 8px; margin: 2px; border-radius: var(--radius-full); color: #92400e; background: var(--color-warning-light); font-size: var(--text-xs); }
.detail-button { min-height: 30px; padding: 0 var(--space-3); border: 1px solid var(--color-border); border-radius: var(--radius); color: var(--color-primary); background: var(--color-surface); font: inherit; font-size: var(--text-xs); cursor: pointer; }
.audit-empty { min-height: 340px; display: grid; place-content: center; gap: var(--space-2); text-align: center; border: 1px dashed var(--color-border); border-radius: var(--radius-lg); color: var(--color-secondary); background: var(--color-surface); }
.audit-empty strong { color: var(--color-foreground); font-size: var(--text-base); }
.audit-empty span { font-size: var(--text-sm); }
.error-banner { padding: var(--space-3) var(--space-4); border: 1px solid #fecaca; border-radius: var(--radius); color: #b91c1c; background: var(--color-destructive-light); font-size: var(--text-sm); }
.dialog-backdrop { position: fixed; z-index: 1000; inset: 0; display: grid; place-items: center; padding: var(--space-6); background: rgba(15, 23, 42, .38); backdrop-filter: blur(2px); }
dialog { position: relative; width: min(900px, 100%); max-height: 82vh; padding: var(--space-6); overflow: auto; border: 1px solid var(--color-border); border-radius: var(--radius-xl); color: var(--color-foreground); background: var(--color-surface); box-shadow: var(--shadow-lg); }
dialog h3 { margin: 0 40px var(--space-2) 0; }
dialog > p { color: var(--color-secondary); font-size: var(--text-sm); }
.close { position: absolute; top: var(--space-4); right: var(--space-4); width: 34px; height: 34px; border: 1px solid var(--color-border); border-radius: var(--radius); color: var(--color-secondary); background: var(--color-surface); font-size: 20px; cursor: pointer; }
.diff { display: grid; grid-template-columns: 70px 1fr 30px 1fr; gap: var(--space-2); padding: var(--space-3); border-radius: var(--radius); background: var(--color-surface-2); margin-bottom: var(--space-2); font-size: var(--text-sm); }
.structure-detail { padding: var(--space-4); margin: var(--space-4) 0; border: 1px solid var(--color-border); border-radius: var(--radius); background: var(--color-accent-subtle); }

@media (max-width: 900px) {
  .page-header { align-items: flex-start; flex-direction: column; }
  .filters { justify-content: flex-start; }
  .table-card { overflow: auto; }
}
</style>
