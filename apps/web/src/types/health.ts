// 健康检查响应类型与运行时结构守卫。
// 后端返回的 JSON 属于不可信输入，需在渲染前校验结构，避免误判服务状态。
export interface HealthResponse {
  status: 'ok'
  service: 'ai-data-agent'
}

/** 仅接受约定服务的成功响应，避免将错误状态或其他服务误报为可用。 */
export function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== 'object' || value === null) return false
  const obj = value as Record<string, unknown>
  return obj.status === 'ok' && obj.service === 'ai-data-agent'
}
