<template>
  <div class="material-job-status" data-test="job-status">
    <span v-if="status === 'queued' || status === 'waiting_for_capacity'" data-test="job-queued">排队中</span>
    <span v-else-if="status === 'running'" data-test="job-running">处理中</span>
    <span v-else-if="status === 'done'" data-test="job-done">已完成</span>
    <span v-else-if="status === 'cancelled'" data-test="job-cancelled">已取消</span>
    <span v-else-if="status === 'archive_only'" data-test="job-archive-only">仅归档</span>
    <span v-else data-test="job-failed">失败{{ errorCode ? `（${errorCode}）` : '' }}</span>
  </div>
</template>

<script setup lang="ts">
defineProps<{ status: string; errorCode?: string }>()
</script>
