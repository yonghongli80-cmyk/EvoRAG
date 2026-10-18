<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  applyCodePatch,
  closeOutMergedCodePatch,
  fetchCodePatch,
  fetchCodePatches,
  regenerateCodePatch,
  submitCodePatchDecision
} from '../../api/evolution'
import type {
  CodePatchDecisionAction,
  EvolutionCodePatch,
  EvolutionCodePatchDetail
} from '../../api/evolution'
import {
  formatDelta,
  formatLatency,
  formatRate,
  formatTime,
  gateLabel,
  gateResultLabel,
  isForbidden,
  layerLabel,
  shortCommit
} from './helpers'

const emit = defineEmits<{
  (e: 'error', message: string): void
  (e: 'forbidden'): void
  (e: 'clearError'): void
}>()

const codePatches = ref<EvolutionCodePatch[]>([])
const codePatchesLoaded = ref(false)
const selectedCodePatch = ref<EvolutionCodePatchDetail | null>(null)
const selectedCodePatchId = ref<number | null>(null)
const codePatchLoading = ref(false)
const codePatchNote = ref('')
/** 决策成功后的可见提示：成功时界面此前**什么都不显示**，看起来像"点了没用"。 */
const decisionHint = ref('')
const codePatchActing = ref(false)
const copyHint = ref('')

/** 只有"待验证"的补丁才接受审核决策（同上）。 */
const codePatchDecidable = computed(
  () => selectedCodePatch.value?.status === 'awaiting_verification'
)

/**
 * "已合入结案"只对**已经走到上线那一步**的补丁有意义（approved_for_canary / canary）。
 *
 * 它处理的是 2026-10-12 演练发现的那类陈旧行：改动早就随提交落地，候选行却停在
 * approved_for_canary，作为"未决补丁"一直占着聚类的准入名额。
 */
const codePatchClosable = computed(() =>
  ['approved_for_canary', 'canary'].includes(selectedCodePatch.value?.status || '')
)

/**
 * 回滚后从同一聚类重新生成一条候选。
 *
 * 回滚把状态写成终态 `rolled_back`，接口层面无法再应用；这条路径产出一条**新**候选回到
 * `candidate_testing`，重新走沙箱 → 审核 → 批准 → 应用，因此它是"再试一次"的正道，
 * 而不是绕过状态机的复活。
 */
const codePatchRegenerable = computed(
  () => selectedCodePatch.value?.status === 'rolled_back'
)

/**
 * 最近一次带结构化指标的**沙箱**条目（没有就返回 null，让面板如实说不显示对照）。
 *
 * 只看 `sandbox_summary`，不看整条 gate_log：宿主 A/B 的条目的 `metrics.verdict` 形状与沙箱
 * 那份相同，若在 gate_log 里按"有 verdict"找，会把宿主 A/B 的数字当成沙箱的结论画出来。
 */
const sandboxMetrics = computed(() => {
  const entries = selectedCodePatch.value?.sandbox_summary || []
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const metrics = entries[index].metrics
    if (metrics?.verdict) return { entry: entries[index], verdict: metrics.verdict }
  }
  return null
})

const sandboxMetricsUnavailable = computed(() => {
  const entries = selectedCodePatch.value?.sandbox_summary || []
  return entries.some((entry) => entry.metrics?.verdict_unavailable)
})

/** 最近一次带对照数字的宿主 A/B 条目（层 2 的另一个生产者，不动候选状态）。 */
const hostAbMetrics = computed(() => {
  const entries = selectedCodePatch.value?.host_ab_summary || []
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const metrics = entries[index].metrics
    if (metrics?.verdict) return { entry: entries[index], verdict: metrics.verdict, metrics }
  }
  return null
})

const hostAbUnavailable = computed(() => {
  const entries = selectedCodePatch.value?.host_ab_summary || []
  return entries.some((entry) => entry.metrics?.verdict_unavailable)
})

async function loadCodePatches() {
  codePatchLoading.value = true
  try {
    const data = await fetchCodePatches({ pageSize: 100 })
    codePatches.value = data.items
    codePatchesLoaded.value = true
    emit('clearError')
  } catch (err) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', '无法加载补丁候选列表')
  } finally {
    codePatchLoading.value = false
  }
}

async function openCodePatch(patch: EvolutionCodePatch) {
  selectedCodePatchId.value = patch.id
  codePatchLoading.value = true
  try {
    selectedCodePatch.value = await fetchCodePatch(patch.id)
    codePatchNote.value = ''
    emit('clearError')
  } catch (err) {
    selectedCodePatch.value = null
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', '无法加载补丁详情')
  } finally {
    codePatchLoading.value = false
  }
}

/** Re-read the open patch after a decision: the trail and the status moved. */
async function reloadCodePatch() {
  if (selectedCodePatchId.value === null) return
  await openCodePatch({ id: selectedCodePatchId.value } as EvolutionCodePatch)
}

/** 已批准的补丁可以一键应用（后端会先跑前置检查，失败就回滚且不重启）。 */
async function runApply() {
  if (!selectedCodePatch.value || codePatchActing.value) return
  codePatchActing.value = true
  try {
    const result = await applyCodePatch(selectedCodePatch.value.id, true)
    const steps = result.steps || {}
    decisionHint.value =
      `已应用（${result.applied_now ? '本次应用' : '此前已应用'}）` +
      `，导入冒烟 ${steps.import_smoke ? '通过' : '未通过'}` +
      `，目标测试 ${steps.target_tests ? '通过' : '未通过'}` +
      `${steps.restart_requested ? '，重启已排队（助手会等端口释放、拉起服务并轮询 /health，起不来会自动回滚）' : ''}`
    emit('clearError')
    await reloadCodePatch()
    await loadCodePatches()
  } catch (err: any) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', err?.response?.data?.detail || '应用失败')
  } finally {
    codePatchActing.value = false
  }
}

async function runCloseOutMerged() {
  if (!selectedCodePatch.value || codePatchActing.value) return
  codePatchActing.value = true
  try {
    const updated = await closeOutMergedCodePatch(selectedCodePatch.value.id, codePatchNote.value)
    selectedCodePatch.value = updated
    decisionHint.value = `已结案为 ${updated.status}（改动已随提交落地）`
    emit('clearError')
    codePatchNote.value = ''
    await loadCodePatches()
  } catch (err: any) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', err?.response?.data?.detail || '结案失败')
  } finally {
    codePatchActing.value = false
  }
}

async function runRegenerate() {
  if (!selectedCodePatch.value || codePatchActing.value) return
  const source = selectedCodePatch.value
  codePatchActing.value = true
  try {
    const created = await regenerateCodePatch(source.id, codePatchNote.value)
    decisionHint.value =
      `已从同一聚类重新生成候选 #${created.id}（状态 ${created.status}）` +
      `：它需要重新走沙箱与审核，批准后才会进入应用`
    emit('clearError')
    codePatchNote.value = ''
    await loadCodePatches()
    await openCodePatch(created)
  } catch (err: any) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', err?.response?.data?.detail || '重新生成失败')
  } finally {
    codePatchActing.value = false
  }
}

async function decideCodePatch(action: CodePatchDecisionAction) {
  if (!selectedCodePatch.value || codePatchActing.value) return
  codePatchActing.value = true
  try {
    await submitCodePatchDecision(selectedCodePatch.value.id, action, codePatchNote.value)
    emit('clearError')
    decisionHint.value = `已提交：${action}`
    codePatchNote.value = ''
    await reloadCodePatch()
    await loadCodePatches()
  } catch (err: any) {
    if (isForbidden(err)) {
      emit('forbidden')
      return
    }
    emit('error', err?.response?.data?.detail || '审核决策提交失败')
  } finally {
    codePatchActing.value = false
  }
}

/** 只读地给出命令供复制：这个界面从不执行 apply / revert（那是管理员在终端做的事）。 */
async function copyCommand(text: string, what: string) {
  try {
    await navigator.clipboard.writeText(text)
    copyHint.value = `已复制${what}`
  } catch {
    copyHint.value = `复制失败，请手动选中${what}`
  }
}

onMounted(() => {
  void loadCodePatches()
})
</script>

<template>
  <div class="tab-panel">
    <div class="table-card">
      <h3 class="panel-title">代码补丁候选（层 4 = 管理员审核，批准只导出不应用）</h3>
      <p v-if="codePatchLoading && codePatches.length === 0" class="muted" data-test="code-candidates-loading">
        加载补丁候选中…
      </p>
      <p v-else-if="codePatches.length === 0" class="muted" data-test="code-candidates-empty">
        暂无补丁：只有经管理员确认根因的失败聚类才会按层级生成代码补丁，越界路径与限额在层 1 即被拒绝。
      </p>
      <table v-else data-test="code-candidate-list">
        <thead>
          <tr>
            <th>ID</th><th>层级</th><th>状态</th><th>置信度</th><th>文件数</th><th>同类</th>
            <th>基线提交</th><th>沙箱</th><th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="patch in codePatches"
            :key="patch.id"
            data-test="code-candidate-row"
            :class="{ 'candidate-row-active': selectedCodePatchId === patch.id }"
            @click="openCodePatch(patch)"
          >
            <td>{{ patch.id }}</td>
            <td>
              {{ layerLabel(patch.layer) }}<code>（{{ patch.layer }}）</code>
            </td>
            <td>
              <span
                class="badge"
                :class="{ 'badge-warn': patch.status === 'rejected' || patch.status === 'rolled_back' }"
              >
                {{ patch.status }}
              </span>
            </td>
            <td>{{ patch.confidence }}</td>
            <td class="cluster-meta" data-test="code-candidate-overlap">
              <span v-if="patch.overlap_ids && patch.overlap_ids.length" class="badge badge-warn">
                同类 { patch.overlap_ids.length } 条
              </span>
              <span v-else>—</span>
            </td>

            <td data-test="code-candidate-row-files">{{ patch.paths.length }}</td>
            <td><code>{{ shortCommit(patch.rollback_ref.base_commit || '') || '—' }}</code></td>
            <td>
              <span
                v-for="(entry, index) in patch.sandbox_summary"
                :key="index"
                class="badge"
                :class="{ 'badge-warn': entry.result !== 'passed' && entry.result !== 'accepted' }"
              >
                {{ gateResultLabel(entry.result) }}
              </span>
              <span v-if="patch.sandbox_summary.length === 0" class="cluster-meta">未验证</span>
            </td>
            <td>{{ formatTime(patch.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="selectedCodePatch" class="candidate-detail" data-test="code-candidate-detail">
      <h3 class="panel-title">
        补丁 #{{ selectedCodePatch.id }}（{{ layerLabel(selectedCodePatch.layer) }}）
      </h3>
      <p v-if="codePatchLoading" class="muted">加载补丁…</p>
      <template v-else>
        <p class="cluster-meta">
          {{ selectedCodePatch.status }} · 置信度 {{ selectedCodePatch.confidence }} ·
          聚类 #{{ selectedCodePatch.cluster_id }}{{ selectedCodePatch.observation_id
            ? ` · 观测 #${selectedCodePatch.observation_id}` : '' }} ·
          提示词 {{ selectedCodePatch.prompt_version || '—' }} / 模型 {{ selectedCodePatch.model || '—' }}
        </p>
        <dl class="draft-grid">
          <dt>生成理由</dt>
          <dd data-test="code-candidate-rationale">{{ selectedCodePatch.rationale }}</dd>
          <dt>预期效果</dt>
          <dd data-test="code-candidate-expected-effect">{{ selectedCodePatch.expected_effect }}</dd>
          <dt>回滚引用</dt>
          <dd data-test="code-candidate-rollback-ref">
            base_commit={{ selectedCodePatch.rollback_ref.base_commit || '—' }}
          </dd>
        </dl>

        <h4 class="panel-subtitle">文件清单（层 1 边界内，只读）</h4>
        <ul class="patch-files" data-test="code-candidate-files">
          <li v-for="path in selectedCodePatch.paths" :key="path">
            <code>{{ path }}</code>
          </li>
        </ul>
        <p class="cluster-meta" data-test="code-candidate-test-paths">
          验证目标：<code v-for="path in selectedCodePatch.test_paths" :key="path">{{ path }}</code>
        </p>

        <h4 class="panel-subtitle">diff 全文（只读；应用由管理员手动执行）</h4>
        <pre class="patch-diff" data-test="patch-diff">{{ selectedCodePatch.diff }}</pre>

        <p
          v-if="selectedCodePatch.overlap_ids && selectedCodePatch.overlap_ids.length"
          class="cluster-meta"
          data-test="code-candidate-overlap-note"
        >
          与
          <code v-for="other in selectedCodePatch.overlap_ids" :key="other" class="term-chip">
            #{{ other }}
          </code>
          改的是同一个函数（{{ selectedCodePatch.overlap_key }}）——它们是同一条改动的变体，
          建议只留一条：并排应用两条会留下两套实现。
        </p>
        <h4 class="panel-subtitle">应用与回滚命令（只复制，不执行）</h4>
        <p class="cluster-meta" data-test="export-state">
          {{
            selectedCodePatch.exported
              ? `已导出：${selectedCodePatch.export_path}`
              : '尚未导出：先“批准并导出”才会写出补丁文件，命令届时才可用'
          }}
        </p>
        <div class="command-row">
          <code class="command-text" data-test="apply-command">{{
            selectedCodePatch.apply_command
          }}</code>
          <button
            type="button"
            class="query-button"
            data-test="copy-apply-command"
            @click="copyCommand(selectedCodePatch.apply_command, '应用命令')"
          >
            复制应用命令
          </button>
        </div>
        <div class="command-row">
          <code class="command-text" data-test="revert-command">{{
            selectedCodePatch.revert_command
          }}</code>
          <button
            type="button"
            class="query-button"
            data-test="copy-revert-command"
            @click="copyCommand(selectedCodePatch.revert_command, '回滚命令')"
          >
            复制回滚命令
          </button>
        </div>
        <p v-if="copyHint" class="cluster-meta" data-test="copy-hint">{{ copyHint }}</p>

        <h4 class="panel-subtitle">沙箱结论（层 2/3：隔离 worktree 内 apply + 目标测试 + 基准回放）</h4>
        <ul
          v-if="selectedCodePatch.sandbox_summary.length > 0"
          class="audit-list"
          data-test="code-candidate-sandbox"
        >
          <li v-for="(entry, index) in selectedCodePatch.sandbox_summary" :key="index">
            <span class="badge" :class="{ 'badge-warn': entry.result !== 'passed' && entry.result !== 'accepted' }">
              {{ gateResultLabel(entry.result) }}
            </span>
            <span class="cluster-meta">{{ entry.detail || '—' }} · {{ formatTime(entry.created_at) }}</span>
          </li>
        </ul>
        <p v-else class="muted">沙箱尚未验证该补丁：未通过层 2/3 的补丁不予审核。</p>

        <h4 class="panel-subtitle">五层门禁轨迹</h4>
        <ul
          v-if="selectedCodePatch.gate_log.length > 0"
          class="audit-list"
          data-test="code-candidate-gate-log"
        >
          <li
            v-for="(entry, index) in selectedCodePatch.gate_log"
            :key="index"
            data-test="code-candidate-gate-entry"
          >
            <span class="audit-action">{{ gateLabel(entry.gate) }}</span>
            <span
              class="badge"
              :class="{ 'badge-warn': entry.result !== 'passed' && entry.result !== 'accepted' }"
            >
              {{ gateResultLabel(entry.result) }}
            </span>
            <span class="cluster-meta">{{ entry.detail || '—' }} · {{ formatTime(entry.created_at) }}</span>
          </li>
        </ul>
        <p v-else class="muted">暂无门禁记录</p>
        <div
          v-if="sandboxMetrics"
          class="ab-table"
          data-test="sandbox-metrics"
        >
          <p class="cluster-meta">
            对照：{{ sandboxMetrics.verdict.passed ? '通过' : '未通过' }} ·
            route {{ formatDelta(sandboxMetrics.verdict.route_delta_pp) }}pp ·
            top-5 {{ formatDelta(sandboxMetrics.verdict.top5_delta_pp) }}pp ·
            失败调用 {{ formatDelta(sandboxMetrics.verdict.failed_calls_delta) }}
          </p>
          <table v-for="(side, name) in sandboxMetrics.verdict.sides" :key="name">
            <thead>
              <tr>
                <th>{{ name }}</th><th>route 准确率</th><th>Top-5 命中</th>
                <th>失败调用</th><th>预处理 P50</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>干净侧</td>
                <td>{{ formatRate(side.baseline.route_accuracy) }}</td>
                <td>{{ formatRate(side.baseline.top5_hit_rate) }}</td>
                <td>{{ side.baseline.failed_calls }}</td>
                <td>{{ formatLatency(side.baseline.prep_p50_ms) }}</td>
              </tr>
              <tr>
                <td>打补丁侧</td>
                <td>{{ formatRate(side.patched.route_accuracy) }}</td>
                <td>{{ formatRate(side.patched.top5_hit_rate) }}</td>
                <td>{{ side.patched.failed_calls }}</td>
                <td>{{ formatLatency(side.patched.prep_p50_ms) }}</td>
              </tr>
            </tbody>
          </table>
          <p class="cluster-meta">
            注：P50 在回放模式下取自录制（两侧相减时抵消，绝对值不是生产时延）。
          </p>
        </div>
        <p
          v-else-if="sandboxMetricsUnavailable"
          class="cluster-meta"
          data-test="sandbox-metrics-unavailable"
        >
          这次沙箱运行没有可比的对照（回放不可用或未产出报告）——"没有数字"不等于"没有变差"。
        </p>

        <h4 class="panel-subtitle">宿主 A/B 对照（层 2 的真实回归：干净树 vs 打过补丁的树）</h4>
        <div v-if="hostAbMetrics" class="ab-table" data-test="host-ab-metrics">
          <p class="cluster-meta">
            对照：{{ hostAbMetrics.verdict.passed ? '通过' : '未通过' }} ·
            route {{ formatDelta(hostAbMetrics.verdict.route_delta_pp) }}pp ·
            top-5 {{ formatDelta(hostAbMetrics.verdict.top5_delta_pp) }}pp ·
            失败调用 {{ formatDelta(hostAbMetrics.verdict.failed_calls_delta) }} ·
            每侧 {{ hostAbMetrics.metrics.runs ?? '—' }} 次（kb={{ hostAbMetrics.metrics.kb_id || '—' }}）
          </p>
          <table v-for="(side, name) in hostAbMetrics.verdict.sides" :key="name">
            <thead>
              <tr>
                <th>{{ name }}</th><th>route 准确率</th><th>Top-5 命中</th>
                <th>失败调用</th><th>预处理 P50</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>干净侧</td>
                <td>{{ formatRate(side.baseline.route_accuracy) }}</td>
                <td>{{ formatRate(side.baseline.top5_hit_rate) }}</td>
                <td>{{ side.baseline.failed_calls }}</td>
                <td>{{ formatLatency(side.baseline.prep_p50_ms) }}</td>
              </tr>
              <tr>
                <td>打补丁侧</td>
                <td>{{ formatRate(side.patched.route_accuracy) }}</td>
                <td>{{ formatRate(side.patched.top5_hit_rate) }}</td>
                <td>{{ side.patched.failed_calls }}</td>
                <td>{{ formatLatency(side.patched.prep_p50_ms) }}</td>
              </tr>
            </tbody>
          </table>
          <p class="cluster-meta">
            这是管理员在**宿主**上跑出来的对照（沙箱无凭据，量不了回归）；它只作为审核证据，
            从不自动改候选状态。
          </p>
        </div>
        <p
          v-else-if="hostAbUnavailable"
          class="cluster-meta"
          data-test="host-ab-unavailable"
        >
          最近一次宿主 A/B 没有产出可比的对照——"没有数字"不等于"没有变差"。
        </p>
        <p v-else class="cluster-meta" data-test="host-ab-empty">
          还没跑过宿主 A/B：层 2 的真实对照发生在应用之后的宿主上
          （<code>scripts/measure_patch_regression_ab.py</code>），跑完会回流到这条轨迹。
        </p>

        <h4 class="panel-subtitle">生命周期审计</h4>
        <ul
          v-if="selectedCodePatch.audit.length > 0"
          class="audit-list"
          data-test="code-candidate-audit"
        >
          <li v-for="row in selectedCodePatch.audit" :key="row.id">
            <span class="audit-action">{{ row.action }}</span>
            <span class="cluster-meta">
              {{ row.actor_id }} · {{ formatTime(row.created_at) }} · {{ row.note || '无备注' }}
            </span>
          </li>
        </ul>
        <p v-else class="muted">暂无审计记录</p>
        <div class="apply-bar" data-test="code-candidate-apply-bar">
          <button
            type="button"
            class="query-button"
            data-test="code-candidate-apply"
            :disabled="codePatchActing || selectedCodePatch?.status !== 'approved_for_canary'"
            @click="runApply"
          >
            应用并重启（先跑前置检查；失败会回滚且不重启）
          </button>
          <span class="cluster-meta">
            只有「已批准（approved_for_canary）」的补丁可用；已应用的补丁再点即为"校验 + 重启"。
          </span>
          <button
            type="button"
            class="query-button"
            data-test="code-candidate-regenerate"
            :disabled="codePatchActing || !codePatchRegenerable"
            @click="runRegenerate"
          >
            回滚后重新生成（同一聚类产出一条新候选，重新走沙箱与审核）
          </button>
          <span class="cluster-meta">
            回滚是终态：这条路径不回滚状态机，而是让同一聚类的**新**候选重新走一遍五层门禁。
          </span>
        </div>

        <div class="decision-bar">
          <textarea
            v-model="codePatchNote"
            class="decision-note"
            data-test="code-candidate-note"
            rows="2"
            placeholder="备注：批准依据、驳回理由或还缺的证据（写入审计）"
            :disabled="codePatchActing"
          ></textarea>
          <div class="decision-buttons">
            <button
              type="button"
              class="query-button"
              data-test="code-candidate-approve"
              :disabled="codePatchActing || !codePatchDecidable"
              @click="decideCodePatch('approve_for_apply')"
            >
              批准并导出
            </button>
            <button
              type="button"
              class="decision-button"
              data-test="code-candidate-reject"
              :disabled="codePatchActing || !codePatchDecidable"
              @click="decideCodePatch('reject')"
            >
              拒绝
            </button>
            <button
              type="button"
              class="decision-button"
              data-test="code-candidate-needs-revision"
              :disabled="codePatchActing || !codePatchDecidable"
              @click="decideCodePatch('needs_revision')"
            >
              需修订
            </button>
            <button
              type="button"
              class="decision-button"
              data-test="code-candidate-close-merged"
              :disabled="codePatchActing || !codePatchClosable"
              @click="runCloseOutMerged"
            >
              已合入：结案
            </button>
          </div>
          <span class="cluster-meta">
            「结案」用于**人已经把改动合进提交**的情形（不经 apply）；后端会核对改动确实在代码里、
            且那些文件相对 HEAD 干净，否则拒绝。
          </span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.tab-panel { display: grid; gap: var(--space-5); }

.table-card, .candidate-detail {
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: var(--shadow-xs);
}

table { width: 100%; border-collapse: collapse; }
th, td { padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--color-border); text-align: left; font-size: var(--text-sm); }
th { color: var(--color-secondary); background: var(--color-surface-2); font-weight: var(--font-semibold); }
tbody tr:hover { background: #f8fbff; }

.panel-title { margin: 0 0 var(--space-3); font-size: var(--text-md); font-weight: var(--font-semibold); color: var(--color-foreground); }
.panel-subtitle { margin: var(--space-4) 0 var(--space-2); font-size: var(--text-sm); font-weight: var(--font-semibold); color: var(--color-secondary); }

.muted { color: var(--color-secondary); font-size: var(--text-sm); }

.candidate-row-active { background: #f8fbff; }

.badge {
  padding: 2px var(--space-3);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius);
  color: var(--color-primary);
  background: var(--color-surface-2);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
}
.badge-warn { color: var(--color-destructive); border-color: #fecaca; background: var(--color-destructive-light); }

.cluster-meta { font-size: var(--text-xs); color: var(--color-secondary); }

.draft-grid { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--space-2) var(--space-4); margin: 0 0 var(--space-4); }
.draft-grid dt { color: var(--color-secondary); font-size: var(--text-xs); }
.draft-grid dd { margin: 0; color: var(--color-foreground); font-size: var(--text-sm); white-space: pre-wrap; word-break: break-word; }

.patch-files { list-style: none; margin: 0; padding-left: 0; }
.patch-files li { padding: var(--space-1) 0; font-size: var(--text-sm); }
.patch-files code { font-family: ui-monospace, monospace; font-size: var(--text-xs); }
.patch-diff {
  max-height: 420px;
  margin: 0;
  padding: var(--space-3);
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface-2);
  font-family: ui-monospace, monospace;
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre;
  user-select: text;
}

.command-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 6px 0;
}

.command-text {
  flex: 1;
  overflow-x: auto;
  white-space: nowrap;
  padding: 4px 6px;
  background: rgba(0, 0, 0, 0.04);
  border-radius: 4px;
}

.term-chip {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 1px 6px;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 10px;
}

.audit-list { list-style: none; margin: 0; padding-left: 0; }
.audit-list li { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-2); padding: var(--space-2) 0; border-bottom: 1px solid var(--color-border); }
.audit-action { color: var(--color-primary); font-family: ui-monospace, monospace; font-size: var(--text-xs); font-weight: var(--font-semibold); }

.ab-table {
  margin: 8px 0 12px;
}

.ab-table table {
  width: 100%;
  margin-top: 4px;
}

.apply-bar {
  display: grid;
  gap: var(--space-2);
  margin-top: var(--space-4);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border);
}

.decision-bar { display: grid; gap: var(--space-3); margin-top: var(--space-4); padding-top: var(--space-4); border-top: 1px solid var(--color-border); }

.decision-note {
  width: 100%;
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface);
  font: inherit;
  font-size: var(--text-sm);
  box-sizing: border-box;
  resize: vertical;
}

.decision-buttons { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.decision-buttons button { min-height: 34px; padding: 0 var(--space-4); font-size: var(--text-xs); }
.decision-buttons button:disabled { cursor: not-allowed; opacity: .5; }

.decision-button {
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-foreground);
  background: var(--color-surface-2);
  font: inherit;
  cursor: pointer;
}

.query-button {
  min-height: 38px;
  padding: 0 var(--space-5);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius);
  color: var(--color-on-primary);
  background: var(--color-primary);
  font: inherit;
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  cursor: pointer;
}

@media (max-width: 900px) {
  .table-card { overflow: auto; }
}
</style>
