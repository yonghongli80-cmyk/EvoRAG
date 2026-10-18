<script setup lang="ts">
defineProps<{
  backlinks: Array<{ id: number; title: string }>
  outgoingLinks: Array<{ target_title: string; target_page_id: number | null }>
}>()

const emit = defineEmits<{
  (e: 'select-page', id: number): void
  (e: 'wikilink-click', targetTitle: string): void
}>()
</script>

<template>
  <div class="dual-links-panel">
    <!-- Backlinks Card -->
    <div class="links-card">
      <div class="links-card-title">
        <span class="icon">↶</span>
        <strong>反向链接 (Backlinks)</strong>
        <span class="badge">{{ backlinks.length }}</span>
      </div>
      <p class="links-desc">引用了本页面的其他 Wiki 词条：</p>
      <div v-if="backlinks.length > 0" class="links-list">
        <button
          v-for="b in backlinks"
          :key="b.id"
          class="link-item"
          type="button"
          @click="emit('select-page', b.id)"
        >
          <span class="link-icon">📄</span>
          <span class="link-title">{{ b.title }}</span>
          <span class="arrow">→</span>
        </button>
      </div>
      <div v-else class="empty-links">暂无其他页面引用此词条</div>
    </div>

    <!-- Outgoing Links Card -->
    <div class="links-card">
      <div class="links-card-title">
        <span class="icon">↷</span>
        <strong>出链引用 (Outgoing Links)</strong>
        <span class="badge">{{ outgoingLinks.length }}</span>
      </div>
      <p class="links-desc">本页面中引用的外部 Wiki 链接：</p>
      <div v-if="outgoingLinks.length > 0" class="links-list">
        <button
          v-for="out in outgoingLinks"
          :key="out.target_title"
          class="link-item"
          type="button"
          @click="emit('wikilink-click', out.target_title)"
        >
          <span class="link-icon">🔗</span>
          <span class="link-title">{{ out.target_title }}</span>
          <el-tag
            size="small"
            :type="out.target_page_id ? 'success' : 'info'"
            class="status-tag"
          >
            {{ out.target_page_id ? '已关联' : '待创建' }}
          </el-tag>
        </button>
      </div>
      <div v-else class="empty-links">本页暂无引出双链语法</div>
    </div>
  </div>
</template>

<style scoped>
.dual-links-panel {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  padding-top: 20px;
  border-top: 1px solid var(--color-border);
}

.links-card {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 16px;
  background: var(--color-surface-2);
}

.links-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
}

.links-card-title .icon {
  font-size: 16px;
}

.links-card-title .badge {
  background: var(--color-primary);
  color: #fff;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 10px;
}

.links-desc {
  font-size: 12px;
  color: var(--color-secondary);
  margin: 6px 0 12px;
}

.links-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.link-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  cursor: pointer;
  text-align: left;
  transition: all 0.15s ease;
}

.link-item:hover {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.link-icon {
  margin-right: 8px;
}

.link-title {
  flex: 1;
  font-size: 13px;
  font-weight: 500;
}

.arrow {
  color: var(--color-secondary);
  font-size: 13px;
}

.status-tag {
  margin-left: 8px;
}

.empty-links {
  font-size: 13px;
  color: var(--color-muted);
  text-align: center;
  padding: 16px 0;
}
</style>
