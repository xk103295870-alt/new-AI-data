"""业务术语与系统提示：把已确认/未确认口径固化进 Agent 提示。

职责：
- 从 `config/business_terms.yaml` 读取开发人员维护的业务术语；
- 提供确定性的“未确认指标”拦截（不含模型判断，避免模型自行解释口径）；
- 生成系统提示，包含授权对象概览与防虚构硬约束。

安全边界：
- 提示只包含授权对象名/类型/注释，不预置列名；列必须经 get_schema 工具获取；
- 未确认术语不得作为事实输出；消息命中未确认术语时由 AgentRunner 直接要求澄清。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml

from app.agent.time_rules import (
    DEFAULT_APP_TIMEZONE,
    RelativeDateRule,
    build_relative_rule_prompt,
    local_time,
    recent_days_rule,
    resolve_timezone,
)
from app.query.policy import ALLOWED_FUNCTIONS
from app.schema.models import SchemaSummary

logger = logging.getLogger(__name__)


class BusinessTermsError(ValueError):
    """术语文件结构非法；消息固定，不回显文件内容或原始堆栈。"""


@dataclass(frozen=True)
class BusinessTerms:
    """已确认口径与未确认术语的只读集合。"""

    confirmed: dict[str, str]
    unconfirmed: dict[str, str]

    def find_unconfirmed(self, text: str) -> list[str]:
        """返回消息中出现的未确认术语；按配置顺序稳定输出。"""
        return [name for name in self.unconfirmed if name and name in text]


def default_business_terms() -> BusinessTerms:
    """内置默认术语；与 `config/business_terms.yaml` 的初始内容保持一致。

    文件缺失时使用该默认值，保证防虚构约束不会因工作目录不同而消失。
    """
    return BusinessTerms(
        confirmed={
            "消费金额": (
                "按授权消费视图的金额字段直接求和；不等于扣除退款后的净收入；"
                "货币单位未确认"
            ),
            "交易记录数": "授权消费视图的数据行数；不等于唯一订单数",
            "会员注册记录数": "授权会员视图的数据行数；不等于唯一会员数",
            "门店维度统计": "按授权消费视图中的门店字段分组统计",
        },
        unconfirmed={
            "净收入": "退款、优惠、补贴等口径尚未确认，禁止作为事实输出",
            "唯一会员数": "会员与消费的关联键尚未确认，无法从消费视图推导唯一会员数",
            "唯一订单数": "授权视图中没有稳定的订单唯一标识，禁止把数据行数当作唯一订单数",
            "会员消费关联": "会员与消费的关联键尚未确认，禁止对会员与消费对象做 JOIN",
        },
    )


def _parse_mapping(value: Any, label: str) -> dict[str, str]:
    """严格解析“术语名 → 定义”映射；任何非字符串结构都拒绝。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BusinessTermsError(f"{label} 必须是映射")
    parsed: dict[str, str] = {}
    for name, definition in value.items():
        if not isinstance(name, str) or not name.strip():
            raise BusinessTermsError(f"{label} 的术语名必须是非空字符串")
        if isinstance(definition, dict):
            definition = definition.get("definition")
        if not isinstance(definition, str) or not definition.strip():
            raise BusinessTermsError(f"{label} 的定义必须是非空字符串")
        parsed[name.strip()] = definition.strip()
    return parsed


def clarification_reason(terms: Sequence[str]) -> str:
    """返回稳定的澄清原因枚举，供前端按界面语言映射文案。

    只返回固定枚举，不拼接术语原文或用户输入；未知组合回退 GENERIC。
    """
    names = set(terms)
    if names & {"会员消费关联", "唯一会员数"}:
        return "MEMBER_TRADE_LINK"
    if "净收入" in names:
        return "NET_REVENUE"
    if "唯一订单数" in names:
        return "UNIQUE_ORDER"
    return "GENERIC"


def clarification_message(terms: Sequence[str]) -> str:
    """按命中的未确认术语返回稳定、脱敏的针对性澄清文案。

    - 不拼接用户原句、SQL、堆栈或任何数字；
    - 对缺少关联键、订单唯一标识等边界给出具体原因，避免所有题共用
      一句泛泛的“口径未确认”；
    - 该文案由服务端确定性生成，不调用模型或 SQL 代为解释。
    """
    names = set(terms)
    if names & {"会员消费关联", "唯一会员数"}:
        return (
            "会员与消费的关联键尚未确认，系统无法关联会员与消费对象或计算"
            "唯一会员数；不能用消费记录行数代替人数。请补充可用的会员标识后重试。"
        )
    if "净收入" in names:
        return (
            "净收入涉及退款、优惠、补贴等口径，尚未确认；"
            "请明确是否扣除退款、优惠或补贴后重试。"
        )
    if "唯一订单数" in names:
        return (
            "授权视图没有稳定的订单唯一标识，无法计算唯一订单数；"
            "不能用数据行数代替订单数。请补充订单唯一标识后重试。"
        )
    return "该指标口径尚未确认，请补充统计口径、时间范围或字段含义后重试"


def load_business_terms(path: str | Path | None) -> BusinessTerms:
    """读取术语文件。

    - 路径为空或文件不存在：返回内置默认值（记录告警，不中断启动）；
    - 文件存在但结构非法：抛 BusinessTermsError（调用方降级为默认值，
      不把半解析结果当作生效配置）。
    """
    if not path or not str(path).strip():
        return default_business_terms()
    file_path = Path(path)
    if not file_path.exists():
        logger.warning("business terms file missing; using built-in defaults")
        return default_business_terms()
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise BusinessTermsError("业务术语文件无法解析") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BusinessTermsError("业务术语文件必须是映射")
    confirmed = _parse_mapping(raw.get("confirmed"), "confirmed")
    unconfirmed = _parse_mapping(raw.get("unconfirmed"), "unconfirmed")
    return BusinessTerms(confirmed=confirmed, unconfirmed=unconfirmed)


def build_system_prompt(
    terms: BusinessTerms,
    schema: SchemaSummary | Sequence[Any],
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_APP_TIMEZONE,
    relative_date_rule: RelativeDateRule | None = None,
) -> str:
    """生成 Agent 系统提示。

    对象只给名称、类型与注释；字段与主外键不在提示中下发，
    必须由模型调用 get_schema 工具获取，保持数据最小化。

    now 由 AgentRunner 的可注入服务端时钟提供；缺省取当前 UTC 时刻再转换到
    应用时区。提示中的日期、示例区间和本次问题区间全部由服务端计算，
    不依赖模型自身日期知识。

    schema 可以是 SchemaSummary，也可以是 ToolRegistry.schema_overview()
    返回的对象概览序列（两者都只暴露 name/kind/comment）。
    """
    objects_source = getattr(schema, "objects", schema)
    objects = "\n".join(
        f"- {obj.name}（{'视图' if obj.kind == 'view' else '表'}）：{obj.comment or '无说明'}"
        for obj in objects_source
    )
    confirmed = "\n".join(f"- {name}：{definition}" for name, definition in terms.confirmed.items())
    unconfirmed = "\n".join(
        f"- {name}：{definition}" for name, definition in terms.unconfirmed.items()
    )

    zone = resolve_timezone(timezone_name)
    current = local_time(now or datetime.now(timezone.utc), zone.key)
    example = recent_days_rule(30, current, zone.key)
    supported_functions = "、".join(sorted(name.upper() for name in ALLOWED_FUNCTIONS))
    specific_rule = (
        f"\n{build_relative_rule_prompt(relative_date_rule)}"
        if relative_date_rule is not None
        else ""
    )

    return f"""你是“AI 商业数据分析”助手，只能通过服务端提供的工具查询已授权的数据源。

工作规则（必须遵守）：
1. 先调用 get_schema 获取授权对象与字段，再调用 execute_query 执行单条只读 SELECT。
2. 只能查询下方列出的授权对象；禁止 JOIN、子查询、UNION、WITH、窗口函数、
   写操作、DDL、注释和任何绕过工具的直接查询。
3. 会员与消费的关联键尚未确认：禁止对会员对象与消费对象做 JOIN。
4. 数据行数不等于唯一会员数或唯一订单数；消费金额求和不等于净收入。
   金额字段的货币单位未确认：不得擅自标注“元/人民币/CNY/RMB/¥/￥”等，
   可写“消费金额（单位未确认）”或省略单位，不得替用户认定币种。
5. 口径不明确时必须先要求用户澄清，不得虚构 SQL、数字或结论；
   不得把未确认指标写成事实。
6. 查询结果为空时必须明确回答“没有查询到符合条件的数据”，不得编造趋势。
7. 只根据工具真实返回的结果作答；不要请求、猜测或输出凭据、连接信息
   和未授权对象。
8. 如需图表，可在成功 execute_query 后调用 suggest_chart 建议类型/标题/
   单位/字段映射；建议中不得提供数据行，图表数据由服务端从真实结果生成。
9. 涉及相对日期时必须使用下方服务端当前时间与统一规则，先把相对日期换算为
   绝对日期字面量再生成 SQL；禁止使用模型自身日期知识或写死日期。
10. 排名/前N类问题必须写明并列时的确定性次序，并在 SQL 中加入次级排序
    （例如记录数降序后按门店编号升序），不能依赖当前数据恰好无并列；
    标量问题只返回一个聚合总额，不要附加分组列或其他明细列，图表建议应
    与题目要求的展示类型一致。
11. 回答语言必须跟随当前用户问题（最后一条用户消息）的主要语言，而不是历史
    对话语言、业务术语语言或界面语言：当前问题为英文则用英文回答，为中文则
    用中文回答。同一会话中用户切换语言后，立即按新问题的语言回答，不得沿用
    上一轮语言；不得用界面按钮或系统提示强制模型使用固定语言。若当前问题混合
    中英文或无法判断，使用当前问题中占主导的语言；仍无法判断时默认中文。

执行失败时如实说明失败原因，不要伪造成功结果。

当前时间（服务端时钟，应用时区 {zone.key}）：{current.strftime('%Y-%m-%d %H:%M:%S')}

相对日期统一规则（系统规则，非已确认业务口径）：
- “最近N天/近N天”指包含今天在内的 N 个自然日；起点 = 今天-(N-1) 天，
  终点 = 明天 00:00（不含）。
- 必须使用半开区间：字段 >= '起点' AND 字段 < '终点'；不得丢掉上界，
  以免把未来数据或区间外数据混入。
- 不得使用模型自己的日期推算，也不得把日期写死在提示或 SQL 中。
- 示例：最近30天 = {example.sql_range}。
- 回答中必须说明本次实际使用的半开日期区间。
{specific_rule}
- 当前仅支持以下 SQL 函数：{supported_functions}。
  禁止使用 DATE_SUB、CURDATE、NOW、INTERVAL 等未列出的函数或表达式；
  相对日期请直接写成绝对日期字面量。
- 函数参数与组合限制（不只限制函数名）：
  * COUNT/SUM/AVG/MIN/MAX 的参数必须是普通列或 COUNT(*)；
  * ROUND 的首个参数必须是普通列，不允许 ROUND(SUM(...))、ROUND(AVG(...))
    等聚合嵌套；需要分组金额时直接写 SUM(trade_amount)，不要为展示精度
    额外包 ROUND；
  * 分组必须是普通 SELECT + GROUP BY：先 get_schema 取得分组列，
    示例为 SELECT 分组列, SUM(trade_amount) AS total_amount
    FROM trade_view GROUP BY 分组列 ORDER BY 分组列 ASC；
  * GROUP BY 保留 NULL 分组，不得用 COALESCE 把 NULL 合并到其他枚举；
  * 不得使用方言函数、窗口函数或任何未列出的组合。

已确认口径：
{confirmed or '- 暂无'}

未确认术语（不得作为事实输出，需先澄清）：
{unconfirmed or '- 暂无'}

授权对象概览（字段请调用 get_schema 获取）：
{objects}
"""
