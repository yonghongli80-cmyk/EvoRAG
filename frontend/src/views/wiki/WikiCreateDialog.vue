<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps<{
  defaultTitle?: string
  selectedKbId?: string
  creating?: boolean
}>()

const visible = defineModel<boolean>('visible', { default: false })

const emit = defineEmits<{
  (e: 'create', payload: { title: string; slug?: string; content: string; tags: string[] }): void
}>()

const newTitle = ref('')
const newSlug = ref('')
const newContent = ref('')
const newTags = ref<string[]>([])

watch(visible, (val) => {
  if (val) {
    const title = props.defaultTitle || ''
    newTitle.value = title
    newSlug.value = title ? title.toLowerCase().replace(/\s+/g, '-') : ''
    newContent.value = `# ${title || '新页面'}\n\n在此输入 Markdown 内容。使用 \`[[页面名称]]\` 创建双向链接。`
    newTags.value = []
  }
})

function handleCreate() {
  const title = newTitle.value.trim()
  if (!title) {
    ElMessage.warning('请输入词条标题')
    return
  }
  if (!props.selectedKbId) {
    ElMessage.warning('请先选择知识库')
    return
  }

  emit('create', {
    title,
    slug: newSlug.value.trim() || undefined,
    content: newContent.value,
    tags: newTags.value
  })
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="新建 Wiki 词条"
    width="640px"
    destroy-on-close
  >
    <div class="dialog-form">
      <div class="form-item">
        <label class="form-label">词条标题 *</label>
        <el-input v-model="newTitle" placeholder="例如: 微服务架构设计规范" />
      </div>
      <div class="form-item">
        <label class="form-label">自定义 Slug (英文别名，可选)</label>
        <el-input v-model="newSlug" placeholder="例如: microservices-spec" />
      </div>
      <div class="form-item">
        <label class="form-label">Markdown 正文内容 *</label>
        <el-input
          v-model="newContent"
          type="textarea"
          :rows="10"
          placeholder="使用 [[其他词条名称]] 创建双链引用..."
        />
      </div>
    </div>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="props.creating"
        data-test="confirm-create-wiki-btn"
        @click="handleCreate"
      >
        创建并同步索引
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dialog-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-label {
  font-size: 13px;
  font-weight: 500;
}
</style>
