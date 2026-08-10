<script setup lang="ts">
type ConfidenceLevel = 'high' | 'medium' | 'low' | 'unavailable'

const props = defineProps<{
  level: ConfidenceLevel
  reasons?: string[]
}>()

const labels: Record<ConfidenceLevel, string> = {
  high: '高',
  medium: '中',
  low: '低',
  unavailable: '不可用'
}
</script>

<template>
  <div class="reliability-badge" :data-level="props.level">
    <span>置信度：{{ labels[props.level] }}</span>
    <span v-for="reason in props.reasons || []" :key="reason" class="reason">{{ reason }}</span>
  </div>
</template>
