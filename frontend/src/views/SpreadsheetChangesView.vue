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
    <header>
      <div>
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
        <button type="button" @click="load">查询</button>
      </div>
    </header>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!loading && !items.length" data-test="audit-empty">暂无表格变更记录</p>
    <table v-else>
      <thead><tr><th>文件</th><th>操作人</th><th>操作</th><th>状态</th><th>变更</th><th>风险</th><th></th></tr></thead>
      <tbody>
        <tr v-for="item in items" :key="item.session_id">
          <td>{{ item.document_name }}</td><td>{{ item.actor_name }}</td><td>{{ item.structure_action ? structureLabel(item.structure_action) : (item.operation === 'rollback' ? '回退' : '编辑') }}</td><td>{{ item.status }}</td><td>{{ item.structure_action ? '1 次结构调整' : `${item.change_count} 个单元格` }}</td>
          <td><span v-for="flag in item.risk_flags" :key="flag" class="risk">{{ flag }}</span></td>
          <td><button data-test="audit-detail" type="button" @click="openDetail(item.session_id)">查看详情</button></td>
        </tr>
      </tbody>
    </table>
    <dialog :open="Boolean(detail)" v-if="detail">
      <button class="close" type="button" @click="detail = null">关闭</button>
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
  </section>
</template>

<style scoped>
.spreadsheet-changes { padding: 24px; height: 100%; overflow: auto; box-sizing: border-box; }
header { display: flex; justify-content: space-between; gap: 20px; align-items: flex-end; }
header p { color: #606266; }
.filters { display: flex; gap: 8px; }
table { width: 100%; border-collapse: collapse; margin-top: 18px; background: white; }
th, td { padding: 10px; border-bottom: 1px solid #ebeef5; text-align: left; }
.risk { display: inline-block; padding: 2px 6px; margin: 2px; border-radius: 10px; color: #b88230; background: #fdf6ec; font-size: 12px; }
dialog { position: fixed; inset: 10% 12%; width: auto; max-height: 75%; overflow: auto; border: 1px solid #dcdfe6; border-radius: 10px; box-shadow: 0 12px 40px #0003; }
.close { float: right; }
.diff { display: grid; grid-template-columns: 70px 1fr 30px 1fr; gap: 8px; padding: 8px; background: #f7f8fa; margin-bottom: 4px; }
.structure-detail { padding: 10px 12px; margin: 12px 0; border-radius: 8px; background: #f4f7ff; }
[role="alert"] { color: #c45656; }
</style>
