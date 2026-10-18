<script setup lang="ts">
// 数据源状态展示：未配置 / 待检查 / 检查中 / 最近检查成功 / 最近检查失败。
// 提供“检查/重试”按钮触发列表加载或授权 Schema 请求，并以纯文本展示授权摘要
// 与安全提示（注释、元数据、告警仅文本渲染，Vue 插值自动转义，不执行 HTML/脚本）。
import { computed, onMounted } from 'vue'
import type { SchemaObject } from '@/types/datasource'
import { formatTime, useDatasourceStatus } from '@/composables/useDatasourceStatus'
import { locale, schemaWarningText, translate } from '@/i18n'

const {
  state,
  message,
  datasource,
  schema,
  warnings,
  warningCodes,
  checkedAt,
  canAct,
  load,
  act,
} = useDatasourceStatus()
onMounted(load)

const tagType = computed(() => {
  switch (state.value) {
    case 'ok':
      return 'success'
    case 'error':
      return 'danger'
    case 'disabled':
      return 'warning'
    default:
      return 'info'
  }
})

const buttonText = computed(() => {
  if (state.value === 'ok') return translate('datasource.recheck')
  if (state.value === 'error') {
    // 首次列表失败（无数据源）→ 重试加载；已有数据源 → 重试检查
    return translate(datasource.value === null ? 'datasource.retryLoad' : 'datasource.retry')
  }
  return translate('datasource.check')
})

const showTime = computed(() => (state.value === 'ok' || state.value === 'error') && checkedAt.value !== null)

/** 将对象的授权列名拼接为可读文本；分隔符随界面语言变化。 */
function columnNames(obj: SchemaObject): string {
  return obj.columns.map((c) => c.name).join(locale.value === 'zh' ? '、' : ', ')
}

/** 固定告警按稳定 code 本地化；未知/旧响应统一展示通用提示，不回显自由文本。 */
function warningLabel(warning: string, index: number): string {
  const code =
    warningCodes.value.length === warnings.value.length ? warningCodes.value[index] : undefined
  return schemaWarningText(code, warning)
}
</script>

<template>
  <div class="datasource-status" role="status" aria-live="polite">
    <el-tag :type="tagType">{{ message }}</el-tag>
    <span v-if="showTime" class="check-time">
      {{ translate('datasource.checkTime', { time: formatTime(checkedAt) }) }}
    </span>
    <el-button
      size="small"
      :disabled="!canAct"
      :loading="state === 'checking' || state === 'loading'"
      :aria-label="buttonText"
      @click="act"
    >
      {{ buttonText }}
    </el-button>
    <span v-if="state === 'disabled'" class="datasource-hint">
      {{ translate('datasource.disabledHint') }}
    </span>

    <div v-if="warnings.length" class="datasource-warnings">
      <div class="warn-title">{{ translate('datasource.securityNotice') }}</div>
      <ul>
        <li v-for="(w, index) in warnings" :key="w">{{ warningLabel(w, index) }}</li>
      </ul>
    </div>

    <div v-if="schema && schema.objects.length" class="schema-summary">
      <div class="schema-title">{{ translate('datasource.schemaSummary') }}</div>
      <ul>
        <li v-for="obj in schema.objects" :key="obj.name">
          <span class="obj-name">
            {{ translate(obj.kind === 'view' ? 'datasource.view' : 'datasource.table') }}
            {{ obj.name }}
          </span>
          <span class="obj-cols">{{ columnNames(obj) }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.datasource-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.datasource-hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.check-time {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.datasource-warnings {
  width: 100%;
  font-size: 13px;
  color: var(--el-color-warning);
}
.warn-title {
  margin-bottom: 2px;
}
.datasource-warnings ul {
  margin: 0;
  padding-left: 18px;
}
.schema-summary {
  width: 100%;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.schema-title {
  margin-bottom: 4px;
  color: var(--el-text-color-secondary);
}
.schema-summary ul {
  margin: 0;
  padding-left: 18px;
}
.obj-name {
  font-weight: 600;
}
.obj-cols {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
}
</style>
