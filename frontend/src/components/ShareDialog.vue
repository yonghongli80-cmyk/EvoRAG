<template>
  <el-dialog v-model="visible" :title="`共享：${itemName}`" width="480px">
    <!-- 可见范围 -->
    <div class="share-section">
      <h4>可见范围</h4>
      <el-radio-group v-model="localScope" @change="updateScope">
        <el-radio value="private">私有（仅自己可见）</el-radio>
        <el-radio value="shared">共享（指定用户可见）</el-radio>
        <el-radio value="public">公开（所有人可见）</el-radio>
      </el-radio-group>
    </div>

    <!-- 已共享用户（仅 shared 模式显示） -->
    <div v-if="localScope === 'shared'" class="share-section" style="margin-top: 20px;">
      <h4>已共享用户</h4>
      <div v-if="sharedUsers.length === 0" class="empty-hint">暂无共享用户</div>
      <div v-for="u in sharedUsers" :key="u.id" class="shared-user-row">
        <span class="shared-username">{{ u.username }}</span>
        <el-select v-model="u.permission" size="small" style="width: 100px;" @change="updatePermission(u)">
          <el-option value="view" label="可查看" />
          <el-option value="edit" label="可编辑" />
        </el-select>
        <el-button size="small" type="danger" text @click="unshare(u.id)">移除</el-button>
      </div>
    </div>

    <!-- 添加共享用户（仅 shared 模式显示） -->
    <div v-if="localScope === 'shared'" class="share-section" style="margin-top: 20px;">
      <h4>添加共享</h4>
      <div style="display: flex; gap: 8px;">
        <el-input
          v-model="searchQuery"
          placeholder="搜索用户名..."
          @input="onSearch"
          clearable
        />
      </div>
      <div v-if="searchResults.length > 0" class="search-results">
        <div
          v-for="u in searchResults"
          :key="u.id"
          class="search-result-item"
          @click="addShare(u)"
        >
          <span>{{ u.username }}</span>
          <el-icon><Plus /></el-icon>
        </div>
      </div>
      <div v-if="searchQuery.length >= 2 && searchResults.length === 0 && !searching" class="empty-hint">
        未找到匹配用户
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import api from '../utils/api'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'

interface SharedUser {
  id: number
  username: string
  permission: string
}

interface SearchResult {
  id: number
  username: string
}

const props = defineProps<{
  itemId: string | null  // kb_id 或 doc_id
  itemName: string
  itemType: 'kb' | 'doc'  // 知识库或文档
}>()

const visible = defineModel<boolean>('visible')

const localScope = ref('private')
const sharedUsers = ref<SharedUser[]>([])
const searchQuery = ref('')
const searchResults = ref<SearchResult[]>([])
const searching = ref(false)

watch(visible, async (v) => {
  if (v && props.itemId) {
    await loadScope()
    if (localScope.value === 'shared') {
      await loadSharedUsers()
    }
  }
})

async function loadScope() {
  try {
    if (props.itemType === 'kb') {
      const res = await api.get(`/knowledge-bases/${props.itemId}`)
      localScope.value = res.data.scope || 'private'
    }
    // 文档 scope 暂不支持单独切换
  } catch (err: any) {
    console.error('加载 scope 失败:', err)
  }
}

async function loadSharedUsers() {
  try {
    const endpoint = props.itemType === 'kb'
      ? `/knowledge-bases/${props.itemId}/shares`
      : `/documents/${props.itemId}/shares`
    const res = await api.get(endpoint)
    sharedUsers.value = res.data || []
  } catch (err: any) {
    console.error('加载共享列表失败:', err)
  }
}

async function updateScope(newScope: string) {
  try {
    if (props.itemType === 'kb') {
      await api.put(`/knowledge-bases/${props.itemId}/scope`, { scope: newScope })
    }
    ElMessage.success('可见范围已更新')
    if (newScope === 'shared') {
      await loadSharedUsers()
    }
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '更新失败')
  }
}

let searchTimer: ReturnType<typeof setTimeout> | null = null

function onSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  if (searchQuery.value.length < 2) {
    searchResults.value = []
    return
  }
  searchTimer = setTimeout(async () => {
    searching.value = true
    try {
      const res = await api.get('/users', {
        params: { q: searchQuery.value }
      })
      searchResults.value = res.data || []
    } catch (err: any) {
      console.error('搜索用户失败:', err)
    } finally {
      searching.value = false
    }
  }, 300)
}

async function addShare(user: SearchResult) {
  try {
    const endpoint = props.itemType === 'kb'
      ? `/knowledge-bases/${props.itemId}/share`
      : `/documents/${props.itemId}/share`
    await api.post(endpoint, { user_id: user.id, permission: 'view' })
    ElMessage.success(`已共享给 ${user.username}`)
    searchQuery.value = ''
    searchResults.value = []
    await loadSharedUsers()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '共享失败')
  }
}

async function unshare(userId: number) {
  try {
    const endpoint = props.itemType === 'kb'
      ? `/knowledge-bases/${props.itemId}/share/${userId}`
      : `/documents/${props.itemId}/share/${userId}`
    await api.delete(endpoint)
    ElMessage.success('已取消共享')
    await loadSharedUsers()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '取消失败')
  }
}

async function updatePermission(user: SharedUser) {
  // 先取消再重新共享（更新权限）
  try {
    const endpoint = props.itemType === 'kb'
      ? `/knowledge-bases/${props.itemId}/share/${user.id}`
      : `/documents/${props.itemId}/share/${user.id}`
    await api.delete(endpoint)
    await api.post(
      props.itemType === 'kb'
        ? `/knowledge-bases/${props.itemId}/share`
        : `/documents/${props.itemId}/share`,
      { user_id: user.id, permission: user.permission }
    )
    ElMessage.success('权限已更新')
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '更新失败')
  }
}
</script>

<style scoped>
.share-section h4 {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--color-foreground);
}

.shared-user-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid var(--color-border);
}

.shared-username {
  flex: 1;
  font-size: 14px;
}

.empty-hint {
  font-size: 13px;
  color: var(--color-secondary);
  padding: 8px 0;
}

.search-results {
  margin-top: 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  max-height: 150px;
  overflow-y: auto;
}

.search-result-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  cursor: pointer;
  font-size: 14px;
  transition: background 0.15s;
}

.search-result-item:hover {
  background: var(--color-muted);
}
</style>
