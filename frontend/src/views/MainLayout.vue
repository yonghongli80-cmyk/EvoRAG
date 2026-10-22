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
  if (route.path.startsWith('/wiki')) return 'wiki'
  if (route.path.startsWith('/operations')) return 'operations'
  if (route.path.startsWith('/analysis')) return 'analysis'
  if (route.path.startsWith('/organization')) return 'organization'
  return 'files'
})

function handleMenuSelect(index: string) {
  router.push(`/${index}`)
}

async function handleLogout() {
  try {
    await authStore.logout()
  } finally {
    // logout() already reset the client stores in its finally block; navigate
    // even if the server call failed so the next account always starts at /login.
    router.push('/login')
  }
}
</script>

<template>
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar">
      <!-- Brand -->
      <div class="sidebar-brand">
        <div class="brand-logo">
          <div class="logo-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
            </svg>
          </div>
          <div class="brand-info">
            <span class="logo-text">RAG Studio</span>
            <span class="logo-badge">v3 Enterprise</span>
          </div>
        </div>
      </div>

      <!-- Navigation -->
      <nav class="sidebar-nav">
        <button
          :class="['nav-item', { active: activeMenu === 'files' }]"
          @click="handleMenuSelect('files')"
          aria-label="文件管理"
        >
          <span class="nav-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
              <polyline points="10 9 9 9 8 9"/>
            </svg>
          </span>
          <span class="nav-label">文件</span>
        </button>

        <button
          :class="['nav-item', { active: activeMenu === 'kb' }]"
          @click="handleMenuSelect('kb')"
          aria-label="知识库管理"
        >
          <span class="nav-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
          </span>
          <span class="nav-label">知识库</span>
        </button>

        <button
          :class="['nav-item', { active: activeMenu === 'wiki' }]"
          data-test="wiki-nav"
          @click="handleMenuSelect('wiki')"
          aria-label="Wiki双链"
        >
          <span class="nav-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="18" cy="5" r="3"/>
              <circle cx="6" cy="12" r="3"/>
              <circle cx="18" cy="19" r="3"/>
              <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/>
              <line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
            </svg>
          </span>
          <span class="nav-label">Wiki双链</span>
        </button>

        <button
          :class="['nav-item', { active: activeMenu === 'operations' }]"
          @click="handleMenuSelect('operations')"
          aria-label="知识运营"
        >
          <span class="nav-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 20v-6M6 20V10M18 20V4"/>
            </svg>
          </span>
          <span class="nav-label">知识运营</span>
        </button>

        <button
          v-if="authStore.user?.is_admin"
          :class="['nav-item', { active: route.path === '/operations/spreadsheet-changes' }]"
          data-test="spreadsheet-audit-nav"
          @click="handleMenuSelect('operations/spreadsheet-changes')"
          aria-label="表格变更记录"
        >
          <span class="nav-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
              <line x1="3" y1="9" x2="21" y2="9"/>
              <line x1="3" y1="15" x2="21" y2="15"/>
              <line x1="9" y1="3" x2="9" y2="21"/>
              <line x1="15" y1="3" x2="15" y2="21"/>
            </svg>
          </span>
          <span class="nav-label">表格变更</span>
        </button>

        <button
          v-if="authStore.user?.is_admin"
          :class="['nav-item', { active: route.path === '/operations/evolution' }]"
          data-test="evolution-nav"
          @click="handleMenuSelect('operations/evolution')"
          aria-label="进化中心"
        >
          <span class="nav-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M2 12h5l3 5 4-10 3 5h5"/>
            </svg>
          </span>
          <span class="nav-label">进化中心</span>
        </button>

        <button
          v-if="authStore.user?.is_admin"
          :class="['nav-item', { active: activeMenu === 'organization' }]"
          @click="handleMenuSelect('organization')"
          aria-label="组织权限管理"
        >
          <span class="nav-icon">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
              <circle cx="9" cy="7" r="4"/>
              <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
              <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
            </svg>
          </span>
          <span class="nav-label">组织权限</span>
        </button>
      </nav>

      <!-- User footer -->
      <div class="sidebar-footer">
        <div class="user-card">
          <div class="user-avatar">{{ authStore.user?.profile?.display_name?.[0] || authStore.user?.username?.[0]?.toUpperCase() || 'U' }}</div>
          <div class="user-details">
            <div class="user-name">{{ authStore.user?.profile?.display_name || authStore.user?.username || '用户' }}</div>
            <div class="user-role">{{ authStore.user?.profile ? `${authStore.user.profile.job_title} · ${authStore.user.profile.primary_org?.name || ''}` : (authStore.user?.is_admin ? '管理员' : '标准用户') }}</div>
          </div>
          <div class="user-actions">
            <el-dropdown trigger="click" placement="top-end">
              <button class="action-btn" title="账户设置" aria-label="设置">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="3"/>
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
                </svg>
              </button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="router.push('/settings/password')">
                    <span style="display:flex;align-items:center;gap:8px;">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                      修改密码
                    </span>
                  </el-dropdown-item>
                  <el-dropdown-item divided @click="handleLogout">
                    <span style="display:flex;align-items:center;gap:8px;color:var(--color-destructive);">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
                      退出登录
                    </span>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
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
  transition: width var(--duration-normal) var(--ease-out);
  box-shadow: 1px 0 3px rgba(0, 0, 0, 0.02);
  z-index: 20;
}

.sidebar-brand {
  padding: 16px 18px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.brand-logo {
  display: flex;
  align-items: center;
  gap: 10px;
}

.logo-icon {
  width: 34px;
  height: 34px;
  background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
  color: #ffffff;
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 10px rgba(79, 70, 229, 0.28);
  flex-shrink: 0;
}

.brand-info {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.logo-text {
  font-family: var(--font-heading);
  font-size: 15px;
  font-weight: 700;
  color: var(--color-foreground);
  letter-spacing: -0.3px;
}

.logo-badge {
  font-size: 10px;
  font-weight: 600;
  color: var(--color-primary);
  text-transform: uppercase;
  letter-spacing: 0.6px;
}

/* ── Navigation ───────────────────────────────────────── */
.sidebar-nav {
  flex: 1;
  padding: 14px 10px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  overflow-y: auto;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: none;
  border: none;
  border-radius: var(--radius-md);
  color: var(--color-secondary);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  font-family: var(--font-body);
  font-size: 13.5px;
  font-weight: 500;
  width: 100%;
  text-align: left;
}

.nav-item:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
  transform: translateX(2px);
}

.nav-item.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-weight: 600;
}

.nav-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  transition: transform var(--duration-fast) var(--ease-out);
}

.nav-item:hover .nav-icon {
  transform: scale(1.08);
}

.nav-label {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── User Footer Card ─────────────────────────────────── */
.sidebar-footer {
  padding: 12px;
  border-top: 1px solid var(--color-border-subtle);
  background: var(--color-surface);
}

.user-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  background: var(--color-background);
  border: 1px solid var(--color-border-subtle);
  transition: all var(--duration-fast) var(--ease-out);
}

.user-card:hover {
  border-color: var(--color-border);
  box-shadow: var(--shadow-xs);
}

.user-avatar {
  width: 32px;
  height: 32px;
  background: linear-gradient(135deg, #e0e7ff 0%, #c7d2fe 100%);
  color: var(--color-primary);
  border-radius: var(--radius-full);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
  flex-shrink: 0;
}

.user-details {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.user-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-role {
  font-size: 11px;
  color: var(--color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-actions {
  display: flex;
  align-items: center;
}

.action-btn {
  width: 28px;
  height: 28px;
  background: none;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--color-muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--duration-fast) var(--ease-out);
}

.action-btn:hover {
  background: var(--color-surface-2);
  color: var(--color-foreground);
}

/* ── Main Workspace ───────────────────────────────────── */
.main {
  flex: 1;
  overflow: hidden;
  background: var(--color-background);
  position: relative;
}

/* ── Responsive ───────────────────────────────────────── */
@media (max-width: 768px) {
  .sidebar {
    width: 64px;
  }

  .sidebar-brand {
    padding: 14px 10px;
    display: flex;
    justify-content: center;
  }

  .brand-info {
    display: none;
  }

  .nav-label {
    display: none;
  }

  .nav-item {
    justify-content: center;
    padding: 10px;
  }

  .user-details {
    display: none;
  }

  .user-card {
    justify-content: center;
    padding: 6px;
  }
}
</style>
