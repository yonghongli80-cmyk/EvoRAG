<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ElMessage } from 'element-plus'

const router = useRouter()
const authStore = useAuthStore()

const username = ref('')
const password = ref('')
const loading = ref(false)

async function handleSubmit() {
  if (!username.value || !password.value) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  loading.value = true
  try {
    await authStore.login(username.value, password.value)
    router.push('/')
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <!-- Ambient glowing backgrounds -->
    <div class="bg-decoration">
      <div class="bg-glow bg-glow-1"></div>
      <div class="bg-glow bg-glow-2"></div>
      <div class="bg-glow bg-glow-3"></div>
      <div class="bg-grid-pattern"></div>
    </div>

    <div class="login-container">
      <!-- Brand -->
      <div class="brand">
        <div class="brand-logo">
          <div class="logo-mark">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
            </svg>
          </div>
          <div class="brand-identity">
            <div class="brand-name-row">
              <span class="logo-text">RAG Studio</span>
              <span class="version-badge">v3.0</span>
            </div>
            <span class="brand-meta">Enterprise Edition</span>
          </div>
        </div>
        <h1 class="brand-title">企业智能协同知识库</h1>
        <p class="brand-subtitle">检索增强生成 · 多模型对齐 · 组织级数据资产治理</p>
      </div>

      <!-- Form Card -->
      <div class="form-card">
        <div class="card-header">
          <h2 class="form-title">系统登录</h2>
          <p class="form-desc">请输入您的组织统一认证账号</p>
        </div>

        <form @submit.prevent="handleSubmit" class="form">
          <div class="field">
            <label class="label">
              <span>姓名或账号编号</span>
            </label>
            <div class="input-wrapper">
              <span class="input-icon">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
                  <circle cx="12" cy="7" r="4"></circle>
                </svg>
              </span>
              <input
                v-model="username"
                type="text"
                class="input"
                placeholder="例如：demo001 或 您的姓名"
                autocomplete="username"
              />
            </div>
          </div>

          <div class="field">
            <label class="label">
              <span>密码</span>
            </label>
            <div class="input-wrapper">
              <span class="input-icon">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                </svg>
              </span>
              <input
                v-model="password"
                type="password"
                class="input"
                placeholder="输入组织账户密码"
                autocomplete="current-password"
              />
            </div>
          </div>

          <button type="submit" class="submit-btn" :disabled="loading">
            <span v-if="loading" class="spinner"></span>
            <span>{{ loading ? '验证凭证中...' : '登录平台' }}</span>
          </button>
        </form>

        <div class="form-footer">
          <div class="footer-badge">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
            </svg>
            <span>企业数据加密传输与访问控制保护</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-background, #f8fafc);
  padding: var(--space-4);
  position: relative;
  overflow: hidden;
  font-family: var(--font-body);
}

/* ── Ambient Background ──────────────────────────────── */
.bg-decoration {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  overflow: hidden;
}

.bg-grid-pattern {
  position: absolute;
  inset: 0;
  background-image: radial-gradient(rgba(148, 163, 184, 0.15) 1px, transparent 1px);
  background-size: 24px 24px;
}

.bg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  opacity: 0.45;
  transition: all 1s ease;
}

.bg-glow-1 {
  width: 480px;
  height: 480px;
  background: radial-gradient(circle, rgba(99, 102, 241, 0.35) 0%, rgba(79, 70, 229, 0) 70%);
  top: -120px;
  right: -80px;
  animation: float 14s ease-in-out infinite alternate;
}

.bg-glow-2 {
  width: 420px;
  height: 420px;
  background: radial-gradient(circle, rgba(14, 165, 233, 0.25) 0%, rgba(56, 189, 248, 0) 70%);
  bottom: -100px;
  left: -80px;
  animation: float 16s ease-in-out infinite alternate-reverse;
}

.bg-glow-3 {
  width: 320px;
  height: 320px;
  background: radial-gradient(circle, rgba(168, 85, 247, 0.2) 0%, rgba(139, 92, 246, 0) 70%);
  top: 40%;
  left: 20%;
  animation: float 18s ease-in-out infinite alternate;
}

@keyframes float {
  0% { transform: translate(0, 0) scale(1); }
  50% { transform: translate(30px, -20px) scale(1.05); }
  100% { transform: translate(-20px, 20px) scale(0.98); }
}

/* ── Container ──────────────────────────────────────── */
.login-container {
  width: 100%;
  max-width: 420px;
  position: relative;
  z-index: 1;
  animation: slideUp 0.4s var(--ease-out, cubic-bezier(0.16, 1, 0.3, 1));
}

/* ── Brand ──────────────────────────────────────────── */
.brand {
  text-align: center;
  margin-bottom: var(--space-6, 24px);
}

.brand-logo {
  display: inline-flex;
  align-items: center;
  gap: var(--space-3, 12px);
  margin-bottom: var(--space-3, 12px);
  padding: 6px 14px 6px 8px;
  background: rgba(255, 255, 255, 0.8);
  border: 1px solid rgba(226, 232, 240, 0.8);
  border-radius: var(--radius-full, 9999px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
  backdrop-filter: blur(8px);
}

.logo-mark {
  width: 36px;
  height: 36px;
  background: linear-gradient(135deg, #6366f1 0%, #4338ca 100%);
  color: #ffffff;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
}

.brand-identity {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  text-align: left;
}

.brand-name-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.logo-text {
  font-family: var(--font-heading);
  font-size: 15px;
  font-weight: 700;
  color: var(--color-foreground, #0f172a);
  letter-spacing: -0.02em;
}

.version-badge {
  font-size: 10px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(79, 70, 229, 0.1);
  color: var(--color-primary, #4f46e5);
  font-family: var(--font-mono, monospace);
}

.brand-meta {
  font-size: 11px;
  color: var(--color-secondary, #64748b);
  letter-spacing: 0.02em;
}

.brand-title {
  font-family: var(--font-heading);
  font-size: 24px;
  font-weight: 700;
  color: var(--color-foreground, #0f172a);
  letter-spacing: -0.03em;
  margin: 0 0 var(--space-2, 8px);
}

.brand-subtitle {
  font-size: 13px;
  color: var(--color-secondary, #64748b);
  margin: 0;
  line-height: 1.5;
}

/* ── Form Card ──────────────────────────────────────── */
.form-card {
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(226, 232, 240, 0.9);
  border-radius: var(--radius-xl, 16px);
  padding: 32px;
  box-shadow: 0 20px 40px -15px rgba(15, 23, 42, 0.08), 0 0 1px 1px rgba(255, 255, 255, 0.6) inset;
}

.card-header {
  margin-bottom: var(--space-6, 24px);
  text-align: center;
}

.form-title {
  margin: 0 0 4px;
  font-size: 18px;
  font-weight: 600;
  color: var(--color-foreground, #0f172a);
  letter-spacing: -0.02em;
}

.form-desc {
  font-size: 12px;
  color: var(--color-secondary, #64748b);
  margin: 0;
}

/* ── Form Fields ────────────────────────────────────── */
.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4, 16px);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.label {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-foreground, #334155);
}

.input-wrapper {
  position: relative;
  display: flex;
  align-items: center;
}

.input-icon {
  position: absolute;
  left: 12px;
  color: var(--color-muted, #94a3b8);
  display: flex;
  align-items: center;
  pointer-events: none;
}

.input {
  width: 100%;
  padding: 10px 14px 10px 38px;
  background: var(--color-surface, #ffffff);
  border: 1px solid var(--color-border, #cbd5e1);
  border-radius: var(--radius-md, 8px);
  color: var(--color-foreground, #0f172a);
  font-family: var(--font-body);
  font-size: 14px;
  outline: none;
  transition: all var(--duration-fast, 150ms) var(--ease-out, ease-out);
}

.input:focus {
  border-color: var(--color-primary, #4f46e5);
  box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12);
}

.input::placeholder {
  color: #94a3b8;
  font-size: 13px;
}

.submit-btn {
  width: 100%;
  padding: 11px 16px;
  background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%);
  color: #ffffff;
  border: none;
  border-radius: var(--radius-md, 8px);
  font-family: var(--font-body);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: all var(--duration-fast, 150ms) var(--ease-out, ease-out);
  margin-top: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  box-shadow: 0 4px 12px rgba(79, 70, 229, 0.25);
}

.submit-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(79, 70, 229, 0.35);
  background: linear-gradient(135deg, #4338ca 0%, #3730a3 100%);
}

.submit-btn:active:not(:disabled) {
  transform: translateY(0);
}

.submit-btn:disabled {
  opacity: 0.65;
  cursor: not-allowed;
  transform: none;
}

.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.4);
  border-top-color: #ffffff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.form-footer {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--color-border-subtle, #f1f5f9);
  text-align: center;
}

.footer-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--color-secondary, #64748b);
}

/* ── Responsive ──────────────────────────────────────── */
@media (max-width: 480px) {
  .form-card {
    padding: 24px 20px;
  }
}
</style>
