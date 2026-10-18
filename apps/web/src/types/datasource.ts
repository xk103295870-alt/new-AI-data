// 数据源列表与 Schema 响应类型 + 运行时结构守卫。
// 后端返回的 JSON 属不可信输入，须在渲染前逐层校验，避免误判数据源状态。

export type DatasourceStatus = 'disabled' | 'pending' | 'ok' | 'error'

export interface DatasourceItem {
  id: string
  name: string
  configured: boolean
  status: DatasourceStatus
  checked_at: string | null
}

export interface DatasourcesResponse {
  request_id: string
  status: 'completed'
  data: { datasources: DatasourceItem[] }
}

export interface ColumnInfo {
  name: string
  data_type: string
  nullable: boolean
  comment: string
}

export interface ForeignKey {
  name: string
  columns: string[]
  referenced_object: string
  referenced_columns: string[]
}

export interface SchemaObject {
  name: string
  kind: 'table' | 'view'
  comment: string
  columns: ColumnInfo[]
  primary_key: string[]
  foreign_keys: ForeignKey[]
}

export interface SchemaSummary {
  objects: SchemaObject[]
}

export interface SchemaResponse {
  request_id: string
  status: 'completed'
  data: {
    source_id: string
    fetched_at: string
    cached: boolean
    warnings: string[]
    /** 与 warnings 对齐的稳定告警 code；旧后端缺失时展示通用提示，不回显原文。 */
    warning_codes?: string[]
    schema: SchemaSummary
  }
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

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString)
}

function isDatasourceStatus(value: unknown): value is DatasourceStatus {
  return value === 'disabled' || value === 'pending' || value === 'ok' || value === 'error'
}

export function isDatasourceItem(value: unknown): value is DatasourceItem {
  if (!isRecord(value)) return false
  return (
    isString(value.id) &&
    isString(value.name) &&
    isBoolean(value.configured) &&
    isDatasourceStatus(value.status) &&
    (value.checked_at === null || isString(value.checked_at))
  )
}

export function isDatasourcesResponse(value: unknown): value is DatasourcesResponse {
  if (!isRecord(value)) return false
  if (!isString(value.request_id) || value.status !== 'completed') return false
  const data = value.data
  if (!isRecord(data)) return false
  const items = data.datasources
  return Array.isArray(items) && items.every(isDatasourceItem)
}

export function isColumnInfo(value: unknown): value is ColumnInfo {
  if (!isRecord(value)) return false
  return (
    isString(value.name) &&
    isString(value.data_type) &&
    isBoolean(value.nullable) &&
    isString(value.comment)
  )
}

export function isForeignKey(value: unknown): value is ForeignKey {
  if (!isRecord(value)) return false
  return (
    isString(value.name) &&
    isStringArray(value.columns) &&
    isString(value.referenced_object) &&
    isStringArray(value.referenced_columns)
  )
}

export function isSchemaObject(value: unknown): value is SchemaObject {
  if (!isRecord(value)) return false
  return (
    isString(value.name) &&
    (value.kind === 'table' || value.kind === 'view') &&
    isString(value.comment) &&
    Array.isArray(value.columns) &&
    value.columns.every(isColumnInfo) &&
    isStringArray(value.primary_key) &&
    Array.isArray(value.foreign_keys) &&
    value.foreign_keys.every(isForeignKey)
  )
}

export function isSchemaSummary(value: unknown): value is SchemaSummary {
  if (!isRecord(value)) return false
  return Array.isArray(value.objects) && value.objects.every(isSchemaObject)
}

export function isSchemaResponse(value: unknown): value is SchemaResponse {
  if (!isRecord(value)) return false
  if (!isString(value.request_id) || value.status !== 'completed') return false
  const data = value.data
  if (!isRecord(data)) return false
  return (
    isString(data.source_id) &&
    isString(data.fetched_at) &&
    isBoolean(data.cached) &&
    isStringArray(data.warnings) &&
    (data.warning_codes === undefined || isStringArray(data.warning_codes)) &&
    isSchemaSummary(data.schema)
  )
}
