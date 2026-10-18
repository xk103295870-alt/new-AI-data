<script setup lang="ts">
// 聊天输入区：受控文本 + 发送按钮。
// 禁用原因由父组件通过 hint 提供；加载中禁止重复发送，空内容不可发送。
import { ref } from 'vue'
import { translate } from '@/i18n'

const props = withDefaults(
  defineProps<{
    disabled?: boolean
    loading?: boolean
    hint?: string
  }>(),
  { disabled: false, loading: false, hint: '' },
)

const emit = defineEmits<{ send: [text: string] }>()
const text = ref('')

function submit(): void {
  if (props.disabled || props.loading) return
  const value = text.value.trim()
  if (!value) return
  text.value = ''
  emit('send', value)
}
</script>

<template>
  <div class="chat-input">
    <el-input
      v-model="text"
      type="textarea"
      :rows="2"
      resize="none"
      :disabled="disabled || loading"
      :placeholder="translate('chat.placeholder')"
      :aria-label="translate('chat.inputAria')"
      @keydown.enter.exact.prevent="submit"
    />
    <el-button
      type="primary"
      :disabled="disabled || loading || !text.trim()"
      :loading="loading"
      :aria-label="translate('chat.send')"
      @click="submit"
    >
      {{ translate('chat.send') }}
    </el-button>
    <p v-if="hint" class="chat-input-hint">{{ hint }}</p>
  </div>
</template>

<style scoped>
.chat-input {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}
.chat-input .el-textarea {
  flex: 1;
  min-width: 240px;
}
.chat-input-hint {
  width: 100%;
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
