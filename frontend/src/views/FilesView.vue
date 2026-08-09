<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useFilesStore } from '../stores/files'
import api from '../utils/api'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'

const filesStore = useFilesStore()
const dragover = ref(false)

onMounted(() => { filesStore.loadFiles() })

function getFileIcon(ext: string): string {
  const map: Record<string, string> = { '.pdf': '📕', '.docx': '📘', '.xlsx': '📊', '.csv': '📊', '.md': '📝', '.txt': '📄' }
  return map[ext] || '📄'
}

async function handleUpload(file: UploadFile) {
  try {
    await filesStore.uploadFile(file.raw!)
    ElMessage.success(`文件 ${file.name} 上传成功`)
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '上传失败')
  }
}

async function handleDelete(name: string) {
  try {
    await ElMessageBox.confirm(`确定删除文件 "${name}"？`, '确认删除', { type: 'warning' })
    await filesStore.deleteFile(name)
    ElMessage.success('文件已删除')
  } catch {}
}

function handleDragover(e: DragEvent) { e.preventDefault(); dragover.value = true }
function handleDragleave() { dragover.value = false }

async function handleDrop(e: DragEvent) {
  e.preventDefault(); dragover.value = false
  const file = e.dataTransfer?.files[0]
  if (file) {
    try {
      await filesStore.uploadFile(file)
      ElMessage.success(`文件 ${file.name} 上传成功`)
    } catch (err: any) {
      ElMessage.error(err.response?.data?.detail || '上传失败')
    }
  }
}

// --- Batch Import ---
const showBatchImportDialog = ref(false)
const batchImportMode = ref<'qa_pair' | 'document' | 'table'>('qa_pair')
const batchImportFile = ref<File | null>(null)
const batchImportFileRaw = ref<UploadFile | null>(null)
const batchImportLoading = ref(false)
const batchImportResult = ref<{ chunks?: number; detail?: string } | null>(null)
const batchQuestionCol = ref('question')
const batchAnswerCol = ref('answer')
const batchContentCol = ref('content')

const batchImportModeOptions = [
  { value: 'qa_pair', label: '问答对模式', desc: '每行一个问题 + 答案' },
  { value: 'document', label: '文档模式', desc: '每行一个文档片段' },
  { value: 'table', label: '表格模式', desc: '整张表转为结构化文本' },
]

const batchConfigFields = computed(() => {
  if (batchImportMode.value === 'qa_pair') {
    return [
      { key: 'question_col', label: '问题列名', model: batchQuestionCol },
      { key: 'answer_col', label: '答案列名', model: batchAnswerCol },
    ]
  }
  if (batchImportMode.value === 'document') {
    return [
      { key: 'content_col', label: '内容列名', model: batchContentCol },
    ]
  }
  return []
})

function openBatchImportDialog() {
  batchImportMode.value = 'qa_pair'
  batchImportFile.value = null
  batchImportFileRaw.value = null
  batchImportResult.value = null
  batchQuestionCol.value = 'question'
  batchAnswerCol.value = 'answer'
  batchContentCol.value = 'content'
  showBatchImportDialog.value = true
}

function handleBatchImportFileChange(file: UploadFile) {
  batchImportFileRaw.value = file
  batchImportFile.value = file.raw ?? null
}

async function submitBatchImport() {
  if (!batchImportFile.value) {
    ElMessage.warning('请选择要导入的文件')
    return
  }

  batchImportLoading.value = true
  batchImportResult.value = null

  const config: Record<string, string> = {}
  for (const field of batchConfigFields.value) {
    config[field.key] = field.model.value
  }

  const formData = new FormData()
  formData.append('file', batchImportFile.value)
  formData.append('mode', batchImportMode.value)
  formData.append('config', JSON.stringify(config))

  try {
    const res = await api.post('/batch-import', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
    batchImportResult.value = { chunks: res.data.chunks, detail: res.data.detail || '导入完成' }
    ElMessage.success(`导入成功，共生成 ${res.data.chunks} 个 chunk`)
    await filesStore.loadFiles(true)
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '批量导入失败')
  } finally {
    batchImportLoading.value = false
  }
}
</script>

<template>
  <div class="files-page">
    <div :class="['drop-zone', { active: dragover }]" @dragover="handleDragover" @dragleave="handleDragleave" @drop="handleDrop">
      <div class="drop-icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
        </svg>
      </div>
      <h3>拖拽文件到此处上传</h3>
      <p>支持 txt、md、pdf、docx、xlsx、csv</p>
      <div class="upload-actions">
        <el-upload :show-file-list="false" :before-upload="() => false" :on-change="handleUpload" accept=".txt,.md,.pdf,.docx,.xlsx,.csv">
          <el-button type="primary">选择文件</el-button>
        </el-upload>
        <el-button @click="openBatchImportDialog">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right: 6px; vertical-align: -2px;">
            <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>
          </svg>
          批量导入
        </el-button>
      </div>
    </div>

    <!-- Batch Import Dialog -->
    <el-dialog v-model="showBatchImportDialog" title="批量导入" width="520px" :close-on-click-modal="false" @closed="batchImportResult = null">
      <div class="batch-import-dialog">
        <label class="field-label">导入文件</label>
        <el-upload
          drag
          :auto-upload="false"
          :show-file-list="false"
          :on-change="handleBatchImportFileChange"
          accept=".csv,.xlsx"
        >
          <template v-if="batchImportFile">
            <div class="uploaded-file">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              <span>{{ batchImportFile.name }}</span>
            </div>
          </template>
          <template v-else>
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color: var(--color-secondary); margin-bottom: 8px;">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
            </svg>
            <div class="el-upload__text">拖拽文件或 <em>点击选择</em></div>
            <div class="el-upload__tip">支持 .csv / .xlsx 格式</div>
          </template>
        </el-upload>

        <label class="field-label">导入模式</label>
        <div class="mode-options">
          <label v-for="opt in batchImportModeOptions" :key="opt.value" :class="['mode-option', { active: batchImportMode === opt.value }]">
            <input type="radio" :value="opt.value" v-model="batchImportMode" />
            <div>
              <div class="mode-option-title">{{ opt.label }}</div>
              <div class="mode-option-desc">{{ opt.desc }}</div>
            </div>
          </label>
        </div>

        <template v-if="batchConfigFields.length">
          <label class="field-label">列名配置</label>
          <div class="config-fields">
            <div v-for="field in batchConfigFields" :key="field.key" class="config-field">
              <span class="config-field-label">{{ field.label }}</span>
              <el-input v-model="field.model.value" :placeholder="field.key" size="default" />
            </div>
          </div>
        </template>

        <div v-if="batchImportResult" class="import-result">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--color-success); flex-shrink: 0;">
            <path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
          </svg>
          <span>导入完成，共生成 <strong>{{ batchImportResult.chunks }}</strong> 个 chunk</span>
        </div>
      </div>

      <template #footer>
        <el-button @click="showBatchImportDialog = false">取消</el-button>
        <el-button type="primary" :loading="batchImportLoading" :disabled="!batchImportFile" @click="submitBatchImport">
          {{ batchImportLoading ? '导入中...' : '开始导入' }}
        </el-button>
      </template>
    </el-dialog>

    <div class="grid">
      <div v-for="file in filesStore.files" :key="file.name" class="file-card">
        <div class="file-icon">{{ getFileIcon(file.ext) }}</div>
        <div class="file-name">{{ file.name }}</div>
        <div class="file-size">{{ file.size_human }}</div>
        <button class="file-delete" @click="handleDelete(file.name)" title="删除">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <line x1="3" y1="3" x2="11" y2="11"/><line x1="11" y1="3" x2="3" y2="11"/>
          </svg>
        </button>
      </div>

      <div v-if="!filesStore.files.length && !filesStore.loading" class="empty">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" opacity="0.3">
          <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>
        </svg>
        <p>暂无文件</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.files-page {
  padding: var(--space-6);
  overflow-y: auto;
  height: 100%;
}

.drop-zone {
  border: 2px dashed var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-12) var(--space-6);
  text-align: center;
  transition: all var(--duration-normal) var(--ease-out);
  margin-bottom: var(--space-6);
}

.drop-zone.active {
  border-color: var(--color-accent);
  background: var(--color-accent-light);
}

.drop-icon { color: var(--color-secondary); margin-bottom: var(--space-4); }

.drop-zone h3 {
  font-family: var(--font-heading);
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
  margin-bottom: var(--space-2);
}

.drop-zone p {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  margin-bottom: var(--space-4);
}

.upload-actions {
  display: flex;
  gap: var(--space-3);
  justify-content: center;
}

/* Batch Import Dialog */
.batch-import-dialog {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.field-label {
  display: block;
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
  margin-bottom: var(--space-1);
}

.uploaded-file {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-foreground);
  font-size: var(--text-sm);
  padding: var(--space-2) 0;
}

.mode-options {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.mode-option {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.mode-option:hover {
  border-color: var(--color-border-hover);
}

.mode-option.active {
  border-color: var(--color-accent);
  background: var(--color-accent-light);
}

.mode-option input[type="radio"] {
  accent-color: var(--color-accent);
  flex-shrink: 0;
}

.mode-option-title {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
}

.mode-option-desc {
  font-size: var(--text-xs);
  color: var(--color-secondary);
  margin-top: 2px;
}

.config-fields {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.config-field {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.config-field-label {
  font-size: var(--text-sm);
  color: var(--color-secondary);
  min-width: 80px;
  flex-shrink: 0;
}

.import-result {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  background: var(--color-success-light, rgba(39, 166, 68, 0.1));
  border-radius: var(--radius);
  font-size: var(--text-sm);
  color: var(--color-foreground);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--space-4);
}

.file-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  padding: var(--space-5);
  position: relative;
  transition: all var(--duration-normal) var(--ease-out);
  cursor: pointer;
  animation: slideUp 0.3s var(--ease-out);
}

.file-card:hover {
  border-color: var(--color-border-hover);
  box-shadow: var(--shadow);
  transform: translateY(-2px);
}

.file-icon { font-size: 28px; margin-bottom: var(--space-3); }

.file-name {
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--color-foreground);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-bottom: var(--space-1);
}

.file-size { font-size: var(--text-xs); color: var(--color-secondary); }

.file-delete {
  position: absolute;
  top: var(--space-2);
  right: var(--space-2);
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  color: var(--color-secondary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  opacity: 0;
  transition: all var(--duration-fast);
}

.file-card:hover .file-delete { opacity: 1; }
.file-delete:hover { background: var(--color-destructive-light); color: var(--color-destructive); }

.empty {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-12);
  color: var(--color-secondary);
}
</style>
