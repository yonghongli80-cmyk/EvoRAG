<script setup lang="ts">
import { ref } from 'vue'
import api from '../utils/api'
import type { LoginAccount } from '../api/organization'

defineProps<{ modelValue: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const query = ref('')
const accounts = ref<LoginAccount[]>([])
const loading = ref(false)

async function search() {
  loading.value = true
  try {
    accounts.value = (await api.get('/login/accounts', { params: { q: query.value } })).data
  } finally {
    loading.value = false
  }
}

function select(account: LoginAccount) {
  emit('update:modelValue', account.username)
  accounts.value = []
  query.value = `${account.display_name} · ${account.department || ''}`
}
</script>

<template>
  <div class="account-picker">
    <input v-model="query" data-test="account-search" class="picker-input" placeholder="按姓名、编号、部门或职位查找" @input="search" />
    <div v-if="accounts.length" class="account-results">
      <button v-for="account in accounts" :key="account.username" :data-test="`account-${account.username}`" type="button" @click="select(account)">
        <strong>{{ account.display_name }}</strong>
        <span>{{ account.username }} · {{ account.job_title }} · {{ account.department }}</span>
      </button>
    </div>
    <span v-if="loading" class="picker-loading">正在查找…</span>
  </div>
</template>

<style scoped>
.account-picker{position:relative}.picker-input{width:100%;padding:10px 12px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-surface)}
.account-results{position:absolute;z-index:20;left:0;right:0;max-height:240px;overflow:auto;background:var(--color-surface);border:1px solid var(--color-border);border-radius:8px;box-shadow:var(--shadow-lg)}
.account-results button{display:flex;width:100%;flex-direction:column;gap:3px;padding:10px 12px;text-align:left;border:0;border-bottom:1px solid var(--color-border);background:transparent;cursor:pointer}.account-results button:hover{background:var(--color-surface-2)}
.account-results span,.picker-loading{font-size:12px;color:var(--color-secondary)}
</style>
