<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

onMounted(() => {
  authStore.fetchUser()
})

const activeMenu = computed(() => {
  if (route.path.startsWith('/files')) return 'files'
  if (route.path.startsWith('/kb')) return 'kb'
  if (route.path.startsWith('/operations')) return 'operations'
  if (route.path.startsWith('/analysis')) return 'analysis'
  return 'files'
})

function handleMenuSelect(index: string) {
  router.push(`/${index}`)
}

function handleLogout() {
  authStore.logout()
  router.push('/login')
}
</script>

<template>
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar">
      <!-- Brand -->
      <div class="sidebar-brand">
        <div class="brand-logo">
          <div class="logo-icon">R</div>
          <span class="logo-text">RAG</span>
        </div>
      </div>

      <!-- Navigation -->
      <nav class="sidebar-nav">
        <button
          :class="['nav-item', { active: activeMenu === 'files' }]"
          @click="handleMenuSelect('files')"
          aria-label="文件管理"
        >
          <span class="nav-icon">📄</span>
          <span class="nav-label">文件</span>
        </button>
        <button
          :class="['nav-item', { active: activeMenu === 'kb' }]"
          @click="handleMenuSelect('kb')"
          aria-label="知识库管理"
        >
          <span class="nav-icon">📚</span>
          <span class="nav-label">知识库</span>
        </button>
        <button
          :class="['nav-item', { active: activeMenu === 'operations' }]"
          @click="handleMenuSelect('operations')"
          aria-label="知识运营"
        >
          <span class="nav-icon">🛠️</span>
          <span class="nav-label">知识运营</span>
        </button>
      </nav>

      <!-- User -->
      <div class="sidebar-footer">
        <div class="user-info">
          <div class="user-avatar">{{ authStore.user?.username?.[0]?.toUpperCase() || 'U' }}</div>
          <div class="user-details">
            <div class="user-name">{{ authStore.user?.username || '用户' }}</div>
            <div class="user-role">{{ authStore.user?.is_admin ? '管理员' : '用户' }}</div>
          </div>
        </div>
        <div class="user-actions">
          <el-dropdown trigger="click">
            <button class="action-btn" title="设置">
              ⚙️
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="router.push('/settings/password')">修改密码</el-dropdown-item>
                <el-dropdown-item divided @click="handleLogout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
    </aside>

    <!-- Main Content -->
    <main class="main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--color-background);
}

/* ── Sidebar ──────────────────────────────────────────── */
.sidebar {
  width: var(--sidebar-width);
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.sidebar-brand {
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.brand-logo {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.logo-icon {
  width: 32px;
  height: 32px;
  background: var(--color-primary);
  color: var(--color-on-primary);
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 14px;
  font-weight: var(--font-bold);
}

.logo-text {
  font-family: var(--font-heading);
  font-size: var(--text-lg);
  font-weight: var(--font-bold);
  color: var(--color-foreground);
}

/* ── Navigation ───────────────────────────────────────── */
.sidebar-nav {
  flex: 1;
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.nav-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  background: none;
  border: none;
  border-radius: var(--radius);
  color: var(--color-secondary);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  font-family: var(--font-body);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  width: 100%;
  text-align: left;
}

.nav-item:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

.nav-item.active {
  background: var(--color-accent-light);
  color: var(--color-primary);
}

.nav-icon {
  font-size: 16px;
  width: 20px;
  text-align: center;
}

.nav-label {
  flex: 1;
}

/* ── Footer ───────────────────────────────────────────── */
.sidebar-footer {
  padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--color-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.user-info {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.user-avatar {
  width: 32px;
  height: 32px;
  background: var(--color-surface-2);
  color: var(--color-foreground);
  border-radius: var(--radius-full);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
}

.user-details {
  display: flex;
  flex-direction: column;
}

.user-name {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
}

.user-role {
  font-size: var(--text-xs);
  color: var(--color-secondary);
}

.user-actions {
  display: flex;
  gap: var(--space-1);
}

.action-btn {
  width: 28px;
  height: 28px;
  background: none;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--color-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  transition: all var(--duration-fast) var(--ease-out);
}

.action-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

/* ── Main Content ─────────────────────────────────────── */
.main {
  flex: 1;
  overflow: hidden;
  background: var(--color-background);
}

/* ── Responsive ───────────────────────────────────────── */
@media (max-width: 768px) {
  .sidebar {
    width: 60px;
  }

  .sidebar-brand {
    padding: var(--space-3);
    display: flex;
    justify-content: center;
  }

  .logo-text {
    display: none;
  }

  .nav-label {
    display: none;
  }

  .nav-item {
    justify-content: center;
    padding: var(--space-3);
  }

  .sidebar-footer {
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-3);
  }

  .user-details {
    display: none;
  }

  .user-actions {
    flex-direction: column;
  }
}
</style>
