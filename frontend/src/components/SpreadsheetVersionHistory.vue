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
    <div class="history-heading">
      <div>
        <p>VERSION HISTORY</p>
        <h3>版本历史</h3>
      </div>
      <span>{{ versions.length }} 个版本</span>
    </div>
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
          class="restore-button"
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
.version-history { width: 310px; flex-shrink: 0; padding: var(--space-5); overflow: auto; border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: var(--color-surface); box-shadow: var(--shadow-xs); }
.history-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-3); }
.history-heading p { margin: 0 0 2px; color: var(--color-primary); font-size: 10px; font-weight: var(--font-bold); letter-spacing: .08em; }
.history-heading h3 { margin: 0; color: var(--color-foreground); font-size: var(--text-lg); }
.history-heading > span { padding: 3px 8px; border-radius: var(--radius-full); color: var(--color-secondary); background: var(--color-surface-2); font-size: var(--text-xs); }
.history-note { margin: var(--space-3) 0 var(--space-4); color: var(--color-secondary); font-size: var(--text-xs); line-height: var(--leading-relaxed); }
ol { list-style: none; padding: 0; margin: 0; }
li { position: relative; padding: var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius); margin-bottom: var(--space-3); background: var(--color-surface); }
li.current { border-color: #a7f3d0; background: var(--color-success-light); }
li.current::before { content: ''; position: absolute; inset: 12px auto 12px -1px; width: 3px; border-radius: var(--radius-full); background: var(--color-success); }
li div { display: flex; justify-content: space-between; gap: var(--space-2); }
li strong { color: var(--color-foreground); }
li span, time, li p { color: var(--color-secondary); font-size: var(--text-xs); }
time { display: block; margin-top: var(--space-1); }
li p { margin: var(--space-2) 0 0; }
.restore-button { width: 100%; min-height: 32px; margin-top: var(--space-3); border: 1px solid var(--color-border); border-radius: var(--radius); color: var(--color-primary); background: var(--color-surface); font: inherit; font-size: var(--text-xs); font-weight: var(--font-medium); cursor: pointer; }
.restore-button:hover { border-color: var(--color-primary); background: var(--color-accent-light); }
.restore-button:disabled { opacity: .5; cursor: not-allowed; }

@media (max-width: 960px) { .version-history { width: 100%; max-height: 340px; } }
</style>
