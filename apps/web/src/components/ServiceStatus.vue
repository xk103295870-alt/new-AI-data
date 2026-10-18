<script setup lang="ts">
// 展示后端服务状态：检查中 / 可用 / 不可用。
// 通过 props 接收状态与文案，便于组件测试直接注入不同状态。
import { computed } from 'vue'
import type { ServiceState } from '@/composables/useServiceStatus'

const props = defineProps<{
  state: ServiceState
  message: string
}>()

const tagType = computed(() => {
  if (props.state === 'available') return 'success'
  if (props.state === 'unavailable') return 'danger'
  return 'info'
})
</script>

<template>
  <el-tag :type="tagType" role="status" aria-live="polite">{{ message }}</el-tag>
</template>
