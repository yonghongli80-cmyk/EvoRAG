<template>
  <div v-if="visible" class="archive-manifest-dialog" data-test="archive-dialog">
    <div class="dialog-title">确认压缩包内容</div>
    <p class="dialog-hint">请确认压缩包内的文件，确认后才会导入。</p>
    <ul class="entry-list">
      <li v-for="entry in entries" :key="entry.id" :data-test="`archive-entry-${entry.state}`">
        {{ entry.normalized_path || entry.relative_path }}
        <span class="entry-state">
          {{ entry.state === 'approved' ? '可导入' : reasonText(entry.reason_code) }}
        </span>
      </li>
    </ul>
    <div class="dialog-actions">
      <button data-test="archive-cancel" @click="$emit('update:visible', false)">取消</button>
      <button data-test="archive-confirm" :disabled="selectedIds.length === 0" @click="confirmImport">
        确认导入
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ visible: boolean; entries: any[] }>()
const emit = defineEmits(['update:visible', 'confirm'])

const selectedIds = computed(() =>
  props.entries.filter((entry) => entry.state === 'approved').map((entry) => entry.id)
)

function reasonText(code: string): string {
  const map: Record<string, string> = {
    unsafe_path: '不安全路径',
    nesting_depth_exceeded: '目录层级过深',
    encrypted_entry: '加密文件',
    unsupported_inner_format: '不支持的格式',
    directory: '目录',
    entry_size_exceeded: '超过大小限制'
  }
  return map[code] || code
}

function confirmImport() {
  emit('confirm', selectedIds.value)
}
</script>

<style scoped>
.archive-manifest-dialog {
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
}
.dialog-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.dialog-hint {
  color: #606266;
  font-size: 13px;
}
.entry-list {
  list-style: none;
  padding: 0;
  margin: 8px 0;
}
.entry-state {
  color: #909399;
  margin-left: 8px;
}
.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
</style>
