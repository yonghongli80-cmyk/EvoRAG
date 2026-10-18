<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps<{
  name: string
  renaming?: boolean
}>()

const visible = defineModel<boolean>('visible', { default: false })

const emit = defineEmits<{
  (e: 'rename', newName: string): void
}>()

const renameDraft = ref('')

watch(visible, (val) => {
  if (val) {
    renameDraft.value = props.name || ''
  }
})

function handleRename() {
  const name = renameDraft.value.trim()
  if (!name) {
    ElMessage.warning('请输入名称')
    return
  }
  emit('rename', name)
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="重命名知识库"
    width="420px"
    :close-on-click-modal="false"
  >
    <el-input
      v-model="renameDraft"
      placeholder="输入知识库名称"
      maxlength="100"
      @keyup.enter="handleRename"
    />
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="props.renaming" @click="handleRename">
        保存
      </el-button>
    </template>
  </el-dialog>
</template>
