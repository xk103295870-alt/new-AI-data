"""服务端时钟、应用时区与相对日期规则。

职责：
- 把可注入的服务端时钟转换为应用时区本地时间；
- 只识别固定语法“最近N天/近N天”，换算为绝对半开日期区间；
- 为系统提示和最终回答提供确定性的日期上下文。

设计原因：
- 模型不能依赖自身日期知识，也不应生成 DATE_SUB/CURDATE 等未授权函数；
- 相对日期在生成 SQL 前由服务端换算，模型只需使用现有 >= / < 语法；
- “最近N天”包含今天、按应用时区自然日计算，是系统统一规则，
  尚未被业务确认为正式口径，因此回答必须说明实际使用的区间。

安全边界：
- 只做文本匹配与日期计算，不执行 SQL、不访问数据库、不调用模型；
- 朴素 datetime 直接拒绝，避免隐式本地时区造成跨日错误；
- 无法识别的相对日期不改写用户问题，交由模型澄清或走既有策略拦截。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_APP_TIMEZONE = "Asia/Shanghai"
_MAX_RELATIVE_DAYS = 3660

# 仅匹配明确的天数表达；“最近一个月”“上季度”等未确认口径不做猜测。
_RECENT_DAYS_PATTERN = re.compile(r"(?:最近|近)\s*(\d{1,4})\s*天")
# 绝对日期与相对日期同时出现时，优先级未被业务确认，不能替用户选择其一。
_ABSOLUTE_DATE_PATTERN = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
)
# 否定词只检查紧邻前缀/后缀，避免把“不要用旧口径，统计最近30天”误判为纯否定。
_NEGATION_PREFIXES = (
    "不要",
    "不用",
    "不看",
    "不计",
    "不算",
    "不包括",
    "不包含",
    "不含",
    "排除",
    "除了",
    "除去",
    "去掉",
    "而非",
    "不是",
    "无需",
    "别用",
    "不统计",
)
_NEGATION_SUFFIXES = ("除外", "以外", "之外", "之前", "以前")
# 截点与相对区间同时出现时，“截至”可能把区间改成另一种口径，必须澄清。
_CUTOFF_MARKERS = ("截至", "截止")
# 与“最近N天”并列的另一种时间语法：仅当用户明确写出这些词时才澄清，
# 不做通用时间 NLP，也不猜测业务优先级。
_COMPETING_TIME_PATTERN = re.compile(
    r"(?:上个月|上月|上季度|本季度|本月|本周|去年|前年|昨天|前天)"
)


class TimeRuleError(ValueError):
    """时区或时钟值非法；消息固定，不回显原始配置。"""


def resolve_timezone(timezone_name: str) -> ZoneInfo:
    """解析 IANA 时区名；空值或未知时区返回固定错误，不静默退回 UTC。"""
    candidate = (timezone_name or "").strip()
    if not candidate:
        raise TimeRuleError("应用时区不能为空")
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        raise TimeRuleError("应用时区非法") from None


def local_time(now: datetime, timezone_name: str) -> datetime:
    """把带时区的服务端时钟转换为应用时区时间。

    拒绝 naive datetime：否则不同机器会按各自本地时区解释同一测试输入，
    相对日期的跨日边界将不可复现。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise TimeRuleError("服务端时钟必须是带时区的时间")
    return now.astimezone(resolve_timezone(timezone_name))


@dataclass(frozen=True)
class RelativeDateRule:
    """服务端换算后的绝对半开区间 [start, end_exclusive)。"""

    days: int
    start: date
    end_exclusive: date
    timezone_name: str

    @property
    def start_text(self) -> str:
        return self.start.isoformat()

    @property
    def end_text(self) -> str:
        return self.end_exclusive.isoformat()

    @property
    def sql_range(self) -> str:
        return f">= '{self.start_text}' AND < '{self.end_text}'"


def recent_days_rule(days: int, now: datetime, timezone_name: str) -> RelativeDateRule:
    """按统一自然日规则换算最近 N 天。

    规则：包含今天在内的 N 个自然日；start = 今天-(N-1)，end = 明天。
    例如 2026-01-01 的最近 30 天为 [2025-12-03, 2026-01-02)。
    """
    if not isinstance(days, int) or not 1 <= days <= _MAX_RELATIVE_DAYS:
        raise TimeRuleError("相对日期天数超出支持范围")
    zone = resolve_timezone(timezone_name)
    today = local_time(now, zone.key).date()
    return RelativeDateRule(
        days=days,
        start=today - timedelta(days=days - 1),
        end_exclusive=today + timedelta(days=1),
        timezone_name=zone.key,
    )


def detect_recent_days(
    message: str, now: datetime, timezone_name: str
) -> RelativeDateRule | None:
    """识别唯一、未被否定的“最近N天/近N天”；否则返回 None。

    保留该函数作为兼容入口；需要区分“没有表达”与“表达冲突”时使用
    detect_relative_date。
    """
    return detect_relative_date(message, now, timezone_name).rule


@dataclass(frozen=True)
class RelativeDateDetection:
    """相对时间表达检测结果。

    ambiguous=True 表示出现了多个、被否定或与绝对日期冲突的表达。
    此时 rule 必须为 None：服务端不能从冲突表达中强行挑选第一个匹配。
    """

    rule: RelativeDateRule | None
    ambiguous: bool = False


def detect_relative_date(
    message: str, now: datetime, timezone_name: str
) -> RelativeDateDetection:
    """检测相对日期表达；冲突时返回 ambiguous，不生成任何区间。"""
    if not isinstance(message, str):
        return RelativeDateDetection(rule=None)
    matches = list(_RECENT_DAYS_PATTERN.finditer(message))
    if not matches:
        return RelativeDateDetection(rule=None)
    if len(matches) > 1:
        return RelativeDateDetection(rule=None, ambiguous=True)

    match = matches[0]
    prefix = message[max(0, match.start() - 8) : match.start()]
    suffix = message[match.end() : match.end() + 4]
    if any(word in prefix for word in _NEGATION_PREFIXES):
        return RelativeDateDetection(rule=None, ambiguous=True)
    if any(word in suffix for word in _NEGATION_SUFFIXES):
        return RelativeDateDetection(rule=None, ambiguous=True)
    # 绝对日期与相对日期同时出现：谁优先未经业务确认，必须澄清。
    if _ABSOLUTE_DATE_PATTERN.search(message):
        return RelativeDateDetection(rule=None, ambiguous=True)
    # 截点表达（如“截至某日”）与“最近N天”不是同一种口径。
    if any(marker in message for marker in _CUTOFF_MARKERS):
        return RelativeDateDetection(rule=None, ambiguous=True)
    # 跨语法比较（如“最近7天和上个月”）不能只取第一个相对区间。
    if _COMPETING_TIME_PATTERN.search(message):
        return RelativeDateDetection(rule=None, ambiguous=True)

    days = int(match.group(1))
    if not 1 <= days <= _MAX_RELATIVE_DAYS:
        return RelativeDateDetection(rule=None)
    return RelativeDateDetection(
        rule=recent_days_rule(days, now, timezone_name)
    )


def build_relative_rule_prompt(rule: RelativeDateRule) -> str:
    """针对本次问题给出服务端已经算好的绝对边界。"""
    return (
        f"- 本次问题命中“最近{rule.days}天”：服务端已按上述自然日规则换算为 "
        f"{rule.sql_range}（应用时区 {rule.timezone_name}）。生成 SQL 时必须"
        "直接使用这两个绝对日期边界，不得再调用 DATE_SUB/CURDATE 或改用自己的日期推算。"
    )


def build_relative_answer_note(rule: RelativeDateRule) -> str:
    """最终回答必须显式说明实际区间；由服务端确定性追加。

    调用方必须先确认每次用于结论的查询都真实带有该半开区间；否则改用
    build_relative_unverified_note，不能把推算区间冒充已验证统计范围。
    """
    return (
        f"（本次“最近{rule.days}天”按应用时区 {rule.timezone_name} 的自然日规则计算，"
        f"统计区间为 >= '{rule.start_text}' 且 < '{rule.end_text}'；"
        "该规则为系统统一口径，尚未确认为正式业务口径。）"
    )


def build_relative_unverified_note(rule: RelativeDateRule) -> str:
    """未从实际 SQL 证明日期边界时使用：不出现任何具体起止日期。

    只说明“服务端建议的区间未被查询验证”，避免用确定性文案为数字背书。
    """
    return (
        f"（本次问题命中“最近{rule.days}天”，但服务端未能从实际查询中验证"
        "目标日期字段与半开区间，日期区间未验证；上述结论不能视为已按该时间区间"
        "统计，如需按时间口径解读，请明确时间范围后重试。）"
    )
