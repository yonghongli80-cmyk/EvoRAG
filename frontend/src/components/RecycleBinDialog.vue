<template>
  <div v-if="visible" class="recycle-dialog" data-test="recycle-dialog">
    <div class="dialog-title">回收站</div>
    <p v-if="items.length === 0" data-test="recycle-empty">回收站为空</p>
    <ul v-else class="recycle-list">
      <li v-for="item in items" :key="item.document_id" data-test="recycle-item">
        {{ item.filename }}
        <span class="item-deadline" :data-test="restoreExpired(item.restore_until) ? 'recycle-expired' : 'recycle-deadline'">
          {{ restoreExpired(item.restore_until) ? '已过期' : `可恢复至 ${formatTime(item.restore_until)}` }}
        </span>
        <button
          :disabled="restoreExpired(item.restore_until)"
          data-test="recycle-restore"
          @click="$emit('restore', item.document_id)"
        >
          恢复
        </button>
      </li>
    </ul>
    <div class="dialog-actions">
      <button data-test="recycle-close" @click="$emit('update:visible', false)">关闭</button>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ visible: boolean; items: any[] }>()
defineEmits(['update:visible', 'restore'])

function restoreExpired(restoreUntil: number): boolean {
  return Date.now() / 1000 > restoreUntil
}

function formatTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString()
}
</script>

<style scoped>
.recycle-dialog {
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
}
.dialog-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.recycle-list {
  list-style: none;
  padding: 0;
  margin: 8px 0;
}
.item-deadline {
  color: #909399;
  margin: 0 8px;
  font-size: 13px;
}
.dialog-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
