<script setup lang="ts">
import type { OrgUnit } from '../api/organization'
defineProps<{ units: OrgUnit[]; selected?: string }>()
defineEmits<{ select: [code: string] }>()
</script>
<template><div class="org-tree"><button v-for="unit in units" :key="unit.code" :data-test="`unit-${unit.code}`" :class="{selected:selected===unit.code}" @click="$emit('select',unit.code)"><span>{{unit.kind==='company'?'◆':unit.kind==='division'?'◇':'▹'}}</span>{{unit.name}}</button></div></template>
<style scoped>.org-tree{display:flex;flex-direction:column;gap:4px}.org-tree button{padding:10px 12px;border:0;border-radius:8px;background:transparent;text-align:left;cursor:pointer}.org-tree button:hover,.org-tree button.selected{background:var(--color-accent-light);color:var(--color-primary)}</style>
