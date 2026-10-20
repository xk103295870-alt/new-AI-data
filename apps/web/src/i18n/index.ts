// 轻量界面国际化（i18n）：集中维护中英文静态文案，不引入运行时翻译服务。
// 设计边界：
// - 首次访问固定英文；只有用户在界面切换后才写入 localStorage 语言标识；
// - localStorage 仅保存 'en'/'zh'，不保存 Key、业务数据或对话内容；
// - 服务端固定错误/警告/澄清使用稳定 code + 安全字符串参数映射，不把任意
//   后端文本当 HTML，也不翻译模型自由回答、数据库对象名或真实分类值；
// - 所有文案通过 Vue 文本插值渲染，禁止 v-html/innerHTML。
import { ref } from 'vue'

export type Locale = 'en' | 'zh'

const STORAGE_KEY = 'ai-data-agent.ui-locale'

const en = {
  'app.title': 'AI Business Data Analysis',
  'app.newConversation': '＋ New conversation',
  'locale.label': 'Interface language',
  'locale.en': 'English',
  'locale.zh': '中文',

  'service.checking': 'Checking backend service…',
  'service.available': 'Backend service available',
  'service.timeout': 'Backend service timed out',
  'service.network': 'Cannot connect to backend service',
  'service.invalidResponse': 'Backend returned an unrecognized response',
  'service.http': 'Backend service temporarily unavailable',
  'service.unknown': 'Unknown error while checking backend service',

  'datasource.loading': 'Loading datasource status…',
  'datasource.checking': 'Checking datasource…',
  'datasource.disabled': 'Datasource not configured',
  'datasource.pending': 'Datasource not checked yet',
  'datasource.ok': 'Last check succeeded',
  'datasource.error': 'Last check failed',
  'datasource.cached': 'Cached result from the last check',
  'datasource.noInfo': 'The backend did not return datasource information',
  'datasource.cannotCheck': 'Datasource is not configured and cannot be checked',
  'datasource.timeout': 'Datasource request timed out',
  'datasource.network': 'Cannot connect to backend service',
  'datasource.invalidResponse': 'Backend returned an unrecognized datasource response',
  'datasource.http': 'Datasource service temporarily unavailable',
  'datasource.unknown': 'Unknown error while checking the datasource',
  'datasource.checkTime': 'Checked at: {time}',
  'datasource.check': 'Check',
  'datasource.recheck': 'Check again',
  'datasource.retryLoad': 'Retry loading',
  'datasource.retry': 'Retry',
  'datasource.disabledHint':
    'Datasource is not enabled and cannot be checked (an authorized database must be configured on the server).',
  'datasource.securityNotice': 'Security notice:',
  'datasource.schemaSummary': 'Authorized objects and fields:',
  'datasource.view': 'View',
  'datasource.table': 'Table',

  'schemaWarning.SCHEMA_PARTIAL_PRIMARY_KEY':
    'The primary key was omitted because some key columns are not visible in the authorized scope.',
  'schemaWarning.SCHEMA_DROPPED_FOREIGN_KEY':
    'Some foreign keys were ignored because the referenced database, object, or columns are not authorized.',
  'schemaWarning.unknown':
    'The backend reported a schema warning that the application cannot identify. Please verify the authorized scope and object structure.',

  'chat.aria': 'Chat area',
  'chat.roleUser': 'Me',
  'chat.roleAssistant': 'AI',
  'chat.queryInfo': 'Query rows: {rows} · {truncation} · Source: {source}',
  'chat.truncated': 'results truncated',
  'chat.notTruncated': 'results not truncated',
  'chat.notice': 'Notice:',
  'chat.loading': 'Analyzing…',
  'chat.empty': 'No conversation yet. Enter a question to start.',
  'chat.readyHint':
    'AI conclusions are based on the authorized datasource; verify row counts and business definitions.',
  'chat.sourceLoading': 'Loading datasources…',
  'chat.sourceUnavailable': 'Datasource is not configured or enabled; analysis cannot start.',
  'chat.sourceErrorSuffix': '{message}. Analysis cannot start.',
  'chat.sourceNoInfo': 'The backend did not return datasource information',
  'chat.inputAria': 'Chat input',
  'chat.send': 'Send',
  'chat.placeholder':
    'Enter your business question, e.g. What was the sales amount in the last 30 days?',
  'chat.error.timeout': 'The analysis request timed out. Please try again later.',
  'chat.error.network': 'Cannot connect to backend service. Please try again later.',
  'chat.error.invalidResponse': 'The backend returned an unrecognized analysis result.',
  'chat.error.unknown': 'Analysis failed. Please try again later.',
  'chat.error.MODEL_UNAVAILABLE': 'The model service is temporarily unavailable. Please try again later.',
  'chat.error.MODEL_TIMEOUT': 'The model response timed out. Please try again later.',
  'chat.error.MODEL_RESPONSE_INVALID':
    'The model returned invalid content. Please retry or adjust your question.',
  'chat.error.AGENT_UNAVAILABLE': 'The analysis service is temporarily unavailable. Please try again later.',
  'chat.error.AGENT_TIMEOUT': 'The analysis timed out. Please narrow the question and try again.',
  'chat.error.SESSION_NOT_FOUND': 'The conversation no longer exists. Please ask again.',
  'chat.error.CHAT_INVALID_REQUEST': 'The request is invalid. Please check your question and try again.',
  'chat.error.INVALID_REQUEST': 'The request is invalid. Please check your question and try again.',
  'chat.error.AMBIGUOUS_TIME_RANGE':
    'The question contains multiple, negated, or conflicting time expressions. Please specify a single time range and try again.',
  'chat.error.AMBIGUOUS_METRIC.MEMBER_TRADE_LINK':
    'The link key between members and consumption is not confirmed, so member consumption cannot be linked and unique members cannot be calculated. Record rows cannot stand in for people. Please provide a confirmed member identifier.',
  'chat.error.AMBIGUOUS_METRIC.NET_REVENUE':
    'Net revenue depends on unconfirmed treatment of refunds, discounts, and subsidies. Please clarify what should be deducted.',
  'chat.error.AMBIGUOUS_METRIC.UNIQUE_ORDER':
    'The authorized views have no stable order identifier, so unique orders cannot be calculated. Rows cannot stand in for orders. Please provide a confirmed order identifier.',
  'chat.error.AMBIGUOUS_METRIC.GENERIC':
    'The metric definition is not confirmed. Please clarify the calculation, time range, or field meaning.',

  'answer.POLICY_BLOCKED':
    'This request exceeds the current authorization scope or uses an unsupported SQL structure, so execution stopped. Please adjust the question or ask an administrator to confirm the authorized scope.',
  'answer.SQL_RETRY_EXHAUSTED':
    'The generated SQL repeatedly failed syntax validation, so this analysis stopped. Please adjust your question and try again.',
  'answer.TOOL_CALL_LIMIT':
    'The analysis exceeded the step limit and stopped. Please narrow the question and try again.',
  'answer.TOOL_ARGUMENT_INVALID':
    'The model returned a tool call that could not be executed, so this analysis stopped.',
  'answer.QUERY_TIMEOUT': 'The query timed out. Please narrow the time or statistical range and try again.',
  'answer.QUERY_BUSY': 'The query service is busy. Please try again later.',
  'answer.QUERY_RESULT_TOO_LARGE': 'The query result is too large. Please narrow the statistical range and try again.',
  'answer.QUERY_FAILED': 'The query failed. Please try again later.',
  'answer.unknown': 'The analysis service stopped processing this request.',

  'warning.UNVERIFIED': 'This answer was not verified by a database query; rely on actual query results.',
  'warning.DATE_RANGE_UNVERIFIED':
    'The requested date range could not be verified from the executed query.',
  'warning.TOOL_CALL_LIMIT': 'Analysis stopped because the step limit was exceeded.',
  'warning.SQL_RETRY_EXHAUSTED': 'The generated SQL repeatedly failed syntax validation.',
  'warning.POLICY_BLOCKED': 'Execution was blocked by the authorization policy.',
  'warning.TOOL_ARGUMENT_INVALID': 'A tool call could not be executed.',
  'warning.QUERY_TIMEOUT': 'The query timed out.',
  'warning.QUERY_BUSY': 'The query service was busy.',
  'warning.QUERY_RESULT_TOO_LARGE': 'The query result was too large.',
  'warning.QUERY_FAILED': 'The query failed.',
  'warning.unknown': 'The analysis service reported an additional issue.',

  'note.DATE_RANGE_VERIFIED':
    '(In this answer, “last {days} days” uses natural-day rules in the application timezone {timezone}; the range is >= {start} and < {end}. This rule is a system-wide convention and has not been confirmed as the official business definition.)',
  'note.DATE_RANGE_UNVERIFIED':
    '(This question mentioned “last {days} days”, but the server could not verify the target date field and half-open range from the executed query, so the date range is unverified. The conclusion above should not be read as statistics for that time range. If you need a time-based interpretation, please specify the range and try again.)',
  'note.UNVERIFIED':
    '(Note: this answer was not verified by a database query. Rely on actual query results.)',

  'chart.ariaFallback': 'Chart',
  'chart.empty': 'No data to display',
  'chart.pieNegative':
    'The metric contains negative values and is not suitable for a share chart; showing a table instead.',
  'chart.pieNoPositive':
    'No positive values are available for a share chart; showing a table instead.',
  'chart.truncated': 'Chart shows only the first {shown} rows ({source} rows returned)',
  'chart.queryTruncated': 'Query results were truncated; the chart shows only the returned subset.',
  'chart.invalid': 'Invalid chart data',
  'chart.unitUnconfirmed': 'Unit not confirmed',
  'chart.unitWrapped': ' ({unit})',
  'chart.title.metric': '{y}',
  'chart.title.trend': '{y} trend over {x}',
  'chart.title.compare': '{y} by {x}',
  'chart.title.share': '{y} share by {x}',
  'chart.title.table': 'Query result',
  'app.settings': 'Settings',

  'settings.title': 'System settings',
  'settings.tabLlm': 'LLM',
  'settings.tabDatasource': 'Datasource',
  'settings.field.baseUrl': 'Base URL',
  'settings.field.apiKey': 'API key',
  'settings.field.model': 'Model',
  'settings.field.timeout': 'Timeout (seconds)',
  'settings.field.maxTokens': 'Max tokens',
  'settings.field.temperature': 'Temperature',
  'settings.field.enabled': 'Enable datasource',
  'settings.field.name': 'Datasource name',
  'settings.field.host': 'Host',
  'settings.field.port': 'Port',
  'settings.field.database': 'Database',
  'settings.field.user': 'User',
  'settings.field.password': 'Password',
  'settings.field.ssl': 'Enable SSL',
  'settings.secretSaved': 'Saved. Leave empty to keep it unchanged',
  'settings.test': 'Test connection',
  'settings.save': 'Save',
  'settings.testing': 'Testing\u2026',
  'settings.saving': 'Saving\u2026',
  'settings.saved': 'Settings saved',
  'settings.loadFailed': 'Failed to load settings',
  'settings.testSuccess': 'Connection succeeded ({ms} ms)',
  'settings.testFailed': 'Connection failed: {reason}',
  'settings.request.timeout': 'Settings request timed out',
  'settings.request.network': 'Cannot connect to backend service',
  'settings.request.invalid-response': 'Backend returned an unrecognized settings response',
  'settings.request.http': 'Settings service temporarily unavailable',
  'settings.llmTestError.config-invalid': 'LLM configuration is incomplete or invalid',
  'settings.llmTestError.timeout': 'LLM request timed out',
  'settings.llmTestError.unauthorized': 'The API key was rejected (unauthorized)',
  'settings.llmTestError.http-error': 'The LLM service returned an HTTP error',
  'settings.llmTestError.network': 'Cannot reach the LLM service',
  'settings.llmTestError.invalid-response': 'The LLM service returned an unrecognized response',
  'settings.llmTestError.unknown': 'LLM connection failed',
  'settings.dsTestError.config-invalid': 'Datasource configuration is incomplete or invalid',
  'settings.dsTestError.timeout': 'Datasource connection timed out',
  'settings.dsTestError.auth': 'Database authentication failed; check user and password',
  'settings.dsTestError.tls': 'TLS/SSL handshake failed; check SSL settings',
  'settings.dsTestError.unavailable': 'Database is unreachable; check host and port',
  'settings.dsTestError.unknown': 'Datasource connection failed',
  'settings.discoverTitle': 'Import authorized tables',
  'settings.loadTables': 'Load database tables',
  'settings.loadingTables': 'Loading tables\u2026',
  'settings.loadTablesFailed': 'Failed to load database tables',
  'settings.tablesEmpty': 'No tables found in the database',
  'settings.selectAll': 'Select all',
  'settings.clearAll': 'Clear',
  'settings.importSelected': 'Import selected tables',
  'settings.noTablesSelected': 'Select at least one table to import',
  'settings.columns': '{n} columns',
  'settings.allowedSummary': 'Authorized objects ({count}):',
  'settings.allowedItem': '{name} ({cols})',
} as const

export type MessageKey = keyof typeof en

const zh: Record<MessageKey, string> = {
  'app.title': 'AI 商业数据分析',
  'app.newConversation': '＋ 新建对话',
  'locale.label': '界面语言',
  'locale.en': 'English',
  'locale.zh': '中文',

  'service.checking': '正在检查后端服务…',
  'service.available': '后端服务可用',
  'service.timeout': '后端服务响应超时',
  'service.network': '无法连接后端服务',
  'service.invalidResponse': '后端返回了无法识别的响应',
  'service.http': '后端服务暂不可用',
  'service.unknown': '检查后端服务时发生未知错误',

  'datasource.loading': '正在加载数据源状态…',
  'datasource.checking': '正在检查数据源…',
  'datasource.disabled': '数据源未配置',
  'datasource.pending': '数据源待检查',
  'datasource.ok': '最近检查成功',
  'datasource.error': '最近检查失败',
  'datasource.cached': '命中缓存，最近检查结果如下',
  'datasource.noInfo': '后端未返回数据源信息',
  'datasource.cannotCheck': '数据源未配置，无法检查',
  'datasource.timeout': '数据源请求超时',
  'datasource.network': '无法连接后端服务',
  'datasource.invalidResponse': '后端返回了无法识别的数据源响应',
  'datasource.http': '数据源服务暂不可用',
  'datasource.unknown': '检查数据源时发生未知错误',
  'datasource.checkTime': '检查时间：{time}',
  'datasource.check': '检查',
  'datasource.recheck': '重新检查',
  'datasource.retryLoad': '重试加载',
  'datasource.retry': '重试',
  'datasource.disabledHint': '未启用数据源，无法检查（需在服务端配置授权数据库）。',
  'datasource.securityNotice': '安全提示：',
  'datasource.schemaSummary': '授权对象与字段：',
  'datasource.view': '视图',
  'datasource.table': '表',

  'schemaWarning.SCHEMA_PARTIAL_PRIMARY_KEY': '部分主键列因授权范围未完整可见，已省略该主键',
  'schemaWarning.SCHEMA_DROPPED_FOREIGN_KEY': '部分外键因引用未授权库、对象或列被忽略',
  'schemaWarning.unknown': '后端报告了当前界面无法识别的 Schema 告警，请核对授权范围与对象结构。',

  'chat.aria': '聊天区',
  'chat.roleUser': '我',
  'chat.roleAssistant': 'AI',
  'chat.queryInfo': '查询行数：{rows} · {truncation} · 来源：{source}',
  'chat.truncated': '结果已截断',
  'chat.notTruncated': '结果未截断',
  'chat.notice': '提示：',
  'chat.loading': '正在分析…',
  'chat.empty': '暂无对话，输入问题开始分析',
  'chat.readyHint': 'AI 结论基于授权数据源；请核对查询行数与业务口径。',
  'chat.sourceLoading': '正在加载数据源…',
  'chat.sourceUnavailable': '数据源未配置或未启用，无法开始分析',
  'chat.sourceErrorSuffix': '{message}，暂无法开始分析',
  'chat.sourceNoInfo': '后端未返回数据源信息',
  'chat.inputAria': '聊天输入',
  'chat.send': '发送',
  'chat.placeholder': '输入你的业务问题，例如：最近 30 天的消费金额是多少？',
  'chat.error.timeout': '分析请求超时，请稍后重试',
  'chat.error.network': '无法连接后端服务，请稍后重试',
  'chat.error.invalidResponse': '后端返回了无法识别的分析结果',
  'chat.error.unknown': '分析失败，请稍后重试',
  'chat.error.MODEL_UNAVAILABLE': '模型服务暂不可用，请稍后重试',
  'chat.error.MODEL_TIMEOUT': '模型响应超时，请稍后重试',
  'chat.error.MODEL_RESPONSE_INVALID': '模型返回内容不合法，请重试或调整问题',
  'chat.error.AGENT_UNAVAILABLE': '分析服务暂不可用，请稍后重试',
  'chat.error.AGENT_TIMEOUT': '分析超时，请缩小问题范围后重试',
  'chat.error.SESSION_NOT_FOUND': '会话不存在或已失效，请重新提问',
  'chat.error.CHAT_INVALID_REQUEST': '请求参数不合法，请检查问题后重试',
  'chat.error.INVALID_REQUEST': '请求参数不合法，请检查问题后重试',
  'chat.error.AMBIGUOUS_TIME_RANGE':
    '问题包含多个、被否定或与绝对日期冲突的时间表达，请明确要统计的时间范围后重试',
  'chat.error.AMBIGUOUS_METRIC.MEMBER_TRADE_LINK':
    '会员与消费的关联键尚未确认，系统无法关联会员与消费对象或计算唯一会员数；不能用消费记录行数代替人数。请补充可用的会员标识后重试。',
  'chat.error.AMBIGUOUS_METRIC.NET_REVENUE':
    '净收入涉及退款、优惠、补贴等口径，尚未确认；请明确是否扣除退款、优惠或补贴后重试。',
  'chat.error.AMBIGUOUS_METRIC.UNIQUE_ORDER':
    '授权视图没有稳定的订单唯一标识，无法计算唯一订单数；不能用数据行数代替订单数。请补充订单唯一标识后重试。',
  'chat.error.AMBIGUOUS_METRIC.GENERIC':
    '该指标口径尚未确认，请补充统计口径、时间范围或字段含义后重试',

  'answer.POLICY_BLOCKED':
    '该请求超出当前授权范围或使用了不支持的 SQL 结构，已停止执行。请调整问题或联系管理员确认授权范围。',
  'answer.SQL_RETRY_EXHAUSTED':
    '生成的 SQL 多次未通过语法校验，已停止本次分析。请补充或调整问题后重试。',
  'answer.TOOL_CALL_LIMIT': '分析步骤超出上限，已停止本次分析。请缩小问题范围后重试。',
  'answer.TOOL_ARGUMENT_INVALID': '模型返回的工具调用无法执行，已停止本次分析。',
  'answer.QUERY_TIMEOUT': '查询超时，请缩小时间范围或统计范围后重试。',
  'answer.QUERY_BUSY': '查询服务繁忙，请稍后重试。',
  'answer.QUERY_RESULT_TOO_LARGE': '查询结果过大，请缩小统计范围后重试。',
  'answer.QUERY_FAILED': '查询执行失败，请稍后重试。',
  'answer.unknown': '分析服务已停止处理本次请求。',

  'warning.UNVERIFIED': '本次回答未经过数据库查询验证，请以实际查询结果为准。',
  'warning.DATE_RANGE_UNVERIFIED': '未能从实际查询中验证目标日期区间。',
  'warning.TOOL_CALL_LIMIT': '分析步骤超出上限，已停止本次分析。',
  'warning.SQL_RETRY_EXHAUSTED': '生成的 SQL 多次未通过语法校验。',
  'warning.POLICY_BLOCKED': '执行被授权策略拦截。',
  'warning.TOOL_ARGUMENT_INVALID': '模型返回的工具调用无法执行。',
  'warning.QUERY_TIMEOUT': '查询超时。',
  'warning.QUERY_BUSY': '查询服务繁忙。',
  'warning.QUERY_RESULT_TOO_LARGE': '查询结果过大。',
  'warning.QUERY_FAILED': '查询执行失败。',
  'warning.unknown': '分析服务报告了其他问题。',

  'note.DATE_RANGE_VERIFIED':
    '（本次“最近{days}天”按应用时区 {timezone} 的自然日规则计算，统计区间为 >= \'{start}\' 且 < \'{end}\'；该规则为系统统一口径，尚未确认为正式业务口径。）',
  'note.DATE_RANGE_UNVERIFIED':
    '（本次问题命中“最近{days}天”，但服务端未能从实际查询中验证目标日期字段与半开区间，日期区间未验证；上述结论不能视为已按该时间区间统计，如需按时间口径解读，请明确时间范围后重试。）',
  'note.UNVERIFIED': '（注：本次回答未经过数据库查询验证，请以实际查询结果为准。）',

  'chart.ariaFallback': '图表',
  'chart.empty': '没有可展示的数据',
  'chart.pieNegative': '指标含负数，不适合占比展示，已改为表格',
  'chart.pieNoPositive': '没有可用于占比展示的正数值，已改为表格',
  'chart.truncated': '图表仅展示前 {shown} 行（查询返回 {source} 行）',
  'chart.queryTruncated': '查询结果已截断，图表仅展示返回的部分数据',
  'chart.invalid': '图表数据格式不合法',
  'chart.unitUnconfirmed': '单位未确认',
  'chart.unitWrapped': '（{unit}）',
  'chart.title.metric': '{y}',
  'chart.title.trend': '{y} 随 {x} 变化趋势',
  'chart.title.compare': '{y} 按 {x} 对比',
  'chart.title.share': '{y} 按 {x} 占比',
  'chart.title.table': '查询结果',
  'app.settings': '设置',

  'settings.title': '系统设置',
  'settings.tabLlm': 'LLM 设置',
  'settings.tabDatasource': '数据源设置',
  'settings.field.baseUrl': 'Base URL',
  'settings.field.apiKey': 'API 密钥',
  'settings.field.model': '模型',
  'settings.field.timeout': '超时时间（秒）',
  'settings.field.maxTokens': '最大 token 数',
  'settings.field.temperature': 'Temperature',
  'settings.field.enabled': '启用数据源',
  'settings.field.name': '数据源名称',
  'settings.field.host': '主机地址',
  'settings.field.port': '端口',
  'settings.field.database': '数据库',
  'settings.field.user': '用户名',
  'settings.field.password': '密码',
  'settings.field.ssl': '启用 SSL',
  'settings.secretSaved': '密钥已保存，留空则不修改',
  'settings.test': '测试连接',
  'settings.save': '保存',
  'settings.testing': '正在测试\u2026',
  'settings.saving': '正在保存\u2026',
  'settings.saved': '设置已保存',
  'settings.loadFailed': '加载设置失败',
  'settings.testSuccess': '连接成功（{ms} 毫秒）',
  'settings.testFailed': '连接失败：{reason}',
  'settings.request.timeout': '设置请求超时',
  'settings.request.network': '无法连接后端服务',
  'settings.request.invalid-response': '后端返回了无法识别的设置响应',
  'settings.request.http': '设置服务暂不可用',
  'settings.llmTestError.config-invalid': 'LLM 配置不完整或不合法',
  'settings.llmTestError.timeout': 'LLM 请求超时',
  'settings.llmTestError.unauthorized': 'API 密钥被拒绝（未授权）',
  'settings.llmTestError.http-error': 'LLM 服务返回 HTTP 错误',
  'settings.llmTestError.network': '无法连接到 LLM 服务',
  'settings.llmTestError.invalid-response': 'LLM 服务返回了无法识别的响应',
  'settings.llmTestError.unknown': 'LLM 连接失败',
  'settings.dsTestError.config-invalid': '数据源配置不完整或不合法',
  'settings.dsTestError.timeout': '数据源连接超时',
  'settings.dsTestError.auth': '数据库认证失败，请检查用户名和密码',
  'settings.dsTestError.tls': 'TLS/SSL 握手失败，请检查 SSL 设置',
  'settings.dsTestError.unavailable': '数据库无法连接，请检查主机和端口',
  'settings.dsTestError.unknown': '数据源连接失败',
  'settings.discoverTitle': '导入授权表（倒表）',
  'settings.loadTables': '加载数据库表',
  'settings.loadingTables': '正在加载数据库表\u2026',
  'settings.loadTablesFailed': '加载数据库表失败',
  'settings.tablesEmpty': '数据库中没有发现表',
  'settings.selectAll': '全选',
  'settings.clearAll': '清空',
  'settings.importSelected': '导入所选表',
  'settings.noTablesSelected': '请先勾选要导入的表',
  'settings.columns': '{n} 列',
  'settings.allowedSummary': '已授权对象（共 {count} 个）：',
  'settings.allowedItem': '{name}（{cols}）',
}

const messages: Record<Locale, Record<MessageKey, string>> = { en, zh }

export type MessageParams = Record<string, string | number>

/** 解析持久化偏好：非法值、缺失或读取失败一律回退英文。 */
export function resolveLocale(raw: string | null | undefined): Locale {
  return raw === 'zh' ? 'zh' : 'en'
}

/** 读取已保存偏好；存储不可用时安全回退英文，不抛错。 */
export function readStoredLocale(): Locale {
  try {
    return resolveLocale(globalThis.localStorage?.getItem(STORAGE_KEY))
  } catch {
    return 'en'
  }
}

export const locale = ref<Locale>(readStoredLocale())

function applyDocumentLocale(): void {
  if (typeof document === 'undefined') return
  document.documentElement.lang = locale.value === 'zh' ? 'zh-CN' : 'en'
  document.title = translate('app.title')
}

/** 切换界面语言：立即生效，尝试持久化；存储失败不阻断界面。 */
export function setLocale(next: Locale): void {
  locale.value = next
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, next)
  } catch {
    // 隐私模式或存储被禁用：只在本次会话生效，仍安全回退默认英文。
  }
  applyDocumentLocale()
}

/** 测试可重置内存状态；正常产品流程由用户切换驱动。 */
export function resetLocaleForTests(next: Locale = 'en'): void {
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEY)
  } catch {
    // 忽略存储不可用。
  }
  locale.value = next
  applyDocumentLocale()
}

/** 应用启动时同步 document.lang 与标题；重复调用幂等。 */
export function initI18n(): void {
  applyDocumentLocale()
}

/** 插值仅替换已知参数占位符；输出始终作为纯文本由 Vue 转义。 */
export function translate(key: MessageKey, params: MessageParams = {}): string {
  const template = messages[locale.value][key] ?? messages.en[key] ?? key
  return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name: string) => {
    const value = params[name]
    return value === undefined ? match : String(value)
  })
}

export interface UseI18n {
  locale: typeof locale
  t: typeof translate
  setLocale: typeof setLocale
}

export function useI18n(): UseI18n {
  return { locale, t: translate, setLocale }
}

/** 服务端固定回答 code 的本地化；未知 code 不展示原始内部标识。 */
export function answerText(code: string): string {
  const key = `answer.${code}` as MessageKey
  return key in en ? translate(key) : translate('answer.unknown')
}

/** 健康检查失败的本地化文案。 */
export function serviceErrorText(kind: string): string {
  const key = `service.${kind === 'invalid-response' ? 'invalidResponse' : kind}` as MessageKey
  return key in en ? translate(key) : translate('service.unknown')
}

/** 数据源请求失败的本地化文案。 */
export function datasourceErrorText(kind: string): string {
  const key = `datasource.${kind === 'invalid-response' ? 'invalidResponse' : kind}` as MessageKey
  return key in en ? translate(key) : translate('datasource.unknown')
}

/** 设置请求失败的本地化文案。 */
export function settingsErrorText(kind: string): string {
  const key = `settings.request.${kind}` as MessageKey
  return key in en ? translate(key) : translate('settings.request.http')
}

/** LLM 连接测试失败按稳定 error code 本地化；未知 code 用通用提示，不回显后端原文。 */
export function llmTestErrorText(code: string | null | undefined): string {
  const key = `settings.llmTestError.${code ?? 'unknown'}` as MessageKey
  return key in en ? translate(key) : translate('settings.llmTestError.unknown')
}

/** 数据源连接测试失败按稳定 error code 本地化；未知 code 用通用提示，不回显后端原文。 */
export function dsTestErrorText(code: string | null | undefined): string {
  const key = `settings.dsTestError.${code ?? 'unknown'}` as MessageKey
  return key in en ? translate(key) : translate('settings.dsTestError.unknown')
}

/**
 * Schema 固定告警只按稳定 code 本地化。
 * - 未知 code（含中文或内部调试串）不回显服务端原文；
 * - 旧后端没有 warning_codes 时同样使用通用提示：自由文本不是稳定的展示契约，
 *   也不能把任意后端字符串当作可安全展示的数据；
 * - fallback 仅为旧调用签名保留，不参与渲染。
 */
export function schemaWarningText(
  code: string | undefined,
  _fallback: string,
): string {
  if (!code) return translate('schemaWarning.unknown')
  const key = `schemaWarning.${code}` as MessageKey
  return Object.hasOwn(en, key) ? translate(key) : translate('schemaWarning.unknown')
}

/**
 * 聊天错误的本地化：只使用稳定 kind/code/params，绝不拼接后端原始 message。
 * 口径澄清按 reason 细分；未知 reason 用通用口径提示，不展示内部 code。
 */
export function chatErrorText(
  kind: string,
  code?: string,
  params: Record<string, string> = {},
): string {
  if (kind === 'timeout') return translate('chat.error.timeout')
  if (kind === 'network') return translate('chat.error.network')
  if (kind === 'invalid-response') return translate('chat.error.invalidResponse')
  if (code) {
    if (code === 'AMBIGUOUS_METRIC') {
      const reason = params.reason
      const key = `chat.error.AMBIGUOUS_METRIC.${reason ?? 'GENERIC'}` as MessageKey
      return translate(key in en ? key : 'chat.error.AMBIGUOUS_METRIC.GENERIC')
    }
    const key = `chat.error.${code}` as MessageKey
    if (key in en) return translate(key)
  }
  return translate('chat.error.unknown')
}

/**
 * 服务端聊天告警 code 的本地化。
 * 当前后端只下发稳定 code；未知 code 或旧后端自由文本一律使用通用提示，
 * 避免把内部标识、调试串或任意后端文本展示给用户。
 */
export function warningText(codeOrText: string): string {
  const key = `warning.${codeOrText}` as MessageKey
  return Object.hasOwn(en, key) ? translate(key) : translate('warning.unknown')
}

/** 服务端确定性注记的本地化；参数只允许字符串，按文本插值渲染。 */
export function noteText(note: { code: string; params?: Record<string, string> }): string {
  const key = `note.${note.code}` as MessageKey
  if (!(key in en)) return ''
  return translate(key, note.params ?? {})
}

/** 图表确定性标题 code 的本地化；没有 code 时保留模型自由标题原文。 */
export function chartTitleText(spec: {
  title: string
  title_code?: string | null
  title_params?: Record<string, string>
}): string {
  if (!spec.title_code) return spec.title
  const key = `chart.title.${spec.title_code}` as MessageKey
  if (!(key in en)) return spec.title
  return translate(key, spec.title_params ?? {})
}

const UNCONFIRMED_UNIT_MARKERS = new Set([
  '单位未确认',
  '未确认',
  'unknown',
  'unconfirmed',
  'n/a',
  'na',
  '-',
])

/** 只转换已知“单位未确认”标记；真实单位/币种保持原文。 */
export function unitText(unit: string | null | undefined): string {
  if (!unit) return ''
  return UNCONFIRMED_UNIT_MARKERS.has(unit.trim().toLowerCase())
    ? translate('chart.unitUnconfirmed')
    : unit
}

/** 本地化浏览器时间展示；语言切换后调用方重新渲染即可更新。 */
export function formatLocaleTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(locale.value === 'zh' ? 'zh-CN' : 'en-US')
}
