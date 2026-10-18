// 聊天客户端：POST /api/v1/chat，校验成功响应与错误信封结构。
// 失败分类为 http / network / invalid-response / timeout，供 UI 展示友好信息；
// 错误信封只提取稳定的 code/message/retryable，不把任意响应文本直接当作结论。
import {
  isChatResponse,
  isErrorEnvelope,
  type ChatRequestBody,
  type ChatResponse,
} from '@/types/chat'

export type ChatErrorKind = 'http' | 'network' | 'invalid-response' | 'timeout'

export class ChatRequestError extends Error {
  readonly kind: ChatErrorKind
  readonly code?: string
  readonly retryable: boolean
  /** 仅接受字符串字典；用于本地化固定错误，不承载后端自由文本。 */
  readonly params: Record<string, string>

  constructor(
    kind: ChatErrorKind,
    message: string,
    code?: string,
    retryable = false,
    params: Record<string, string> = {},
  ) {
    super(message)
    this.name = 'ChatRequestError'
    this.kind = kind
    this.code = code
    this.retryable = retryable
    this.params = params
  }
}

// Agent 端整体预算默认 60s（服务端 agent_request_timeout_seconds）；
// 前端留出传输余量，避免服务端已超时而浏览器仍在无限等待。
const DEFAULT_CHAT_TIMEOUT_MS = 90_000

/** 发送一轮对话；session_id 为 null 时由后端创建新会话。 */
export async function sendChat(
  body: ChatRequestBody,
  timeoutMs = DEFAULT_CHAT_TIMEOUT_MS,
): Promise<ChatResponse> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    let resp: Response
    try {
      resp = await fetch('/api/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: body.session_id,
          source_id: body.source_id,
          message: body.message,
        }),
        signal: controller.signal,
      })
    } catch {
      if (controller.signal.aborted) {
        throw new ChatRequestError('timeout', '分析请求超时')
      }
      throw new ChatRequestError('network', '无法连接后端服务')
    }

    if (!resp.ok) {
      let envelope: unknown = null
      try {
        envelope = await resp.json()
      } catch {
        // 错误体不是 JSON 时仍按 HTTP 状态归类，不展示原始正文。
        if (controller.signal.aborted) {
          throw new ChatRequestError('timeout', '分析请求超时')
        }
      }
      if (isErrorEnvelope(envelope)) {
        throw new ChatRequestError(
          'http',
          envelope.error.message,
          envelope.error.code,
          envelope.error.retryable,
          envelope.error.params ?? {},
        )
      }
      throw new ChatRequestError('http', `后端返回状态码 ${resp.status}`)
    }

    let data: unknown
    try {
      data = await resp.json()
    } catch {
      if (controller.signal.aborted) {
        throw new ChatRequestError('timeout', '分析请求超时')
      }
      throw new ChatRequestError('invalid-response', '分析结果不是合法 JSON')
    }

    if (!isChatResponse(data)) {
      throw new ChatRequestError('invalid-response', '分析结果结构不合法')
    }
    return data
  } finally {
    clearTimeout(timer)
  }
}
