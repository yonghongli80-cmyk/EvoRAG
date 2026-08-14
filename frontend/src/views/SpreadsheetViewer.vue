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
    <header class="viewer-toolbar">
      <div>
        <h2>表格查看与编辑</h2>
        <span>版本 v{{ currentVersionId }}</span>
      </div>
      <div class="toolbar-actions">
        <button data-test="show-version-history" type="button" @click="showHistory = !showHistory">版本历史</button>
        <button
          v-if="canEdit && !editing"
          data-test="structure-spreadsheet"
          type="button"
          @click="showStructure = !showStructure"
        >结构调整</button>
        <button
          v-if="canEdit && !editing"
          data-test="edit-spreadsheet"
          type="button"
          @click="editing = true"
        >编辑表格</button>
        <template v-if="editing">
          <input v-model="reason" maxlength="500" placeholder="修改说明（可选）">
          <button data-test="discard-spreadsheet" type="button" @click="discard">撤销未保存修改</button>
          <button
            data-test="save-spreadsheet"
            type="button"
            :disabled="!dirtyCells.length || saving || tooManyChanges"
            @click="save"
          >{{ saving ? '保存中…' : `保存 ${dirtyCells.length} 处修改` }}</button>
          <span v-if="tooManyChanges" role="alert">单次最多保存 500 个单元格，请分批提交。</span>
        </template>
      </div>
    </header>
    <form v-if="showStructure" class="structure-panel" @submit.prevent="applyStructure">
      <label>
        操作
        <select v-model="structureAction" data-test="structure-action">
          <option value="insert_rows">插入整行</option>
          <option value="delete_rows">删除整行</option>
          <option value="insert_columns">插入整列</option>
          <option value="delete_columns">删除整列</option>
          <option value="add_sheet">新增工作表</option>
        </select>
      </label>
      <template v-if="!structureIsAddSheet">
        <label>起始位置 <input v-model.number="structureIndex" data-test="structure-index" type="number" min="1"></label>
        <label>数量 <input v-model.number="structureCount" data-test="structure-count" type="number" min="1" max="100"></label>
      </template>
      <label v-else>工作表名称 <input v-model="newSheetName" data-test="new-sheet-name" maxlength="31"></label>
      <input v-model="structureReason" maxlength="500" placeholder="修改说明（可选）">
      <button data-test="apply-structure" type="button" :disabled="!structureCanSubmit || saving" @click="applyStructure">
        {{ saving ? '处理中…' : '保存为新版本' }}
      </button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <div class="viewer-body">
    <div class="sheet-scroll">
      <table v-if="columns.length">
        <thead>
          <tr><th>行</th><th v-for="column in columns" :key="column.column_key">{{ column.display_name }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.row_number" :data-row="row.row_number">
            <th>{{ row.row_number }}</th>
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
.viewer-toolbar { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 16px; }
.toolbar-actions { display: flex; gap: 8px; align-items: center; }
.structure-panel { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; padding: 12px; margin-bottom: 16px; border: 1px solid #dcdfe6; border-radius: 8px; background: #fafafa; }
.structure-panel label { display: grid; gap: 4px; }
.sheet-scroll { overflow: auto; border: 1px solid #e5e7eb; border-radius: 8px; }
.viewer-body { display: flex; gap: 18px; align-items: flex-start; }
.viewer-body .sheet-scroll { flex: 1; min-width: 0; }
table { width: 100%; border-collapse: collapse; }
th, td { border: 1px solid #eef0f3; padding: 8px; white-space: nowrap; }
thead th { background: #f7f8fa; position: sticky; top: 0; }
td input { width: 100%; min-width: 110px; box-sizing: border-box; border: 1px solid #dcdfe6; padding: 6px; }
td input.is-dirty { background: #fff7d6; border-color: #e6a23c; }
[role="alert"] { color: #c45656; }
</style>
