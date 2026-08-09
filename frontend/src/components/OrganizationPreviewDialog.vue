<template>
  <div v-if="visible" class="organization-dialog" data-test="organization-dialog">
    <div class="dialog-title">将修改以下资料</div>
    <p v-if="items.length === 0" data-test="organization-empty">没有可执行的整理建议</p>
    <ul v-else class="organization-list">
      <li v-for="item in items" :key="item.id" data-test="organization-item">
        <span data-test="organization-document-id">文档 #{{ item.document_id }}</span>
        <span v-if="item.status === 'rejected'" class="item-reason" data-test="organization-rejected">
          （{{ item.reason_code }}）
        </span>
        <span v-else class="item-target" data-test="organization-target">
          移动到「{{ item.target_path }}」
        </span>
      </li>
    </ul>
    <div class="dialog-actions">
      <button data-test="organization-cancel" @click="$emit('update:visible', false)">取消</button>
      <button
        data-test="organization-confirm"
        :disabled="executableIds.length === 0"
        @click="confirmOrganization"
      >
        确认整理
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ visible: boolean; items: any[] }>()
const emit = defineEmits(['update:visible', 'confirm'])

const executableIds = computed(() =>
  props.items.filter((item) => item.status === 'executable').map((item) => item.id)
)

function confirmOrganization() {
  emit('confirm', executableIds.value)
}
</script>

<style scoped>
.organization-dialog {
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
}
.dialog-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.organization-list {
  list-style: none;
  padding: 0;
  margin: 8px 0;
}
.item-target,
.item-reason {
  color: #606266;
  margin-left: 8px;
}
.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
</style>
