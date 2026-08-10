<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { getSpreadsheetRows, type SpreadsheetRow } from '../api/spreadsheets'

const route = useRoute()
const rows = ref<SpreadsheetRow[]>([])
const error = ref('')
const documentId = Number(route.params.documentId)
const versionId = Number(route.params.versionId)
const sheetKey = String(route.query.sheet || 'sheet_0001')

onMounted(async () => {
  try { rows.value = (await getSpreadsheetRows(documentId, versionId, sheetKey)).rows }
  catch { error.value = '无法加载表格数据' }
})
</script>

<template>
  <section class="spreadsheet-viewer">
    <h2>表格查看</h2>
    <p v-if="error" role="alert">{{ error }}</p>
    <table v-else>
      <tbody><tr v-for="row in rows" :key="row.row_number" :data-row="row.row_number"><th>{{ row.row_number }}</th><td v-for="(value, index) in row.values" :key="index">{{ value }}</td></tr></tbody>
    </table>
  </section>
</template>
