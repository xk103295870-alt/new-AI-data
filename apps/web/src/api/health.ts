// 健康检查客户端：请求后端 /health，并把失败分类为
// http / network / invalid-response / timeout，供 UI 展示友好信息。
// 使用相对路径 /health，经 Vite 代理访问后端，避免浏览器跨域。
import { isHealthResponse, type HealthResponse } from '@/types/health'

export type HealthErrorKind = 'http' | 'network' | 'invalid-response' | 'timeout'

export class HealthRequestError extends Error {
  readonly kind: HealthErrorKind

  constructor(kind: HealthErrorKind, message: string) {
    super(message)
    this.name = 'HealthRequestError'
    this.kind = kind
  }
}

const DEFAULT_TIMEOUT_MS = 5000

/** 获取符合契约的健康响应；响应头和正文共用超时，失败抛出分类错误。 */
export async function fetchHealth(timeoutMs = DEFAULT_TIMEOUT_MS): Promise<HealthResponse> {
  const controller = new AbortController()
  // 超时中止：防止后端无响应时 UI 长时间停留在“检查中”。
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    let resp: Response
    try {
      resp = await fetch('/health', { signal: controller.signal })
    } catch (err) {
      // 区分“主动中止（超时）”与真实网络错误；两者都属于非成功路径。
      if (controller.signal.aborted) {
        throw new HealthRequestError('timeout', '健康检查超时')
      }
      throw new HealthRequestError('network', '无法连接后端服务')
    }

    if (!resp.ok) {
      throw new HealthRequestError('http', `后端返回状态码 ${resp.status}`)
    }

    let data: unknown
    try {
      data = await resp.json()
    } catch {
      // fetch 返回响应头后，读取正文仍可能因同一计时器中止；这不是 JSON 格式错误。
      if (controller.signal.aborted) {
        throw new HealthRequestError('timeout', '健康检查超时')
      }
      throw new HealthRequestError('invalid-response', '响应不是合法 JSON')
    }

    if (!isHealthResponse(data)) {
      throw new HealthRequestError('invalid-response', '响应结构不合法')
    }

    return data
  } finally {
    clearTimeout(timer)
  }
}
