"""图表语义层：ChartSpec 契约、确定性推导与受控数据来源。

边界：
- ChartSpec 是「标题 + 数据 + 维度/指标映射」的语义结构，不是 ECharts Option；
- dataset 只能由服务端从真实 QueryResult 构造，模型建议通道不接收数据行；
- 图表的类型/标题/单位可由模型建议，但必须经过本模块校验，非法即回退。
"""
from app.chart.formatter import ChartSpecFormatter
from app.chart.models import (
    MAX_CHART_ROWS,
    ChartSpec,
    ChartSuggestion,
    ChartType,
)

__all__ = [
    "MAX_CHART_ROWS",
    "ChartSpec",
    "ChartSpecFormatter",
    "ChartSuggestion",
    "ChartType",
]
