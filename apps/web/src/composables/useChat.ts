// 聊天状态机：维护消息列表、加载态、错误与会话 ID。
// 约束：
// - 同一时间只允许一个在途请求，重复提交直接忽略；
// - 失败以“错误消息”追加，不伪造 AI 结论；
// - 仅服务端返回 session_id 后复用它，前端不自行生成会话标识；
// - 会话失效时清空本地 session_id，下一次发送重新创建会话。
// - 界面语言只影响固定展示文案；用户问题、模型自由回答与业务数据保持原文。
import { computed, ref } from 'vue'
import { ChatRequestError, sendChat } from '@/api/chat'
import type { AnswerNote, QueryInfo } from '@/types/chat'
import { parseChartSpecs } from '@/types/chart'
import type { ChartSpec } from '@/types/chart'
import { answerText, chatErrorText, noteText } from '@/i18n'

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  queryInfo?: QueryInfo
  warnings?: string[]
  /** 仅保留通过运行时校验的图表；非法项被丢弃，不影响文字结论。 */
  charts?: ChartSpec[]
  isError?: boolean
  /** 模型自由回答原文；不存在时回退 content（旧后端/历史消息）。 */
  answerBase?: string
  /** 服务端固定回答 code，前端不再展示其后端中文原文。 */
  answerCode?: string
  answerParams?: Record<string, string>
  /** 服务端确定性注记（日期区间验证等），按当前界面语言渲染。 */
  answerNotes?: AnswerNote[]
  /** 错误的结构化来源；用于语言切换后重新本地化，不保留后端自由文本。 */
  errorKind?: string
  errorCode?: string
  errorParams?: Record<string, string>
}

export type ChatSendState = 'idle' | 'sending'

/** 将结构化错误映射为当前界面语言的友好文案；不使用后端自由 message。 */
export function friendlyChatMessage(err: unknown): string {
  if (err instanceof ChatRequestError) {
    return chatErrorText(err.kind, err.code, err.params)
  }
  return chatErrorText('unknown')
}

/** 渲染一条消息的显示文本；computed 中调用可随界面语言切换实时更新。 */
export function displayChatMessage(message: ChatMessage): string {
  if (message.role === 'user') return message.content
  if (message.isError) {
    return chatErrorText(message.errorKind ?? 'unknown', message.errorCode, message.errorParams)
  }
  const base = message.answerCode
    ? answerText(message.answerCode)
    : (message.answerBase ?? message.content)
  const notes = (message.answerNotes ?? []).map((note) => noteText(note)).filter(Boolean)
  return notes.length ? `${base}\n\n${notes.join('\n\n')}` : base
}

export function useChat() {
  const messages = ref<ChatMessage[]>([])
  const state = ref<ChatSendState>('idle')
  const errorMessage = ref('')
  const sessionId = ref<string | null>(null)
  const busy = computed(() => state.value === 'sending')

  /** 发送一条问题；sourceId 必须来自后端数据源列表，不在前端拼接。 */
  async function send(text: string, sourceId: string): Promise<void> {
    const content = text.trim()
    if (!content || !sourceId || busy.value) return

    messages.value.push({ role: 'user', content })
    state.value = 'sending'
    errorMessage.value = ''
    try {
      const resp = await sendChat({
        session_id: sessionId.value,
        source_id: sourceId,
        message: content,
      })
      sessionId.value = resp.session_id
      const charts = parseChartSpecs(resp.charts)
      messages.value.push({
        role: 'assistant',
        content: resp.answer,
        queryInfo: resp.query_info ?? undefined,
        warnings: resp.warnings,
        // 仅在有合法图表时附带字段，保持无图表消息的结构稳定。
        ...(charts.length ? { charts } : {}),
        // 展示元数据按存在性附加，兼容旧后端与既有历史消息结构。
        ...(resp.answer_base ? { answerBase: resp.answer_base } : {}),
        ...(resp.answer_code ? { answerCode: resp.answer_code } : {}),
        ...(resp.answer_code
          ? { answerParams: { ...(resp.answer_params ?? {}) } }
          : {}),
        ...(resp.answer_notes?.length ? { answerNotes: resp.answer_notes } : {}),
      })
    } catch (err) {
      errorMessage.value = friendlyChatMessage(err)
      if (err instanceof ChatRequestError && err.code === 'SESSION_NOT_FOUND') {
        // 会话已失效：丢弃旧 ID，避免后续请求持续失败。
        sessionId.value = null
      }
      const structured =
        err instanceof ChatRequestError
          ? {
              errorKind: err.kind,
              ...(err.code ? { errorCode: err.code } : {}),
              ...(Object.keys(err.params).length ? { errorParams: { ...err.params } } : {}),
            }
          : { errorKind: 'unknown' }
      messages.value.push({
        role: 'assistant',
        content: errorMessage.value,
        isError: true,
        ...structured,
      })
    } finally {
      state.value = 'idle'
    }
  }

  /** 新建对话：清空消息、会话 ID 与错误；不取消已在途请求（按钮同时禁用）。 */
  function reset(): void {
    if (busy.value) return
    messages.value = []
    sessionId.value = null
    state.value = 'idle'
    errorMessage.value = ''
  }

  return { messages, state, errorMessage, sessionId, busy, send, reset }
}
