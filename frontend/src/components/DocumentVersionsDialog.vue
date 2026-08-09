<template>
  <el-dialog
    :model-value="visible"
    title="文档版本"
    width="520px"
    @update:model-value="$emit('update:visible', $event)"
  >
    <el-table :data="versions" size="small">
      <el-table-column prop="version_no" label="版本" width="70" />
      <el-table-column prop="filename" label="文件名" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'current' ? 'success' : 'info'" size="small">
            {{ row.status === 'current' ? '当前' : '历史' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            @click="$emit('download', row.id)"
          >
            下载
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>
</template>

<script setup lang="ts">
import type { DocumentVersion } from '../api/documents'

defineProps<{ visible: boolean; versions: DocumentVersion[] }>()

defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'download', versionId: number): void
}>()
</script>
