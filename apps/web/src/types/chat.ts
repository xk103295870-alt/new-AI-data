// 聊天 API 类型与运行时结构守卫。
// 后端响应属不可信输入，渲染前必须逐层校验；错误信封只提取稳定 code/message。

export interface QueryInfo {
  row_count: number
  truncated: boolean
  source_id: string
}

export interface AnswerNote {
  code: string
  params: Record<string, string>
}

export interface ChatResponse {
  request_id: string
  session_id: string
  status: 'completed'
  answer: string
  query_info: QueryInfo | null
  warnings: string[]
  charts: unknown[]
  /** 模型自由回答原文；确定性注记单独下发，供前端按界面语言渲染。 */
  answer_base?: string | null
  /** 服务端固定回答 code；存在时前端不展示后端中文原文。 */
  answer_code?: string | null
  answer_params?: Record<string, string>
  answer_notes?: AnswerNote[]
}

export interface ChatRequestBody {
  session_id: string | null
  source_id: string
  message: string
}

export interface ErrorEnvelope {
  request_id: string
  status: 'error'
  error: {
    code: string
    message: string
    retryable: boolean
    /** 固定错误的安全结构化参数（如口径澄清 reason）；不解码后端自由文本。 */
    params?: Record<string, string>
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isStringRecord(value: unknown): value is Record<string, string> {
  if (!isRecord(value)) return false
  return Object.values(value).every(isString)
}

export function isAnswerNote(value: unknown): value is AnswerNote {
  if (!isRecord(value)) return false
  if (!isString(value.code) || value.code.length === 0) return false
  return value.params === undefined || isStringRecord(value.params)
}

function isString(value: unknown): value is string {
  return typeof value === 'string'
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString)
}

export function isQueryInfo(value: unknown): value is QueryInfo {
  if (!isRecord(value)) return false
  return (
    typeof value.row_count === 'number' &&
    Number.isInteger(value.row_count) &&
    value.row_count >= 0 &&
    typeof value.truncated === 'boolean' &&
    isString(value.source_id)
  )
}

export function isChatResponse(value: unknown): value is ChatResponse {
  if (!isRecord(value)) return false
  if (!isString(value.request_id) || !isString(value.session_id)) return false
  if (value.status !== 'completed' || !isString(value.answer)) return false
  if (value.query_info !== null && !isQueryInfo(value.query_info)) return false
  if (!isStringArray(value.warnings) || !Array.isArray(value.charts)) return false
  if (value.answer_base !== undefined && value.answer_base !== null && !isString(value.answer_base)) {
    return false
  }
  if (value.answer_code !== undefined && value.answer_code !== null && !isString(value.answer_code)) {
    return false
  }
  if (value.answer_params !== undefined && !isStringRecord(value.answer_params)) return false
  if (value.answer_notes !== undefined) {
    return Array.isArray(value.answer_notes) && value.answer_notes.every(isAnswerNote)
  }
  return true
}

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!isRecord(value)) return false
  if (!isString(value.request_id) || value.status !== 'error') return false
  const error = value.error
  if (!isRecord(error)) return false
  if (
    !isString(error.code) ||
    !isString(error.message) ||
    typeof error.retryable !== 'boolean'
  ) {
    return false
  }
  return error.params === undefined || isStringRecord(error.params)
}
