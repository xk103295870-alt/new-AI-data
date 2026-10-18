<script setup lang="ts">
// 聊天面板：数据源就绪后启用输入；展示用户问题、AI 结论、查询元信息、提示与图表。
// 约束：
// - source_id 只取自后端数据源列表，不接受用户输入或前端拼接；
// - 查询元信息（行数/截断/来源）在结论下方如实展示，空结果与截断由后端标注；
// - 图表只渲染通过运行时校验的 ChartSpec；无图表时不显示占位或伪造图形。
import { computed, onMounted, ref } from 'vue'
import ChartRenderer from '@/components/ChartRenderer.vue'
import ChatInput from '@/components/ChatInput.vue'
import { fetchDatasources } from '@/api/datasource'
import type { DatasourceErrorKind } from '@/api/datasource'
import { displayChatMessage, useChat } from '@/composables/useChat'
import { datasourceErrorText, translate, warningText } from '@/i18n'
import type { DatasourceItem } from '@/types/datasource'

type SourceState = 'loading' | 'ready' | 'unavailable' | 'error'

const { messages, busy, send, reset } = useChat()

const source = ref<DatasourceItem | null>(null)
const sourceState = ref<SourceState>('loading')
// 只保存稳定错误 kind，文案在渲染时按当前界面语言计算，切换语言不重新请求。
const sourceErrorKind = ref<DatasourceErrorKind | 'unknown'>('unknown')
const sourceMissing = ref(false)

/** 加载数据源列表；只有 configured=true 才允许发送，状态未知时不放行。 */
async function loadSource(): Promise<void> {
  sourceState.value = 'loading'
  sourceMissing.value = false
  sourceErrorKind.value = 'unknown'
  try {
    const resp = await fetchDatasources()
    const item = resp.data.datasources[0]
    if (!item) {
      sourceState.value = 'error'
      sourceMissing.value = true
      return
    }
    source.value = item
    if (!item.configured) {
      sourceState.value = 'unavailable'
      return
    }
    sourceState.value = 'ready'
  } catch (err) {
    sourceState.value = 'error'
    sourceMissing.value = false
    sourceErrorKind.value =
      err instanceof Error && 'kind' in err
        ? ((err as { kind: DatasourceErrorKind }).kind)
        : 'unknown'
  }
}

onMounted(loadSource)

const inputDisabled = computed(() => sourceState.value !== 'ready')
const hint = computed(() => {
  switch (sourceState.value) {
    case 'loading':
      return translate('chat.sourceLoading')
    case 'unavailable':
      return translate('chat.sourceUnavailable')
    case 'error':
      return sourceMissing.value
        ? translate('chat.sourceNoInfo')
        : translate('chat.sourceErrorSuffix', {
            message: datasourceErrorText(sourceErrorKind.value),
          })
    default:
      return busy.value ? translate('chat.loading') : translate('chat.readyHint')
  }
})

function roleText(role: 'user' | 'assistant'): string {
  return translate(role === 'user' ? 'chat.roleUser' : 'chat.roleAssistant')
}

function queryInfoText(info: NonNullable<(typeof messages.value)[number]['queryInfo']>): string {
  return translate('chat.queryInfo', {
    rows: info.row_count,
    truncation: translate(info.truncated ? 'chat.truncated' : 'chat.notTruncated'),
    source: info.source_id,
  })
}

function messageText(message: (typeof messages.value)[number]): string {
  return displayChatMessage(message)
}

async function handleSend(text: string): Promise<void> {
  const sourceId = source.value?.id
  if (!sourceId || sourceState.value !== 'ready') return
  await send(text, sourceId)
}

// 供页面“新建对话”调用；reset 在请求进行中会自行忽略。
defineExpose({ reset, busy })
</script>

<template>
  <section class="chat-panel" :aria-label="translate('chat.aria')">
    <div v-if="messages.length" class="chat-messages" role="log" aria-live="polite">
      <div
        v-for="(message, index) in messages"
        :key="index"
        :class="['chat-message', message.role, { 'is-error': message.isError }]"
      >
        <div class="chat-bubble">
          <span class="chat-role">{{ roleText(message.role) }}</span>
          <p class="chat-content">{{ messageText(message) }}</p>
          <p v-if="message.queryInfo" class="query-info">
            {{ queryInfoText(message.queryInfo) }}
          </p>
          <div v-if="message.warnings && message.warnings.length" class="chat-warnings">
            <span>{{ translate('chat.notice') }}</span>
            <ul>
              <li v-for="warning in message.warnings" :key="warning">
                {{ warningText(warning) }}
              </li>
            </ul>
          </div>
          <ChartRenderer
            v-for="chart in message.charts"
            :key="chart.id"
            :spec="chart"
            :query-truncated="message.queryInfo?.truncated ?? false"
          />
        </div>
      </div>
      <p v-if="busy" class="chat-loading" role="status">{{ translate('chat.loading') }}</p>
    </div>
    <el-empty v-else :description="translate('chat.empty')" />

    <ChatInput
      :disabled="inputDisabled"
      :loading="busy"
      :hint="hint"
      @send="handleSend"
    />
  </section>
</template>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
}
.chat-messages {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 480px;
  overflow-y: auto;
}
.chat-message {
  display: flex;
}
.chat-message.user {
  justify-content: flex-end;
}
.chat-bubble {
  max-width: 80%;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
}
.chat-message.user .chat-bubble {
  background: var(--el-color-primary-light-9);
}
.chat-message.is-error .chat-bubble {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}
.chat-role {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.chat-content {
  margin: 4px 0 0;
  white-space: pre-wrap;
  word-break: break-word;
}
.query-info {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.chat-warnings {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-color-warning);
}
.chat-warnings ul {
  margin: 2px 0 0;
  padding-left: 18px;
}
.chat-loading {
  margin: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
