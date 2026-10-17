<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import api from '../utils/api'
import { ElMessage } from 'element-plus'

const router = useRouter()
const authStore = useAuthStore()

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const loading = ref(false)

// Password strength
const strength = computed(() => {
  const p = newPassword.value
  if (!p) return { level: 0, label: '', color: '' }
  let score = 0
  if (p.length >= 8) score++
  if (/[A-Z]/.test(p)) score++
  if (/[a-z]/.test(p)) score++
  if (/[0-9]/.test(p)) score++
  if (score <= 1) return { level: 1, label: '弱', color: '#ef4444' }
  if (score <= 2) return { level: 2, label: '中', color: '#f59e0b' }
  if (score <= 3) return { level: 3, label: '强', color: '#22c55e' }
  return { level: 4, label: '非常强', color: '#10b981' }
})

// Validation
const errors = computed(() => {
  const errs: string[] = []
  if (newPassword.value && newPassword.value.length < 8) errs.push('至少 8 位')
  if (newPassword.value && !/[A-Z]/.test(newPassword.value)) errs.push('需含大写字母')
  if (newPassword.value && !/[a-z]/.test(newPassword.value)) errs.push('需含小写字母')
  if (newPassword.value && !/[0-9]/.test(newPassword.value)) errs.push('需含数字')
  if (confirmPassword.value && newPassword.value !== confirmPassword.value) errs.push('两次密码不一致')
  return errs
})

const canSubmit = computed(() => {
  return oldPassword.value && newPassword.value && confirmPassword.value && errors.value.length === 0
})

async function handleSubmit() {
  if (!canSubmit.value) return
  loading.value = true
  try {
    await api.put('/users/me/password', {
      old_password: oldPassword.value,
      new_password: newPassword.value,
      confirm_password: confirmPassword.value
    })
    ElMessage.success('密码已修改，请重新登录')
    authStore.logout()
    router.push('/login')
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '修改失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="change-password-page">
    <div class="change-password-card">
      <h2 class="card-title">修改密码</h2>
      <p class="card-subtitle">修改密码后需要重新登录</p>

      <form @submit.prevent="handleSubmit" class="form">
        <div class="form-group">
          <label class="form-label">旧密码</label>
          <el-input
            v-model="oldPassword"
            type="password"
            placeholder="输入当前密码"
            show-password
          />
        </div>

        <div class="form-group">
          <label class="form-label">新密码</label>
          <el-input
            v-model="newPassword"
            type="password"
            placeholder="输入新密码"
            show-password
          />
          <div v-if="newPassword" class="strength-bar">
            <div class="strength-fill" :style="{ width: (strength.level * 25) + '%', background: strength.color }"></div>
          </div>
          <span v-if="newPassword" class="strength-label" :style="{ color: strength.color }">{{ strength.label }}</span>
        </div>

        <div class="form-group">
          <label class="form-label">确认新密码</label>
          <el-input
            v-model="confirmPassword"
            type="password"
            placeholder="再次输入新密码"
            show-password
          />
        </div>

        <div v-if="errors.length" class="errors">
          <p v-for="err in errors" :key="err" class="error-item">• {{ err }}</p>
        </div>

        <el-button
          type="primary"
          :loading="loading"
          :disabled="!canSubmit"
          @click="handleSubmit"
          class="submit-btn"
        >
          修改密码
        </el-button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.change-password-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: var(--space-8);
  background: var(--color-background);
}

.change-password-card {
  width: 100%;
  max-width: 420px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: 32px;
  box-shadow: var(--shadow-card);
}

.card-title {
  font-family: var(--font-heading);
  font-size: var(--text-2xl);
  font-weight: var(--font-bold);
  color: var(--color-foreground);
  margin: 0 0 var(--space-2);
}

.card-subtitle {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  margin: 0 0 var(--space-6);
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.form-label {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
}

.strength-bar {
  height: 4px;
  background: var(--color-muted);
  border-radius: 2px;
  overflow: hidden;
}

.strength-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.3s, background 0.3s;
}

.strength-label {
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
}

.errors {
  padding: var(--space-3);
  background: var(--color-destructive-light);
  border: 1px solid var(--color-destructive);
  border-radius: var(--radius);
}

.error-item {
  font-size: var(--text-xs);
  color: var(--color-destructive);
  margin: 0;
}

.submit-btn {
  width: 100%;
  height: 44px;
  margin-top: var(--space-2);
}
</style>
