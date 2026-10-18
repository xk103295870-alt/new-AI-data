<script setup lang="ts">
// ChartRenderer：ChartSpec 的唯一渲染入口。
// 安全边界：
// - spec 在渲染前过运行时守卫，非法结构只显示提示，不进入 ECharts；
// - line/bar/pie 的 Option 只能由 charts/options.ts 的纯函数生成，组件不接受外部 Option；
// - table/metric 用模板文本渲染，不使用 v-html。
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { buildChartOption, pieIssue } from '@/charts/options'
import { chartTitleText, translate, unitText } from '@/i18n'
import { isChartSpec } from '@/types/chart'
import type { ChartScalar, ChartSpec } from '@/types/chart'

// 按需注册，减小打包体积；只注册受控图表类型。
use([
  CanvasRenderer,
  LineChart,
  BarChart,
  PieChart,
  GridComponent,
  TitleComponent,
  TooltipComponent,
])

const props = withDefaults(
  defineProps<{
    /** 后端下发、尚未信任的图表结构。 */
    spec: unknown
    /** 查询本身是否被截断；与图表独立裁剪是两回事。 */
    queryTruncated?: boolean
  }>(),
  { queryTruncated: false },
)

const parsed = computed<ChartSpec | null>(() => (isChartSpec(props.spec) ? props.spec : null))
/** 确定性标题按界面语言渲染；模型自由标题没有 code，保持原文。 */
const chartTitle = computed(() => (parsed.value ? chartTitleText(parsed.value) : ''))
const displayUnit = computed(() => unitText(parsed.value?.unit))
// 单位与标题一样是展示派生值：只把本地化后的单位传给受控转换器，
// 不修改原 ChartSpec，切语言时 Option 随 computed 重新生成。
const option = computed(() =>
  parsed.value
    ? buildChartOption(parsed.value, chartTitle.value, displayUnit.value)
    : null,
)
const isEmpty = computed(() => (parsed.value?.dataset.length ?? 0) === 0)
const isTable = computed(() => parsed.value?.type === 'table')
const isMetric = computed(() => parsed.value?.type === 'metric')

/** 饼图数据不适合占比时，保留原值改用表格，而不是画一张误导图形。 */
const pieFallback = computed(
  () => parsed.value?.type === 'pie' && pieIssue(parsed.value) !== null,
)
const pieFallbackNote = computed(() => {
  if (!pieFallback.value || !parsed.value) return ''
  return pieIssue(parsed.value) === 'negative'
    ? translate('chart.pieNegative')
    : translate('chart.pieNoPositive')
})
const showTable = computed(() => isTable.value || pieFallback.value)

/** 表头取所有行字段的并集，保持首次出现顺序，避免列丢失。 */
const columns = computed<string[]>(() => {
  const seen: string[] = []
  for (const row of parsed.value?.dataset ?? []) {
    for (const key of Object.keys(row)) {
      if (!seen.includes(key)) seen.push(key)
    }
  }
  return seen
})

const metricValue = computed<ChartScalar | undefined>(() => {
  if (!parsed.value || parsed.value.type !== 'metric') return undefined
  return parsed.value.dataset[0]?.[parsed.value.encoding.y]
})

/** 空值统一显示占位符，不把 null 伪造成 0。 */
function formatCell(value: ChartScalar | undefined): string {
  if (value === null || value === undefined) return '—'
  return String(value)
}
</script>

<template>
  <div
    class="chart-renderer"
    role="figure"
    :aria-label="parsed ? chartTitle : translate('chart.ariaFallback')"
  >
    <template v-if="parsed">
      <h4 class="chart-title">
        {{ chartTitle }}
        <span v-if="displayUnit" class="chart-unit">
          {{ translate('chart.unitWrapped', { unit: displayUnit }) }}
        </span>
      </h4>

      <p v-if="isEmpty" class="chart-empty">{{ translate('chart.empty') }}</p>

      <template v-else>
        <p v-if="pieFallbackNote" class="chart-fallback" role="note">
          {{ pieFallbackNote }}
        </p>

        <div v-if="showTable" class="chart-table-wrap">
          <table>
            <thead>
              <tr>
                <th v-for="column in columns" :key="column">{{ column }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in parsed.dataset" :key="index">
                <td v-for="column in columns" :key="column">{{ formatCell(row[column]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-else-if="isMetric" class="chart-metric">
          <span class="metric-value">{{ formatCell(metricValue) }}</span>
          <span v-if="displayUnit" class="metric-unit">{{ displayUnit }}</span>
          <span class="metric-label">{{ parsed.encoding.y }}</span>
        </div>

        <VChart v-else-if="option" class="chart-canvas" :option="option" autoresize />
      </template>

      <!-- 图表裁剪与查询截断分别提示，不把“查询未截断”当成“图表完整”。 -->
      <p v-if="parsed.truncated" class="chart-truncated" role="note">
        {{
          translate('chart.truncated', {
            shown: parsed.dataset.length,
            source: parsed.source_row_count,
          })
        }}
      </p>
      <p v-if="queryTruncated" class="chart-truncated" role="note">
        {{ translate('chart.queryTruncated') }}
      </p>
    </template>

    <p v-else class="chart-invalid" role="note">{{ translate('chart.invalid') }}</p>
  </div>
</template>

<style scoped>
.chart-renderer {
  margin-top: 12px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}
.chart-title {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}
.chart-unit {
  font-weight: 400;
  color: var(--el-text-color-secondary);
}
.chart-canvas {
  width: 100%;
  height: 280px;
}
.chart-empty,
.chart-invalid {
  margin: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.chart-table-wrap {
  max-height: 280px;
  overflow: auto;
}
.chart-table-wrap table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.chart-table-wrap th,
.chart-table-wrap td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  text-align: left;
  white-space: nowrap;
}
.chart-metric {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.metric-value {
  font-size: 28px;
  font-weight: 600;
}
.metric-unit {
  font-size: 14px;
  color: var(--el-text-color-regular);
}
.metric-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.chart-truncated,
.chart-fallback {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--el-color-warning);
}
</style>
