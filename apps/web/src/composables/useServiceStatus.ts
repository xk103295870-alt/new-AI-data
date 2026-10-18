// 后端服务状态机：checking / available / unavailable。
// 与“数据源状态”相互独立：后端存活不代表数据源已连接，二者不可混淆。
// 状态保存稳定 message key，文案由当前界面语言实时计算，切换语言无需重新请求。
import { computed, ref } from 'vue'
import { fetchHealth, HealthRequestError } from '@/api/health'
import { serviceErrorText, translate } from '@/i18n'
import type { MessageKey } from '@/i18n'

export type ServiceState = 'checking' | 'available' | 'unavailable'

/** 将底层错误映射为不泄露实现细节的友好文案。 */
export function friendlyMessage(err: unknown): string {
  if (err instanceof HealthRequestError) {
    return serviceErrorText(err.kind)
  }
  return serviceErrorText('unknown')
}

export function useServiceStatus() {
  const state = ref<ServiceState>('checking')
  const messageKey = ref<MessageKey>('service.checking')
  const message = computed(() => translate(messageKey.value))

  async function check(): Promise<void> {
    state.value = 'checking'
    messageKey.value = 'service.checking'
    try {
      await fetchHealth()
      state.value = 'available'
      messageKey.value = 'service.available'
    } catch (err) {
      state.value = 'unavailable'
      if (err instanceof HealthRequestError) {
        const kind = err.kind === 'invalid-response' ? 'invalidResponse' : err.kind
        messageKey.value = `service.${kind}` as MessageKey
      } else {
        messageKey.value = 'service.unknown'
      }
    }
  }

  return { state, message, check }
}
