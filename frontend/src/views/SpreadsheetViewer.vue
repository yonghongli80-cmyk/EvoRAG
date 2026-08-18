<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  getSpreadsheetEditStatus,
  getSpreadsheetRows,
  saveSpreadsheetEdits,
  saveSpreadsheetStructureEdit,
  restoreSpreadsheetVersion,
  type SpreadsheetCellPatch,
  type SpreadsheetColumn,
  type SpreadsheetRow,
  type SpreadsheetStructureAction,
} from '../api/spreadsheets'
import { fetchDocumentVersions, type DocumentVersion } from '../api/documents'
import SpreadsheetVersionHistory from '../components/SpreadsheetVersionHistory.vue'

const route = useRoute()
const router = useRouter()
const documentId = Number(route.params.documentId)
const sheetKey = String(route.query.sheet || 'sheet_0001')
const currentVersionId = ref(Number(route.params.versionId))
const rows = ref<SpreadsheetRow[]>([])
const columns = ref<SpreadsheetColumn[]>([])
const originals = ref<Record<string, unknown>>({})
const drafts = ref<Record<string, unknown>>({})
const nonEditable = ref(new Set<string>())
const canEdit = ref(false)
const editing = ref(false)
const saving = ref(false)
const reason = ref('')
const error = ref('')
const versions = ref<DocumentVersion[]>([])
const showHistory = ref(false)
const showStructure = ref(false)
const structureAction = ref<SpreadsheetStructureAction>('insert_rows')
const structureIndex = ref(1)
const structureCount = ref(1)
const newSheetName = ref('')
const structureReason = ref('')
const structureIsAddSheet = computed(() => structureAction.value === 'add_sheet')
const structureCanSubmit = computed(() => structureIsAddSheet.value
  ? Boolean(newSheetName.value.trim())
  : structureIndex.value >= 1 && structureCount.value >= 1 && structureCount.value <= 100)

function columnLetter(ordinal: number) {
  let value = ordinal
  let result = ''
  while (value > 0) {
    value -= 1
    result = String.fromCharCode(65 + (value % 26)) + result
    value = Math.floor(value / 26)
  }
  return result
}

function address(row: SpreadsheetRow, column: SpreadsheetColumn) {
  return `${columnLetter(column.ordinal)}${row.row_number}`
}

function sameValue(left: unknown, right: unknown) {
  if (left === null || left === undefined || left === '') {
    return right === null || right === undefined || right === ''
  }
  return String(left) === String(right)
}

function isDirty(cell: string) {
  return !sameValue(originals.value[cell], drafts.value[cell])
}

const dirtyCells = computed(() => Object.keys(drafts.value).filter(isDirty))
const tooManyChanges = computed(() => dirtyCells.value.length > 500)

async function load() {
  const response = await getSpreadsheetRows(
    documentId, currentVersionId.value, sheetKey,
  )
  rows.value = response.rows
  columns.value = response.columns
  canEdit.value = response.can_edit
  nonEditable.value = new Set(response.non_editable_cells)
  const values: Record<string, unknown> = {}
  for (const row of response.rows) {
    response.columns.forEach((column, index) => {
      values[address(row, column)] = row.values[index]
    })
  }
  originals.value = values
  drafts.value = { ...values }
}

async function loadHistory() {
  versions.value = await fetchDocumentVersions(documentId)
}

function updateCell(cell: string, column: SpreadsheetColumn, event: Event) {
  const input = event.target as HTMLInputElement
  if (column.value_type === 'boolean') {
    drafts.value[cell] = input.checked
  } else {
    drafts.value[cell] = input.value
  }
}

function discard() {
  drafts.value = { ...originals.value }
  error.value = ''
}

function patchFor(cell: string, column: SpreadsheetColumn): SpreadsheetCellPatch {
  const raw = drafts.value[cell]
  if (raw === '' || raw === null || raw === undefined) {
    return { sheet_key: sheetKey, cell, value_type: 'blank', value: null }
  }
  if (column.value_type === 'integer' || column.value_type === 'decimal') {
    return { sheet_key: sheetKey, cell, value_type: 'number', value: Number(raw) }
  }
  if (column.value_type === 'boolean') {
    return { sheet_key: sheetKey, cell, value_type: 'boolean', value: Boolean(raw) }
  }
  if (column.value_type === 'date' || column.value_type === 'datetime') {
    return { sheet_key: sheetKey, cell, value_type: 'date', value: String(raw) }
  }
  return { sheet_key: sheetKey, cell, value_type: 'text', value: String(raw) }
}

async function poll(sessionId: number) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const status = await getSpreadsheetEditStatus(documentId, sessionId)
    if (status.status === 'active') return status.version_id
    if (['failed', 'cancelled', 'rejected'].includes(status.status)) {
      throw new Error(status.error_code || 'spreadsheet_edit_failed')
    }
    await new Promise(resolve => window.setTimeout(resolve, 250 + attempt * 100))
  }
  throw new Error('spreadsheet_edit_timeout')
}

async function save() {
  if (!dirtyCells.value.length || saving.value || tooManyChanges.value) return
  saving.value = true
  error.value = ''
  try {
    const patches = dirtyCells.value.map((cell) => {
      const columnOrdinal = cell.match(/^[A-Z]+/)?.[0]
      const column = columns.value.find(
        item => columnLetter(item.ordinal) === columnOrdinal,
      )!
      return patchFor(cell, column)
    })
    const result = await saveSpreadsheetEdits(documentId, {
      base_version_id: currentVersionId.value,
      changes: patches,
      reason: reason.value,
    })
    if (result.status === 'unchanged') {
      originals.value = { ...drafts.value }
      editing.value = false
      return
    }
    const activatedVersion = await poll(result.session_id)
    currentVersionId.value = activatedVersion
    await router.replace({
      name: route.name || undefined,
      params: { ...route.params, versionId: String(activatedVersion) },
      query: route.query,
    })
    await load()
    editing.value = false
    reason.value = ''
  } catch (caught: any) {
    if (caught?.response?.status === 409) {
      error.value = '文件已被其他人更新，请刷新后重新应用当前修改。'
    } else {
      error.value = '保存失败，本地修改仍然保留，请稍后重试。'
    }
  } finally {
    saving.value = false
  }
}

async function applyStructure() {
  if (!structureCanSubmit.value || saving.value) return
  const destructive = structureAction.value === 'delete_rows'
    || structureAction.value === 'delete_columns'
  if (destructive && !window.confirm('删除可能清除已有内容，确认继续并创建新版本吗？')) return
  saving.value = true
  error.value = ''
  try {
    const request = structureIsAddSheet.value
      ? {
          base_version_id: currentVersionId.value,
          action: structureAction.value,
          new_sheet_name: newSheetName.value.trim(),
          reason: structureReason.value,
        }
      : {
          base_version_id: currentVersionId.value,
          action: structureAction.value,
          sheet_key: sheetKey,
          index: structureIndex.value,
          count: structureCount.value,
          confirm_nonempty: destructive,
          reason: structureReason.value,
        }
    const accepted = await saveSpreadsheetStructureEdit(documentId, request)
    const activatedVersion = await poll(accepted.session_id)
    currentVersionId.value = activatedVersion
    await router.replace({
      name: route.name || undefined,
      params: { ...route.params, versionId: String(activatedVersion) },
      query: route.query,
    })
    await Promise.all([load(), loadHistory()])
    showStructure.value = false
    newSheetName.value = ''
    structureReason.value = ''
  } catch (caught: any) {
    const code = caught?.response?.data?.detail?.code
    error.value = caught?.response?.status === 409
      ? '文件已被其他人更新，请刷新后重试。'
      : `结构调整失败${code ? `：${code}` : '，请稍后重试。'}`
  } finally {
    saving.value = false
  }
}

async function restoreVersion(sourceVersionId: number) {
  const source = versions.value.find(item => item.id === sourceVersionId)
  const current = versions.value.find(item => item.id === currentVersionId.value)
  if (!window.confirm(
    `确认从 v${current?.version_no ?? currentVersionId.value} 回退到 v${source?.version_no ?? sourceVersionId}？系统会创建一个新版本。`,
  )) return
  const restoreReason = window.prompt('回退说明（可选）', '') ?? ''
  saving.value = true
  error.value = ''
  try {
    const accepted = await restoreSpreadsheetVersion(
      documentId,
      sourceVersionId,
      { base_version_id: currentVersionId.value, reason: restoreReason },
    )
    const activatedVersion = await poll(accepted.session_id)
    currentVersionId.value = activatedVersion
    await router.replace({
      name: route.name || undefined,
      params: { ...route.params, versionId: String(activatedVersion) },
      query: route.query,
    })
    await Promise.all([load(), loadHistory()])
  } catch (caught: any) {
    error.value = caught?.response?.status === 409
      ? '文件已被其他人更新，无法按旧版本基线回退，请刷新后重试。'
      : '回退失败，原来的当前版本没有改变。'
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    await Promise.all([load(), loadHistory()])
  } catch {
    error.value = '无法加载表格数据'
  }
})
</script>

<template>
  <section class="spreadsheet-viewer">
    <header class="viewer-toolbar" data-test="spreadsheet-toolbar">
      <div class="toolbar-heading">
        <p class="eyebrow">表格工作台</p>
        <div class="title-row">
          <h2>表格查看与编辑</h2>
          <span class="version-chip">v{{ currentVersionId }}</span>
        </div>
        <p class="toolbar-subtitle">修改会保存为新版本，历史内容始终可回退。</p>
      </div>
      <div class="toolbar-actions">
        <button class="action-button secondary" data-test="show-version-history" type="button" @click="showHistory = !showHistory">版本历史</button>
        <button
          v-if="canEdit && !editing"
          class="action-button secondary"
          data-test="structure-spreadsheet"
          type="button"
          @click="showStructure = !showStructure"
        >结构调整</button>
        <button
          v-if="canEdit && !editing"
          class="action-button primary"
          data-test="edit-spreadsheet"
          type="button"
          @click="editing = true"
        >编辑表格</button>
        <template v-if="editing">
          <input class="reason-input" v-model="reason" maxlength="500" placeholder="修改说明（可选）">
          <button class="action-button secondary" data-test="discard-spreadsheet" type="button" @click="discard">撤销修改</button>
          <button
            class="action-button primary"
            data-test="save-spreadsheet"
            type="button"
            :disabled="!dirtyCells.length || saving || tooManyChanges"
            @click="save"
          >{{ saving ? '保存中…' : `保存 ${dirtyCells.length} 处修改` }}</button>
        </template>
      </div>
    </header>
    <div v-if="editing" class="edit-status" :class="{ dirty: dirtyCells.length }">
      <span class="status-dot" />
      <span>{{ dirtyCells.length ? `已有 ${dirtyCells.length} 处未保存修改` : '编辑模式：选择单元格开始修改' }}</span>
      <span v-if="tooManyChanges" role="alert">单次最多保存 500 个单元格，请分批提交。</span>
    </div>
    <form v-if="showStructure" class="structure-panel" @submit.prevent="applyStructure">
      <div class="structure-heading">
        <strong>结构调整</strong>
        <span>本次操作完成后会自动生成新版本</span>
      </div>
      <label class="field-label">
        <span>操作类型</span>
        <select v-model="structureAction" data-test="structure-action">
          <option value="insert_rows">插入整行</option>
          <option value="delete_rows">删除整行</option>
          <option value="insert_columns">插入整列</option>
          <option value="delete_columns">删除整列</option>
          <option value="add_sheet">新增工作表</option>
        </select>
      </label>
      <template v-if="!structureIsAddSheet">
        <label class="field-label"><span>起始位置</span><input v-model.number="structureIndex" data-test="structure-index" type="number" min="1"></label>
        <label class="field-label"><span>数量</span><input v-model.number="structureCount" data-test="structure-count" type="number" min="1" max="100"></label>
      </template>
      <label v-else class="field-label"><span>工作表名称</span><input v-model="newSheetName" data-test="new-sheet-name" maxlength="31"></label>
      <label class="field-label grow"><span>修改说明</span><input v-model="structureReason" maxlength="500" placeholder="可选"></label>
      <button class="action-button primary" data-test="apply-structure" type="button" :disabled="!structureCanSubmit || saving" @click="applyStructure">
        {{ saving ? '处理中…' : '保存为新版本' }}
      </button>
    </form>
    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <div class="viewer-body">
      <div class="sheet-scroll">
        <table v-if="columns.length">
          <thead>
            <tr>
              <th class="row-heading">行</th>
              <th v-for="column in columns" :key="column.column_key">
                <span class="column-letter">{{ columnLetter(column.ordinal) }}</span>
                <span>{{ column.display_name }}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.row_number" :data-row="row.row_number">
              <th class="row-heading">{{ row.row_number }}</th>
              <td v-for="column in columns" :key="column.column_key">
                <input
                  v-if="editing"
                  :data-cell="address(row, column)"
                  :class="{ 'is-dirty': isDirty(address(row, column)) }"
                  :type="column.value_type === 'boolean' ? 'checkbox' : (column.value_type === 'integer' || column.value_type === 'decimal' ? 'number' : (column.value_type === 'date' ? 'date' : 'text'))"
                  :value="column.value_type === 'boolean' ? undefined : drafts[address(row, column)] as any"
                  :checked="column.value_type === 'boolean' ? Boolean(drafts[address(row, column)]) : undefined"
                  :disabled="nonEditable.has(address(row, column))"
                  @input="updateCell(address(row, column), column, $event)"
                  @change="updateCell(address(row, column), column, $event)"
                >
                <span v-else>{{ drafts[address(row, column)] }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else-if="!error" class="empty-sheet">暂无可展示的表格数据</div>
      </div>
      <SpreadsheetVersionHistory
        v-if="showHistory"
        :versions="versions"
        :can-edit="canEdit"
        :current-version-id="currentVersionId"
        :busy="saving"
        @restore="restoreVersion"
      />
    </div>
  </section>
</template>

<style scoped>
.spreadsheet-viewer {
  height: 100%;
  padding: var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  overflow: hidden;
  background: var(--color-background);
}

.viewer-toolbar {
  display: flex;
  justify-content: space-between;
  gap: var(--space-6);
  align-items: center;
  padding: var(--space-5) var(--space-6);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: var(--shadow-xs);
}

.toolbar-heading { min-width: 260px; }
.eyebrow { margin: 0 0 var(--space-1); color: var(--color-primary); font-size: var(--text-xs); font-weight: var(--font-semibold); }
.title-row { display: flex; align-items: center; gap: var(--space-3); }
.title-row h2 { margin: 0; font-family: var(--font-heading); font-size: var(--text-xl); color: var(--color-foreground); }
.version-chip { padding: 3px 9px; border-radius: var(--radius-full); background: var(--color-accent-light); color: var(--color-primary); font-size: var(--text-xs); font-weight: var(--font-semibold); }
.toolbar-subtitle { margin: var(--space-1) 0 0; color: var(--color-secondary); font-size: var(--text-sm); }
.toolbar-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--space-2); align-items: center; }

.action-button {
  min-height: 36px;
  padding: 0 var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  font-family: var(--font-body);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}
.action-button.secondary { color: var(--color-foreground); background: var(--color-surface); }
.action-button.secondary:hover { border-color: var(--color-border-hover); background: var(--color-surface-2); }
.action-button.primary { color: var(--color-on-primary); border-color: var(--color-primary); background: var(--color-primary); }
.action-button.primary:hover { background: var(--color-primary-hover); }
.action-button:disabled { opacity: .5; cursor: not-allowed; }
.reason-input { width: 190px; }

.reason-input,
.field-label input,
.field-label select {
  min-height: 36px;
  padding: 0 var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface);
  font: inherit;
}

.edit-status {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-secondary);
  background: var(--color-surface);
  font-size: var(--text-sm);
}
.edit-status.dirty { border-color: #fde68a; color: #92400e; background: var(--color-warning-light); }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--color-muted); }
.edit-status.dirty .status-dot { background: var(--color-warning); }

.structure-panel {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: end;
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: var(--shadow-xs);
}
.structure-heading { min-width: 180px; display: grid; gap: 2px; align-self: center; }
.structure-heading strong { color: var(--color-foreground); }
.structure-heading span { color: var(--color-secondary); font-size: var(--text-xs); }
.field-label { display: grid; gap: var(--space-1); color: var(--color-secondary); font-size: var(--text-xs); font-weight: var(--font-medium); }
.field-label.grow { flex: 1; min-width: 180px; }

.error-banner { margin: 0; padding: var(--space-3) var(--space-4); border: 1px solid #fecaca; border-radius: var(--radius); color: #b91c1c; background: var(--color-destructive-light); font-size: var(--text-sm); }
.viewer-body { flex: 1; min-height: 0; display: flex; gap: var(--space-4); align-items: stretch; }
.sheet-scroll { flex: 1; min-width: 0; overflow: auto; border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: var(--color-surface); box-shadow: var(--shadow-xs); }
table { width: 100%; border-collapse: separate; border-spacing: 0; }
th, td { min-width: 140px; height: 44px; padding: var(--space-2) var(--space-3); border-right: 1px solid var(--color-border); border-bottom: 1px solid var(--color-border); white-space: nowrap; text-align: left; font-size: var(--text-sm); }
thead th { position: sticky; z-index: 2; top: 0; background: var(--color-surface-2); color: var(--color-secondary); font-weight: var(--font-semibold); }
.row-heading { position: sticky; z-index: 1; left: 0; min-width: 58px; width: 58px; text-align: center; color: var(--color-secondary); background: var(--color-surface-2); }
thead .row-heading { z-index: 3; }
.column-letter { display: inline-flex; align-items: center; justify-content: center; min-width: 24px; height: 20px; margin-right: var(--space-2); border-radius: var(--radius-sm); color: var(--color-primary); background: var(--color-accent-light); font-size: 11px; }
tbody tr:hover td { background: #f8fbff; }
td input { width: 100%; min-width: 110px; min-height: 32px; padding: 5px var(--space-2); border: 1px solid var(--color-border); border-radius: var(--radius-sm); color: var(--color-foreground); background: var(--color-surface); font: inherit; }
td input:focus { border-color: var(--color-primary); box-shadow: 0 0 0 2px var(--color-accent-light); outline: none; }
td input.is-dirty { border-color: var(--color-warning); background: var(--color-warning-light); }
.empty-sheet { min-height: 320px; display: grid; place-items: center; color: var(--color-secondary); font-size: var(--text-sm); }

@media (max-width: 960px) {
  .spreadsheet-viewer { padding: var(--space-3); }
  .viewer-toolbar { align-items: flex-start; flex-direction: column; }
  .toolbar-actions { justify-content: flex-start; }
  .viewer-body { flex-direction: column; }
}
</style>
