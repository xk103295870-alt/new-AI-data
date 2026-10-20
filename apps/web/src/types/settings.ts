// 系统设置（LLM 参数 + 数据源连接信息/授权白名单）类型 + 运行时结构守卫。
// 后端返回的 JSON 属不可信输入，须在渲染/回填前逐层校验，避免把非法结构写入表单。

export interface LlmSettingsSnapshot {
  base_url: string
  model: string
  timeout_seconds: number
  max_tokens: number
  temperature: number
  configured: boolean
  /** 服务端只回传“是否已配置密钥”，永不回传真实密钥。 */
  api_key_set: boolean
}

export interface DatasourceSettingsSnapshot {
  enabled: boolean
  id: string
  name: string
  host: string
  port: number
  database: string
  user: string
  ssl_enabled: boolean
  /** 服务端只回传“是否已配置密码”，永不回传真实密码。 */
  password_set: boolean
  /** 授权白名单：对象名 → 授权列名列表。 */
  allowed_objects: Record<string, string[]>
}

export interface SettingsSnapshot {
  llm: LlmSettingsSnapshot
  datasource: DatasourceSettingsSnapshot
}

/** PUT /settings/llm 请求体；api_key 省略或为空字符串 = 保留原密钥。 */
export interface LlmSettingsPayload {
  base_url: string
  model: string
  timeout_seconds: number
  max_tokens: number
  temperature: number
  api_key?: string
}

/** POST /settings/llm/test 请求体；全部可选，未填字段由服务端回退到已保存值。 */
export interface LlmTestPayload {
  base_url?: string
  api_key?: string
  model?: string
  timeout_seconds?: number
  max_tokens?: number
  temperature?: number
}

export type LlmTestErrorCode =
  | 'config-invalid'
  | 'timeout'
  | 'unauthorized'
  | 'http-error'
  | 'network'
  | 'invalid-response'

export interface LlmTestResult {
  ok: boolean
  latency_ms: number | null
  error: LlmTestErrorCode | null
}

export interface LlmTestResponse {
  request_id: string
  status: 'completed'
  data: LlmTestResult
}

/** PUT /settings/datasource 请求体；password 省略或为空字符串 = 保留原密码。 */
export interface DatasourceSettingsPayload {
  enabled: boolean
  name: string
  host: string
  port: number
  database: string
  user: string
  ssl_enabled: boolean
  allowed_objects: Record<string, string[]>
  password?: string
}

/** POST /settings/datasource/test 请求体；全部可选，未填字段由服务端回退到已保存值。 */
export interface DatasourceTestPayload {
  host?: string
  port?: number
  database?: string
  user?: string
  password?: string
  ssl_enabled?: boolean
}

export type DatasourceTestErrorCode =
  | 'config-invalid'
  | 'timeout'
  | 'auth'
  | 'tls'
  | 'unavailable'
  | 'unknown'

export interface DatasourceTestResult {
  ok: boolean
  latency_ms: number | null
  error: DatasourceTestErrorCode | null
}

export interface DatasourceTestResponse {
  request_id: string
  status: 'completed'
  data: DatasourceTestResult
}

export interface DiscoveredColumn {
  name: string
  data_type: string
  nullable: boolean
  comment: string
}

export interface DiscoveredObject {
  name: string
  kind: 'table' | 'view'
  columns: DiscoveredColumn[]
}

export interface DiscoverObjectsData {
  objects: DiscoveredObject[]
  truncated: boolean
}

export interface DiscoverObjectsResponse {
  request_id: string
  status: 'completed'
  data: DiscoverObjectsData
}

export interface SettingsResponse {
  request_id: string
  status: 'completed'
  data: SettingsSnapshot
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isString(value: unknown): value is string {
  return typeof value === 'string'
}

function isBoolean(value: unknown): value is boolean {
  return typeof value === 'boolean'
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString)
}

function isAllowedObjects(value: unknown): value is Record<string, string[]> {
  if (!isRecord(value)) return false
  return Object.values(value).every(isStringArray)
}

export function isLlmSettingsSnapshot(value: unknown): value is LlmSettingsSnapshot {
  if (!isRecord(value)) return false
  return (
    isString(value.base_url) &&
    isString(value.model) &&
    isNumber(value.timeout_seconds) &&
    isNumber(value.max_tokens) &&
    isNumber(value.temperature) &&
    isBoolean(value.configured) &&
    isBoolean(value.api_key_set)
  )
}

export function isDatasourceSettingsSnapshot(
  value: unknown,
): value is DatasourceSettingsSnapshot {
  if (!isRecord(value)) return false
  return (
    isBoolean(value.enabled) &&
    isString(value.id) &&
    isString(value.name) &&
    isString(value.host) &&
    isNumber(value.port) &&
    isString(value.database) &&
    isString(value.user) &&
    isBoolean(value.ssl_enabled) &&
    isBoolean(value.password_set) &&
    isAllowedObjects(value.allowed_objects)
  )
}

export function isSettingsSnapshot(value: unknown): value is SettingsSnapshot {
  if (!isRecord(value)) return false
  return isLlmSettingsSnapshot(value.llm) && isDatasourceSettingsSnapshot(value.datasource)
}

export function isSettingsResponse(value: unknown): value is SettingsResponse {
  if (!isRecord(value)) return false
  if (!isString(value.request_id) || value.status !== 'completed') return false
  return isSettingsSnapshot(value.data)
}

function isLlmTestErrorCode(value: unknown): value is LlmTestErrorCode {
  return (
    value === 'config-invalid' ||
    value === 'timeout' ||
    value === 'unauthorized' ||
    value === 'http-error' ||
    value === 'network' ||
    value === 'invalid-response'
  )
}

export function isLlmTestResult(value: unknown): value is LlmTestResult {
  if (!isRecord(value)) return false
  return (
    isBoolean(value.ok) &&
    (value.latency_ms === null || isNumber(value.latency_ms)) &&
    (value.error === null || isLlmTestErrorCode(value.error))
  )
}

export function isLlmTestResponse(value: unknown): value is LlmTestResponse {
  if (!isRecord(value)) return false
  if (!isString(value.request_id) || value.status !== 'completed') return false
  return isLlmTestResult(value.data)
}

function isDatasourceTestErrorCode(value: unknown): value is DatasourceTestErrorCode {
  return (
    value === 'config-invalid' ||
    value === 'timeout' ||
    value === 'auth' ||
    value === 'tls' ||
    value === 'unavailable' ||
    value === 'unknown'
  )
}

export function isDatasourceTestResult(value: unknown): value is DatasourceTestResult {
  if (!isRecord(value)) return false
  return (
    isBoolean(value.ok) &&
    (value.latency_ms === null || isNumber(value.latency_ms)) &&
    (value.error === null || isDatasourceTestErrorCode(value.error))
  )
}

export function isDatasourceTestResponse(value: unknown): value is DatasourceTestResponse {
  if (!isRecord(value)) return false
  if (!isString(value.request_id) || value.status !== 'completed') return false
  return isDatasourceTestResult(value.data)
}

export function isDiscoveredColumn(value: unknown): value is DiscoveredColumn {
  if (!isRecord(value)) return false
  return (
    isString(value.name) &&
    isString(value.data_type) &&
    isBoolean(value.nullable) &&
    isString(value.comment)
  )
}

export function isDiscoveredObject(value: unknown): value is DiscoveredObject {
  if (!isRecord(value)) return false
  return (
    isString(value.name) &&
    (value.kind === 'table' || value.kind === 'view') &&
    Array.isArray(value.columns) &&
    value.columns.every(isDiscoveredColumn)
  )
}

export function isDiscoverObjectsResponse(value: unknown): value is DiscoverObjectsResponse {
  if (!isRecord(value)) return false
  if (!isString(value.request_id) || value.status !== 'completed') return false
  const data = value.data
  if (!isRecord(data)) return false
  return (
    Array.isArray(data.objects) &&
    data.objects.every(isDiscoveredObject) &&
    isBoolean(data.truncated)
  )
}
