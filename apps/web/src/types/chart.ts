// ChartSpec 前端类型与运行时守卫。
// 后端响应属于不可信输入：只有逐层校验通过的 ChartSpec 才允许进入渲染器；
// 非法图表被丢弃并降级为纯文字，绝不让任意结构/代码进入 ECharts。

export const CHART_TYPES = ['line', 'bar', 'pie', 'table', 'metric'] as const
export type ChartType = (typeof CHART_TYPES)[number]
export type ChartScalar = string | number | boolean | null

export interface ChartSpec {
  id: string
  type: ChartType
  title: string
  /** 服务端确定性标题的展示元数据；为空表示模型自由标题，保持原文。 */
  title_code?: string | null
  title_params?: Record<string, string>
  dataset: Record<string, ChartScalar>[]
  encoding: Record<string, string>
  unit: string | null
  /** 图表可用的查询结果行数（裁剪前）。 */
  source_row_count: number
  /** dataset 是否小于 source_row_count；与 query_info.truncated 独立。 */
  truncated: boolean
}

export const MAX_CHART_ROWS = 200

const MAX_TITLE_CHARS = 80
const MAX_UNIT_CHARS = 16
const MAX_FIELD_NAME_CHARS = 128
const CHART_ID_PATTERN = /^chart_[0-9]{3}$/
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/
const DANGEROUS_FIELDS = new Set(['__proto__', 'constructor', 'prototype'])
/** 与后端 CHART_TITLE_CODES 保持一致；前端不猜测其他 code 的语义。 */
export const TITLE_CODES: Record<string, readonly string[]> = {
  metric: ['y'],
  trend: ['x', 'y'],
  compare: ['x', 'y'],
  share: ['x', 'y'],
  table: [],
}
const SPEC_KEYS = new Set([
  'id',
  'type',
  'title',
  'title_code',
  'title_params',
  'dataset',
  'encoding',
  'unit',
  'source_row_count',
  'truncated',
])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/**
 * 数值必须在 JS 安全范围内：JSON 解析阶段超界整数会被静默改写，
 * 不能仅凭 Number.isFinite 放行。后端已把不安全数值转成无损字符串，
 * 此处若仍收到超界数字，说明来源不可信，整个 ChartSpec 拒绝渲染。
 */
function isSafeNumber(value: unknown): value is number {
  return (
    isFiniteNumber(value) &&
    (!Number.isInteger(value) || Number.isSafeInteger(value))
  )
}

function isScalar(value: unknown): value is ChartScalar {
  return (
    value === null ||
    typeof value === 'string' ||
    typeof value === 'boolean' ||
    isSafeNumber(value)
  )
}

function isSafeFieldName(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= MAX_FIELD_NAME_CHARS &&
    !CONTROL_CHARS.test(value) &&
    !DANGEROUS_FIELDS.has(value)
  )
}

function isCleanText(value: unknown, maxChars: number): value is string {
  return (
    typeof value === 'string' &&
    value.trim().length > 0 &&
    value.length <= maxChars &&
    !CONTROL_CHARS.test(value)
  )
}

function isEncoding(value: unknown): value is Record<string, string> {
  if (!isRecord(value)) return false
  return Object.entries(value).every(
    ([key, field]) => (key === 'x' || key === 'y') && isSafeFieldName(field),
  )
}

/** 标题展示元数据封闭校验：未知 code、多余/缺失参数一律拒绝。 */
function isTitleMetadata(value: Record<string, unknown>): boolean {
  const code = value.title_code
  const params = value.title_params
  if (code === undefined || code === null) {
    return params === undefined || (isRecord(params) && Object.keys(params).length === 0)
  }
  // 必须用自有属性判定：`code in TITLE_CODES` 会命中 Object.prototype 的
  // constructor/toString 等，随后把函数当作参数名数组调用而抛 TypeError，
  // 使整条成功回答被误判为失败。未知原型名一律按非法图表丢弃。
  if (typeof code !== 'string' || !Object.hasOwn(TITLE_CODES, code)) return false
  if (!isRecord(params)) return false
  const expected = TITLE_CODES[code]
  const keys = Object.keys(params)
  if (keys.length !== expected.length || !keys.every((key) => expected.includes(key))) {
    return false
  }
  return Object.values(params).every(isSafeFieldName)
}

export function isChartSpec(value: unknown): value is ChartSpec {
  if (!isRecord(value)) return false
  if (Object.keys(value).some((key) => !SPEC_KEYS.has(key))) return false
  if (typeof value.id !== 'string' || !CHART_ID_PATTERN.test(value.id)) return false
  if (!CHART_TYPES.includes(value.type as ChartType)) return false
  if (!isCleanText(value.title, MAX_TITLE_CHARS)) return false
  if (!isTitleMetadata(value)) return false
  if (value.unit !== null && !isCleanText(value.unit, MAX_UNIT_CHARS)) return false
  if (!Array.isArray(value.dataset) || value.dataset.length > MAX_CHART_ROWS) return false
  if (!isEncoding(value.encoding)) return false

  const dataset = value.dataset
  if (
    typeof value.source_row_count !== 'number' ||
    !Number.isSafeInteger(value.source_row_count) ||
    value.source_row_count < 0 ||
    typeof value.truncated !== 'boolean'
  ) {
    return false
  }
  if (value.source_row_count < dataset.length) return false
  if (value.truncated !== value.source_row_count > dataset.length) return false
  for (const row of dataset) {
    if (!isRecord(row) || Object.keys(row).length === 0) return false
    for (const [field, cell] of Object.entries(row)) {
      if (!isSafeFieldName(field) || !isScalar(cell)) return false
    }
  }

  const encodingKeys = Object.keys(value.encoding)
  if (value.type === 'table') {
    return encodingKeys.length === 0
  }

  if (value.type === 'metric') {
    if (encodingKeys.length !== 1 || encodingKeys[0] !== 'y') return false
    if (dataset.length !== 1) return false
    return isSafeNumber(dataset[0][value.encoding.y])
  }

  if (encodingKeys.length !== 2) return false
  const x = value.encoding.x
  const y = value.encoding.y
  if (!x || !y || dataset.length === 0) return false
  return dataset.every((row) => {
    if (!(x in row) || !(y in row)) return false
    const measure = row[y]
    return isScalar(row[x]) && (measure === null || isSafeNumber(measure))
  })
}

/** 单条校验；失败返回 null，调用方降级为纯文字。 */
export function parseChartSpec(value: unknown): ChartSpec | null {
  return isChartSpec(value) ? value : null
}

/** 过滤掉非法图表而不是拒绝整条聊天响应，保证文字结论仍可展示。 */
export function parseChartSpecs(value: unknown): ChartSpec[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => parseChartSpec(item))
    .filter((item): item is ChartSpec => item !== null)
}
