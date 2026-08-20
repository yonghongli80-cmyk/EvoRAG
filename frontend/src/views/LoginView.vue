<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ElMessage } from 'element-plus'
import AccountPicker from '../components/AccountPicker.vue'

const router = useRouter()
const authStore = useAuthStore()

const isLogin = ref(true)
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
    if (isLogin.value) {
      await authStore.login(username.value, password.value)
    } else {
      await authStore.register(username.value, password.value)
    }
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
    <!-- Background decoration -->
    <div class="bg-decoration">
      <div class="bg-circle bg-circle-1"></div>
      <div class="bg-circle bg-circle-2"></div>
      <div class="bg-circle bg-circle-3"></div>
    </div>

    <div class="login-container">
      <!-- Brand -->
      <div class="brand">
        <div class="brand-logo">
          <div class="logo-icon">R</div>
          <span class="logo-text">RAG</span>
        </div>
        <h1 class="brand-title">智能知识库</h1>
        <p class="brand-subtitle">基于检索增强生成的企业级知识库系统</p>
      </div>

      <!-- Form -->
      <div class="form-card">
        <div class="tabs">
          <button :class="['tab', { active: isLogin }]" @click="isLogin = true">登录</button>
          <button :class="['tab', { active: !isLogin }]" @click="isLogin = false">注册</button>
        </div>

        <form @submit.prevent="handleSubmit" class="form">
          <div v-if="isLogin" class="field">
            <label class="label">选择员工</label>
            <AccountPicker v-model="username" />
          </div>
          <div class="field">
            <label class="label">用户名</label>
            <input
              v-model="username"
              type="text"
              class="input"
              placeholder="输入用户名"
              autocomplete="username"
            />
          </div>
          <div class="field">
            <label class="label">密码</label>
            <input
              v-model="password"
              type="password"
              class="input"
              placeholder="输入密码"
              :autocomplete="isLogin ? 'current-password' : 'new-password'"
            />
          </div>
          <button type="submit" class="submit-btn" :disabled="loading">
            {{ loading ? '处理中...' : (isLogin ? '登录' : '注册') }}
          </button>
        </form>
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
  background: var(--color-background);
  padding: var(--space-4);
  position: relative;
  overflow: hidden;
}

/* ── Background decoration ──────────────────────────── */
.bg-decoration {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 0;
}

.bg-circle {
  position: absolute;
  border-radius: 50%;
  opacity: 0.08;
}

.bg-circle-1 {
  width: 400px;
  height: 400px;
  background: var(--color-primary);
  top: -100px;
  right: -100px;
  animation: float 8s ease-in-out infinite;
}

.bg-circle-2 {
  width: 300px;
  height: 300px;
  background: var(--color-success);
  bottom: -80px;
  left: -80px;
  animation: float 10s ease-in-out infinite reverse;
}

.bg-circle-3 {
  width: 200px;
  height: 200px;
  background: var(--color-warning);
  top: 50%;
  left: 10%;
  animation: float 12s ease-in-out infinite;
}

@keyframes float {
  0%, 100% { transform: translate(0, 0); }
  50% { transform: translate(20px, -20px); }
}

/* ── Container ──────────────────────────────────────── */
.login-container {
  width: 100%;
  max-width: 400px;
  position: relative;
  z-index: 1;
  animation: slideUp 0.5s var(--ease-out);
}

/* ── Brand ──────────────────────────────────────────── */
.brand {
  text-align: center;
  margin-bottom: var(--space-8);
}

.brand-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}

.logo-icon {
  width: 40px;
  height: 40px;
  background: var(--color-primary);
  color: var(--color-on-primary);
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 18px;
  font-weight: var(--font-bold);
}

.logo-text {
  font-family: var(--font-heading);
  font-size: var(--text-xl);
  font-weight: var(--font-bold);
  color: var(--color-foreground);
}

.brand-title {
  font-family: var(--font-heading);
  font-size: var(--text-2xl);
  font-weight: var(--font-bold);
  color: var(--color-foreground);
  margin: 0 0 var(--space-2);
}

.brand-subtitle {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  margin: 0;
}

/* ── Form Card ──────────────────────────────────────── */
.form-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--space-8);
  box-shadow: var(--shadow-lg);
}

/* ── Tabs ────────────────────────────────────────────── */
.tabs {
  display: flex;
  gap: var(--space-1);
  margin-bottom: var(--space-6);
  background: var(--color-surface-2);
  border-radius: var(--radius);
  padding: 3px;
}

.tab {
  flex: 1;
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-secondary);
  background: none;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  font-family: var(--font-body);
}

.tab.active {
  background: var(--color-surface);
  color: var(--color-foreground);
  box-shadow: var(--shadow-xs);
}

/* ── Form ────────────────────────────────────────────── */
.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.label {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
}

.input {
  width: 100%;
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  font-family: var(--font-body);
  font-size: var(--text-base);
  outline: none;
  transition: all var(--duration-fast) var(--ease-out);
}

.input:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-accent-light);
}

.input::placeholder {
  color: var(--color-muted);
}

.submit-btn {
  width: 100%;
  padding: var(--space-3) var(--space-4);
  background: var(--color-primary);
  color: var(--color-on-primary);
  border: none;
  border-radius: var(--radius);
  font-family: var(--font-body);
  font-size: var(--text-base);
  font-weight: var(--font-semibold);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  margin-top: var(--space-2);
}

.submit-btn:hover:not(:disabled) {
  background: var(--color-primary-hover);
  transform: translateY(-1px);
  box-shadow: var(--shadow-sm);
}

.submit-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* ── Responsive ──────────────────────────────────────── */
@media (max-width: 480px) {
  .form-card {
    padding: var(--space-6);
  }
}
</style>
