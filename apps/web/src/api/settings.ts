// 系统设置客户端：请求 /api/v1/settings 下 LLM 与数据源配置、连接测试与表结构发现。
// 失败分类为 http / network / invalid-response / timeout，供 UI 展示友好信息。
// 使用相对路径经 Vite 代理访问后端，避免浏览器跨域；密钥/密码永不回读。
import {
  isDatasourceTestResponse,
  isDiscoverObjectsResponse,
  isLlmTestResponse,
  isSettingsResponse,
  type DatasourceSettingsPayload,
  type DatasourceTestPayload,
  type DatasourceTestResponse,
  type DiscoverObjectsResponse,
  type LlmSettingsPayload,
  type LlmTestPayload,
  type LlmTestResponse,
  type SettingsResponse,
} from '@/types/settings'

export type SettingsErrorKind = 'http' | 'network' | 'invalid-response' | 'timeout'

export class SettingsRequestError extends Error {
  readonly kind: SettingsErrorKind

  constructor(kind: SettingsErrorKind, message: string) {
    super(message)
    this.name = 'SettingsRequestError'
    this.kind = kind
  }
}

// 快照读取与保存是轻量管理操作；连接测试与表结构发现需要真实建连，预算更长。
const DEFAULT_SETTINGS_TIMEOUT_MS = 15000
const LLM_TEST_TIMEOUT_MS = 30000
const DATASOURCE_TEST_TIMEOUT_MS = 25000
const DISCOVER_TABLES_TIMEOUT_MS = 25000

interface RequestOptions {
  method?: 'GET' | 'PUT' | 'POST'
  body?: unknown
  timeoutMs: number
}

/** 请求相对路径并返回解析后的 JSON；响应头与正文共用超时，失败抛分类错误。 */
async function requestJson(path: string, options: RequestOptions): Promise<unknown> {
  const { method = 'GET', body, timeoutMs } = options
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    let resp: Response
    try {
      resp = await fetch(path, {
        method,
        signal: controller.signal,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch (err) {
      if (controller.signal.aborted) {
        throw new SettingsRequestError('timeout', '设置请求超时')
      }
      throw new SettingsRequestError('network', '无法连接后端服务')
    }

    if (!resp.ok) {
      throw new SettingsRequestError('http', `后端返回状态码 ${resp.status}`)
    }

    try {
      return await resp.json()
    } catch {
      // 响应头已返回后读取正文仍可能因同一计时器中止，属超时而非非法 JSON。
      if (controller.signal.aborted) {
        throw new SettingsRequestError('timeout', '设置请求超时')
      }
      throw new SettingsRequestError('invalid-response', '响应不是合法 JSON')
    }
  } finally {
    clearTimeout(timer)
  }
}

/** 获取 LLM 与数据源设置快照；结构不符契约时归类为 invalid-response。 */
export async function fetchSettings(timeoutMs = DEFAULT_SETTINGS_TIMEOUT_MS): Promise<SettingsResponse> {
  const data = await requestJson('/api/v1/settings', { timeoutMs })
  if (!isSettingsResponse(data)) {
    throw new SettingsRequestError('invalid-response', '设置快照响应结构不合法')
  }
  return data
}

/** 保存 LLM 设置；api_key 省略或为空表示保留原密钥。 */
export async function updateLlmSettings(
  payload: LlmSettingsPayload,
  timeoutMs = DEFAULT_SETTINGS_TIMEOUT_MS,
): Promise<SettingsResponse> {
  const data = await requestJson('/api/v1/settings/llm', {
    method: 'PUT',
    body: payload,
    timeoutMs,
  })
  if (!isSettingsResponse(data)) {
    throw new SettingsRequestError('invalid-response', '保存 LLM 设置响应结构不合法')
  }
  return data
}

/** 测试 LLM 连接；未填字段由服务端回退到已保存值。 */
export async function testLlmSettings(
  payload: LlmTestPayload,
  timeoutMs = LLM_TEST_TIMEOUT_MS,
): Promise<LlmTestResponse> {
  const data = await requestJson('/api/v1/settings/llm/test', {
    method: 'POST',
    body: payload,
    timeoutMs,
  })
  if (!isLlmTestResponse(data)) {
    throw new SettingsRequestError('invalid-response', 'LLM 测试响应结构不合法')
  }
  return data
}

/** 保存数据源设置；password 省略或为空表示保留原密码，id 由服务端管理不传。 */
export async function updateDatasourceSettings(
  payload: DatasourceSettingsPayload,
  timeoutMs = DEFAULT_SETTINGS_TIMEOUT_MS,
): Promise<SettingsResponse> {
  const data = await requestJson('/api/v1/settings/datasource', {
    method: 'PUT',
    body: payload,
    timeoutMs,
  })
  if (!isSettingsResponse(data)) {
    throw new SettingsRequestError('invalid-response', '保存数据源设置响应结构不合法')
  }
  return data
}

/** 测试数据源连接；未填字段由服务端回退到已保存值。 */
export async function testDatasourceSettings(
  payload: DatasourceTestPayload,
  timeoutMs = DATASOURCE_TEST_TIMEOUT_MS,
): Promise<DatasourceTestResponse> {
  const data = await requestJson('/api/v1/settings/datasource/test', {
    method: 'POST',
    body: payload,
    timeoutMs,
  })
  if (!isDatasourceTestResponse(data)) {
    throw new SettingsRequestError('invalid-response', '数据源测试响应结构不合法')
  }
  return data
}

/** 发现数据库表结构（要求数据源已保存且可连接）；用于导入授权白名单。 */
export async function discoverDatasourceTables(
  timeoutMs = DISCOVER_TABLES_TIMEOUT_MS,
): Promise<DiscoverObjectsResponse> {
  const data = await requestJson('/api/v1/settings/datasource/tables', { timeoutMs })
  if (!isDiscoverObjectsResponse(data)) {
    throw new SettingsRequestError('invalid-response', '数据库表发现响应结构不合法')
  }
  return data
}
