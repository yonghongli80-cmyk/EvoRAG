import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { requiresAuth: false }
    },
    {
      path: '/',
      component: () => import('../views/MainLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          redirect: '/files'
        },
        {
          path: 'files',
          name: 'files',
          component: () => import('../views/FileModeView.vue')
        },
        {
          path: 'kb',
          name: 'kb',
          component: () => import('../views/KBModeView.vue')
        },
        {
          path: 'kb/:id',
          name: 'kb-detail',
          component: () => import('../views/KnowledgeDetailView.vue')
        },
        {
          path: 'documents/:documentId/versions/:versionId/spreadsheet',
          name: 'spreadsheet-viewer',
          component: () => import('../views/SpreadsheetViewer.vue')
        },
        {
          path: 'operations',
          name: 'knowledge-operations',
          component: () => import('../views/KnowledgeOperationsView.vue')
        },
        {
          path: 'operations/spreadsheet-changes',
          name: 'spreadsheet-changes',
          component: () => import('../views/SpreadsheetChangesView.vue')
        },
        {
          path: 'analysis',
          name: 'analysis',
          component: () => import('../views/AnalysisModeView.vue')
        },
        {
          path: 'organization',
          name: 'organization-admin',
          component: () => import('../views/OrganizationAdminView.vue')
        },
        {
          path: 'settings/password',
          name: 'change-password',
          component: () => import('../views/ChangePasswordView.vue')
        }
      ]
    }
  ]
})

// Navigation guard
router.beforeEach((to, _from, next) => {
  const authStore = useAuthStore()

  if (to.meta.requiresAuth !== false && !authStore.isAuthenticated) {
    next('/login')
  } else if (to.name === 'login' && authStore.isAuthenticated) {
    next('/')
  } else {
    next()
  }
})

export default router
