// ChartSpec → ECharts Option 的受控转换器。
// 设计原因：模型只提供语义（类型/标题/单位/字段名），Option 完全由本地纯函数生成，
// 因此不存在执行模型代码、注入自定义 formatter 或 HTML 的通道。
import type { EChartsOption } from 'echarts'
import type { ChartScalar, ChartSpec } from '@/types/chart'

type EncodedSpec = ChartSpec & { encoding: { x: string; y: string } }

function isSafeNumber(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isFinite(value) &&
    (!Number.isInteger(value) || Number.isSafeInteger(value))
  )
}

function measure(value: ChartScalar | undefined): number | null {
  return isSafeNumber(value) ? value : null
}

export type PieIssue = 'negative' | 'no-positive'

/**
 * 饼图适用域：非空指标不得为负，且至少一个为正。
 * null 表示缺失，可以跳过但不能单独构成占比；0 总和没有占比语义。
 * 不适用时由组件回退表格，禁止取绝对值或把 null/0 当成占比来源。
 */
export function pieIssue(spec: ChartSpec): PieIssue | null {
  if (spec.type !== 'pie') return null
  const y = spec.encoding.y
  if (!y) return 'no-positive'

  let hasPositive = false
  for (const row of spec.dataset) {
    const value = row[y]
    if (value === null || value === undefined) continue
    if (!isSafeNumber(value)) return 'no-positive'
    if (value < 0) return 'negative'
    if (value > 0) hasPositive = true
  }
  return hasPositive ? null : 'no-positive'
}

function dimension(value: ChartScalar | undefined): string {
  return value === null || value === undefined ? '' : String(value)
}

/**
 * `unit` 是展示层派生值（当前界面语言），默认仍回退 spec.unit。
 * 只影响 Option 的轴标题，不修改原 ChartSpec、encoding 或 dataset。
 */
function axisOption(
  spec: EncodedSpec,
  series: Record<string, unknown>,
  title = spec.title,
  unit: string | null | undefined = spec.unit,
): EChartsOption {
  return {
    title: { text: title },
    tooltip: { trigger: 'axis' },
    // name 只用于轴标题，不拼接进任何可执行结构。
    yAxis: { type: 'value', name: unit ?? '' },
    xAxis: {
      type: 'category',
      data: spec.dataset.map((row) => dimension(row[spec.encoding.x])),
    },
    series: [series],
  }
}

export function toLineOption(
  spec: EncodedSpec,
  title = spec.title,
  unit: string | null | undefined = spec.unit,
): EChartsOption {
  return axisOption(
    spec,
    {
      name: spec.encoding.y,
      type: 'line',
      // null 保持为空缺，不插值，避免视觉上编造数据点。
      data: spec.dataset.map((row) => measure(row[spec.encoding.y])),
      connectNulls: false,
    },
    title,
    unit,
  )
}

export function toBarOption(
  spec: EncodedSpec,
  title = spec.title,
  unit: string | null | undefined = spec.unit,
): EChartsOption {
  return axisOption(
    spec,
    {
      name: spec.encoding.y,
      type: 'bar',
      data: spec.dataset.map((row) => measure(row[spec.encoding.y])),
    },
    title,
    unit,
  )
}

export function toPieOption(spec: EncodedSpec, title = spec.title): EChartsOption {
  return {
    title: { text: title },
    tooltip: { trigger: 'item' },
    series: [
      {
        name: spec.encoding.y,
        type: 'pie',
        // 饼图不展示 null 扇区；缺失值不会以 0 参与占比。
        data: spec.dataset.flatMap((row) => {
          const value = measure(row[spec.encoding.y])
          if (value === null) return []
          return [{ name: dimension(row[spec.encoding.x]), value }]
        }),
      },
    ],
  }
}

/**
 * table/metric 不是 ECharts 图表；返回 null 由组件用 DOM 渲染。
 * `unit` 传入时只用于轴标题的本地化展示，不改变图表数据语义。
 */
export function buildChartOption(
  spec: ChartSpec,
  title?: string,
  unit?: string | null,
): EChartsOption | null {
  switch (spec.type) {
    case 'line':
    case 'bar':
    case 'pie': {
      const x = spec.encoding.x
      const y = spec.encoding.y
      if (!x || !y) return null
      const encoded = spec as EncodedSpec
      if (spec.type === 'line') return toLineOption(encoded, title, unit)
      if (spec.type === 'bar') return toBarOption(encoded, title, unit)
      // 不适合占比语义时绝不生成饼图 Option，由组件降级为表格。
      if (pieIssue(encoded) !== null) return null
      return toPieOption(encoded, title)
    }
    default:
      return null
  }
}
