<template>
  <el-dialog
    :model-value="visible"
    :title="`同名文件「${filename}」已存在`"
    width="420px"
    :close-on-click-modal="false"
    @update:model-value="$emit('update:visible', $event)"
  >
    <div data-test="duplicate-dialog">
      <p>已存在该文件，当前版本为 {{ currentVersion }}。请选择处理方式：</p>
      <el-space direction="vertical" class="duplicate-actions">
        <el-button data-test="duplicate-overwrite" @click="$emit('confirm', 'overwrite')">
          覆盖（保留历史版本）
        </el-button>
        <el-button data-test="duplicate-keep" @click="$emit('confirm', 'keep')">
          保留（另存为新文件）
        </el-button>
        <el-button data-test="duplicate-reject" @click="$emit('confirm', 'reject')">
          跳过（取消上传）
        </el-button>
      </el-space>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import type { DuplicateAction } from '../api/documents'

defineProps<{
  visible: boolean
  filename: string
  currentVersion: number
}>()

defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'confirm', action: DuplicateAction): void
}>()
</script>
