// 数据源客户端：请求 /api/v1/datasources 与 schema，并校验响应结构。
// 失败分类为 http / network / invalid-response / timeout，供 UI 展示友好信息。
// 使用相对路径经 Vite 代理访问后端，避免浏览器跨域。
import {
  isDatasourcesResponse,
  isSchemaResponse,
  type DatasourcesResponse,
  type SchemaResponse,
} from '@/types/datasource'

export type DatasourceErrorKind = 'http' | 'network' | 'invalid-response' | 'timeout'

export class DatasourceRequestError extends Error {
  readonly kind: DatasourceErrorKind

  constructor(kind: DatasourceErrorKind, message: string) {
    super(message)
    this.name = 'DatasourceRequestError'
    this.kind = kind
  }
}

// 列表请求快（不触发数据库连接），使用较短预算。
const DEFAULT_LIST_TIMEOUT_MS = 8000
// 后端启用配置强制 0 < 阶段预算 <= 总预算 <= 15s（datasource/config.py）；
// Schema 请求固定 20s，覆盖所有合法配置并保留至少 5s 传输余量。
const DEFAULT_SCHEMA_TIMEOUT_MS = 20000

/** 请求相对路径并返回解析后的 JSON；响应头与正文共用超时，失败抛分类错误。 */
async function requestJson(path: string, timeoutMs: number): Promise<unknown> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    let resp: Response
    try {
      resp = await fetch(path, { signal: controller.signal })
    } catch (err) {
      if (controller.signal.aborted) {
        throw new DatasourceRequestError('timeout', '数据源请求超时')
      }
      throw new DatasourceRequestError('network', '无法连接后端服务')
    }

    if (!resp.ok) {
      throw new DatasourceRequestError('http', `后端返回状态码 ${resp.status}`)
    }

    try {
      return await resp.json()
    } catch {
      // 响应头已返回后读取正文仍可能因同一计时器中止，属超时而非非法 JSON。
      if (controller.signal.aborted) {
        throw new DatasourceRequestError('timeout', '数据源请求超时')
      }
      throw new DatasourceRequestError('invalid-response', '响应不是合法 JSON')
    }
  } finally {
    clearTimeout(timer)
  }
}

/** 获取数据源列表；结构不符契约时归类为 invalid-response。 */
export async function fetchDatasources(timeoutMs = DEFAULT_LIST_TIMEOUT_MS): Promise<DatasourcesResponse> {
  const data = await requestJson('/api/v1/datasources', timeoutMs)
  if (!isDatasourcesResponse(data)) {
    throw new DatasourceRequestError('invalid-response', '数据源列表响应结构不合法')
  }
  return data
}

/** 获取指定数据源的授权 Schema；source_id 只作为路径标识，浏览器不传连接信息。 */
export async function fetchSchema(sourceId: string, timeoutMs = DEFAULT_SCHEMA_TIMEOUT_MS): Promise<SchemaResponse> {
  const data = await requestJson(`/api/v1/datasources/${encodeURIComponent(sourceId)}/schema`, timeoutMs)
  if (!isSchemaResponse(data)) {
    throw new DatasourceRequestError('invalid-response', '数据源 Schema 响应结构不合法')
  }
  return data
}
