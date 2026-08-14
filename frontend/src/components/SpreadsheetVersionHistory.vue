<script setup lang="ts">
import type { DocumentVersion } from '../api/documents'

defineProps<{
  versions: DocumentVersion[]
  canEdit: boolean
  currentVersionId: number
  busy?: boolean
}>()

defineEmits<{
  (event: 'restore', sourceVersionId: number): void
}>()

function formatTime(timestamp: number) {
  return new Date(timestamp * 1000).toLocaleString('zh-CN')
}
</script>

<template>
  <aside class="version-history" aria-label="表格版本历史">
    <h3>版本历史</h3>
    <p class="history-note">回退不会覆盖历史文件，而是创建一个新的当前版本。</p>
    <ol>
      <li
        v-for="version in versions"
        :key="version.id"
        :data-version="version.id"
        :class="{ current: version.id === currentVersionId }"
      >
        <div>
          <strong>v{{ version.version_no }}</strong>
          <span>{{ version.id === currentVersionId ? '当前版本' : '历史版本' }}</span>
        </div>
        <time>{{ formatTime(version.created_at) }}</time>
        <p v-if="version.restored_from_version_id">
          由 v{{ versions.find(item => item.id === version.restored_from_version_id)?.version_no ?? version.restored_from_version_id }} 回退生成
        </p>
        <button
          v-if="canEdit && version.id !== currentVersionId"
          data-test="restore-version"
          type="button"
          :disabled="busy"
          @click="$emit('restore', version.id)"
        >回退到此版本</button>
      </li>
    </ol>
  </aside>
</template>

<style scoped>
.version-history { width: 290px; border-left: 1px solid #e5e7eb; padding-left: 18px; }
.history-note { color: #606266; font-size: 13px; }
ol { list-style: none; padding: 0; margin: 0; }
li { padding: 12px; border: 1px solid #ebeef5; border-radius: 8px; margin-bottom: 10px; }
li.current { border-color: #67c23a; background: #f0f9eb; }
li div { display: flex; justify-content: space-between; }
li span, time, li p { color: #606266; font-size: 12px; }
button { margin-top: 8px; }
</style>
