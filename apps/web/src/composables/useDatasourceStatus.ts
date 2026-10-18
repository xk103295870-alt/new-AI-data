// 数据源状态机：加载列表状态 + 触发 Schema 检查。
// 与“后端服务状态”相互独立：数据源未配置/待检查/成功/失败由数据源 API 决定。
// 关键语义：
// - 命中缓存不更新“最近真实检查时间”，也不显示为“刚刚连接成功”。
// - 检查失败清空旧摘要，并经不触发数据库连接的列表接口同步后端最近检查时间；
//   无法同步时不编造数据库检查时间。
// - busy 标志防并发请求（重复点击/重试不会叠加）。
// - 状态保存稳定 message key，切换界面语言时文案实时更新，不重新请求。
import { computed, ref } from 'vue'
import { DatasourceRequestError, fetchDatasources, fetchSchema } from '@/api/datasource'
import type { DatasourceItem, SchemaSummary } from '@/types/datasource'
import { datasourceErrorText, formatLocaleTime, translate } from '@/i18n'
import type { MessageKey } from '@/i18n'

export type DatasourceViewState = 'loading' | 'disabled' | 'pending' | 'ok' | 'error' | 'checking'

/** 将底层错误映射为不泄露实现细节的友好文案。 */
export function friendlyDatasourceMessage(err: unknown): string {
  if (err instanceof DatasourceRequestError) {
    return datasourceErrorText(err.kind)
  }
  return datasourceErrorText('unknown')
}

function errorMessageKey(err: unknown): MessageKey {
  if (err instanceof DatasourceRequestError) {
    const kind = err.kind === 'invalid-response' ? 'invalidResponse' : err.kind
    return `datasource.${kind}` as MessageKey
  }
  return 'datasource.unknown'
}

/** 将 ISO 时间格式化为本地显示；无效或缺失返回占位符。 */
export function formatTime(iso: string | null): string {
  return formatLocaleTime(iso)
}

export function useDatasourceStatus() {
  const state = ref<DatasourceViewState>('loading')
  const messageKey = ref<MessageKey>('datasource.loading')
  const message = computed(() => translate(messageKey.value))
  const datasource = ref<DatasourceItem | null>(null)
  const schema = ref<SchemaSummary | null>(null)
  const warnings = ref<string[]>([])
  // 与 warnings 一一对应的稳定 code（未知告警没有 code），仅用于本地化固定文案；
  // 缺失或长度不一致时展示通用提示，不回显后端原文。
  const warningCodes = ref<string[]>([])
  const checkedAt = ref<string | null>(null)
  const busy = ref(false)  // 是否有在途请求，防并发

  function applyListStatus(status: DatasourceItem['status']): void {
    switch (status) {
      case 'disabled':
        state.value = 'disabled'
        messageKey.value = 'datasource.disabled'
        break
      case 'pending':
        state.value = 'pending'
        messageKey.value = 'datasource.pending'
        break
      case 'ok':
        state.value = 'ok'
        messageKey.value = 'datasource.ok'
        break
      case 'error':
        state.value = 'error'
        messageKey.value = 'datasource.error'
        break
    }
  }

  async function load(): Promise<void> {
    if (busy.value) return
    busy.value = true
    state.value = 'loading'
    messageKey.value = 'datasource.loading'
    try {
      const resp = await fetchDatasources()
      const items = resp.data.datasources
      if (items.length === 0) {
        state.value = 'error'
        messageKey.value = 'datasource.noInfo'
        return
      }
      const item = items[0]
      datasource.value = item
      checkedAt.value = item.checked_at
      applyListStatus(item.status)
    } catch (err) {
      state.value = 'error'
      messageKey.value = errorMessageKey(err)
    } finally {
      busy.value = false
    }
  }

  async function check(): Promise<void> {
    const id = datasource.value?.id
    if (!id) {
      state.value = 'error'
      messageKey.value = 'datasource.cannotCheck'
      return
    }
    if (busy.value) return
    busy.value = true
    state.value = 'checking'
    messageKey.value = 'datasource.checking'
    try {
      const resp = await fetchSchema(id)
      schema.value = resp.data.schema
      warnings.value = resp.data.warnings
      warningCodes.value = resp.data.warning_codes ?? []
      if (resp.data.cached) {
        // 命中缓存：未真实连接，保留原最近检查时间，不显示为刚刚连接成功。
        state.value = 'ok'
        messageKey.value = 'datasource.cached'
      } else {
        checkedAt.value = resp.data.fetched_at
        state.value = 'ok'
        messageKey.value = 'datasource.ok'
      }
    } catch (err) {
      state.value = 'error'
      messageKey.value = errorMessageKey(err)
      // 旧摘要不再展示，避免把历史数据当作当前结果
      schema.value = null
      warnings.value = []
      warningCodes.value = []
      await syncCheckedAtAfterFailure()
    } finally {
      busy.value = false
    }
  }

  /** 经列表接口同步后端最近真实检查时间；列表不触发数据库连接。 */
  async function syncCheckedAtAfterFailure(): Promise<void> {
    try {
      const resp = await fetchDatasources()
      const item = resp.data.datasources[0]
      if (item) {
        datasource.value = item
        checkedAt.value = item.checked_at
      }
    } catch {
      // 无法同步（如后端不可达）时，不编造数据库检查时间
      checkedAt.value = null
    }
  }

  /** 统一动作：未加载到数据源时重试列表，否则检查已知数据源。 */
  async function act(): Promise<void> {
    if (datasource.value === null) {
      await load()
    } else {
      await check()
    }
  }

  const canAct = computed(() => !busy.value && state.value !== 'disabled')

  return {
    state,
    message,
    datasource,
    schema,
    warnings,
    warningCodes,
    checkedAt,
    canAct,
    load,
    check,
    act,
  }
}
