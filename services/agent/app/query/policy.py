"""QueryPolicy：MySQL 方言下的受限 SELECT 解析、白名单校验与安全 SQL 生成。

安全边界（默认拒绝）：
- 只接受单条单表 SELECT；对象、列、函数、别名在全部位置逐一校验，未知结构拒绝。
- 词法层拒绝注释/优化器 hint/占位符/会话变量；AST 层使用正白名单节点集合，
  因此 JOIN、子查询、CTE、UNION、窗口、CASE/CAST、LIKE/REGEXP、INTO、锁定读、
  索引 hint、ROLLUP、OFFSET 等均不可通过。
- 函数名做 token 级白名单，防止 SQLGlot 把 IFNULL/TIME_FORMAT/TRUNCATE 等
  归一化成允许节点而绕过；同时校验函数参数个数与常量边界。
- 只执行由本模块基于已校验 AST 生成并在生成后二次解析验证的 SQL，绝不执行原文。
- 所有拒绝文案固定，不拼接 SQL 原文、常量、列名或驱动信息。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from sqlglot import exp, parse, tokenize
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

from app.datasource.config import AllowedScope
from app.errors import QueryInvalidRequest, QueryNotAllowed, SqlNotSupported, SqlSyntaxError
from app.query.models import OutputColumn, ValidatedQuery
from app.schema.models import SchemaSummary

# 允许的标量函数名（小写）。聚合函数单独在 AST 层校验参数。
# 公开给 Agent 提示复用，保证“告诉模型的函数清单”与真正策略白名单同源。
ALLOWED_FUNCTIONS = frozenset({
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "round",
    "coalesce",
    "date",
    "year",
    "month",
    "date_format",
})

# 这些 token 类型可能是函数名；DATE/YEAR 在 SQLGlot 中有专用 token 类型。
_FUNCTION_TOKEN_TYPES = {
    TokenType.VAR,
    TokenType.IDENTIFIER,
    TokenType.DATE,
    TokenType.YEAR,
}

# 词法层直接拒绝的 token：占位符、会话/用户变量、hint。
_FORBIDDEN_TOKEN_TYPES = {
    td
    for td in (
        TokenType.HINT,
        getattr(TokenType, "SESSION_PARAMETER", None),
        getattr(TokenType, "PLACEHOLDER", None),
        getattr(TokenType, "PARAMETER", None),
        getattr(TokenType, "VARIABLE", None),
    )
    if td is not None
}

# Select 节点允许出现的参数；None 值不算“出现”，因为 SQLGlot 会预填空槽位。
_ALLOWED_SELECT_ARGS = {
    "expressions",
    "from",
    "distinct",
    "limit",
    "where",
    "group",
    "having",
    "order",
}

# 正白名单节点集合：任何不在其中的节点都拒绝。
_ALLOWED_NODES = (
    exp.Select,
    exp.From,
    exp.Table,
    exp.TableAlias,
    exp.Identifier,
    exp.Column,
    exp.Alias,
    exp.Star,
    exp.Null,
    exp.Literal,
    exp.Distinct,
    exp.Where,
    exp.Group,
    exp.Having,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    # 布尔与比较
    exp.And,
    exp.Or,
    exp.Not,
    exp.Paren,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Between,
    exp.In,
    exp.Is,
    # 算术
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Mod,
    # 聚合与标量函数
    exp.Count,
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.Round,
    exp.Coalesce,
    exp.TsOrDsToDate,  # DATE(...)
    exp.Year,
    exp.Month,
    exp.TimeToStr,  # DATE_FORMAT(...)
)

_AGGREGATES = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
_NUMBER_RE = re.compile(r"^\d+$")
# 仅接受能稳定表达日期粒度的格式；单独的 %m/%d 会产生跨年歧义，明确关闭。
_ALLOWED_DATE_FORMATS = {"%Y", "%Y-%m", "%Y-%m-%d"}

# 资源边界：在配置上限之外再有固定天花板，避免单条 SQL 触发过深/过大 AST。
_MAX_AST_NODES = 300
_MAX_AST_DEPTH = 32
_MAX_PROJECTIONS = 16
_MAX_IN_VALUES = 100


def _strip_parens(node: exp.Expression) -> exp.Expression:
    """展开括号包裹，避免 (1) 绕过 GROUP BY/ORDER BY 的位置引用检查。

    MySQL 对括号中的整数仍按投影位置解释；只展开 exp.Paren，不改变其他表达式。
    """
    while isinstance(node, exp.Paren):
        inner = node.this
        if inner is None:
            break
        node = inner
    return node

# 输出类型标签对应的字段；仅用于前端/上层展示，不参与执行。
_TYPE_INT = "integer"
_TYPE_DECIMAL = "decimal"
_TYPE_FLOAT = "float"
_TYPE_STRING = "string"
_TYPE_DATE = "date"
_TYPE_DATETIME = "datetime"
_TYPE_TIME = "time"
_TYPE_BINARY = "binary"
_TYPE_NULL = "null"
_TYPE_UNKNOWN = "unknown"

_SCHEMA_TYPE_MAP = {
    "tinyint": _TYPE_INT,
    "smallint": _TYPE_INT,
    "mediumint": _TYPE_INT,
    "int": _TYPE_INT,
    "integer": _TYPE_INT,
    "bigint": _TYPE_INT,
    "decimal": _TYPE_DECIMAL,
    "numeric": _TYPE_DECIMAL,
    "float": _TYPE_FLOAT,
    "double": _TYPE_FLOAT,
    "real": _TYPE_FLOAT,
    "date": _TYPE_DATE,
    "datetime": _TYPE_DATETIME,
    "timestamp": _TYPE_DATETIME,
    "time": _TYPE_TIME,
    "char": _TYPE_STRING,
    "varchar": _TYPE_STRING,
    "tinytext": _TYPE_STRING,
    "text": _TYPE_STRING,
    "mediumtext": _TYPE_STRING,
    "longtext": _TYPE_STRING,
    "enum": _TYPE_STRING,
    "set": _TYPE_STRING,
    "json": _TYPE_STRING,
    "binary": _TYPE_BINARY,
    "varbinary": _TYPE_BINARY,
    "tinyblob": _TYPE_BINARY,
    "blob": _TYPE_BINARY,
    "mediumblob": _TYPE_BINARY,
    "longblob": _TYPE_BINARY,
}


@dataclass(frozen=True)
class _Context:
    """表达式校验上下文：限定符、可访问列与子句级限制。"""

    qualifier: str
    allowed_columns: frozenset[str]
    schema_types: dict[str, str]
    allow_aggregates: bool = True
    allow_output_aliases: bool = False
    output_aliases: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Analysis:
    """单次 AST 分析的稳定结果，用于生成前后一致性比对。"""

    ast: exp.Select
    source: str
    columns: tuple[OutputColumn, ...]
    user_limit: int | None


class QueryPolicy:
    """受限 SQL 策略；无状态，可被多个请求共享。"""

    def __init__(
        self,
        *,
        max_rows: int = 200,
        hard_max_rows: int = 1000,
        max_sql_bytes: int = 8192,
    ) -> None:
        if max_rows <= 0 or hard_max_rows < max_rows or max_sql_bytes <= 0:
            # 构造参数来自已校验的 QueryLimits；这里只做防御性检查，不泄露配置。
            raise ValueError("invalid query policy limits")
        self._max_rows = max_rows
        self._hard_max_rows = hard_max_rows
        self._max_sql_bytes = max_sql_bytes

    # ---- 对外入口 ----

    def validate(
        self,
        sql: str,
        *,
        scope: AllowedScope,
        schema: SchemaSummary,
        database: str,
    ) -> ValidatedQuery:
        """校验用户 SQL 并返回“生成 SQL + 输出描述”。

        只解析一次用户输入，生成后再解析一次做结构一致性确认；任何不一致都
        拒绝执行，保证执行层拿到的 SQL 不包含未校验的结构。
        """
        if not isinstance(sql, str):
            raise QueryInvalidRequest()

        text = sql.strip()
        if not text:
            raise SqlSyntaxError()
        if len(text.encode("utf-8")) > self._max_sql_bytes:
            raise SqlNotSupported()

        self._check_tokens(text)
        ast = self._parse(text)

        # 输入模式不允许库名前缀；生成模式由 _revalidate_generated 单独放行。
        analysis = self._analyze(ast, scope, schema, database, allow_db=False)

        effective, fetch, clamped = self._resolve_limit(analysis.user_limit)

        generated_ast = analysis.ast.copy()
        table = next(generated_ast.find_all(exp.Table), None)
        if table is None:
            raise SqlNotSupported()
        table.set("db", exp.Identifier(this=database, quoted=False))
        generated_ast.set("limit", exp.Limit(expression=exp.Literal.number(fetch)))
        generated_sql = generated_ast.sql(dialect="mysql")

        self._revalidate_generated(generated_sql, analysis, scope, schema, database, fetch)

        return ValidatedQuery(
            sql=generated_sql,
            database=database,
            source=analysis.source,
            columns=analysis.columns,
            effective_limit=effective,
            fetch_limit=fetch,
            clamped=clamped,
            scope_fingerprint=scope.fingerprint(),
        )

    # ---- 词法层 ----

    @staticmethod
    def _check_tokens(sql: str) -> None:
        """拒绝注释/hint/变量/占位符，并校验每个函数名的 token 白名单。"""
        try:
            tokens = tokenize(sql, read="mysql")
        except TokenError:
            raise SqlSyntaxError() from None

        for index, token in enumerate(tokens):
            if token.comments:
                raise SqlNotSupported()
            if token.token_type in _FORBIDDEN_TOKEN_TYPES:
                raise SqlNotSupported()
            if token.text.upper() == "NULLS":
                # SQLGlot 的 MySQL 生成器会静默丢掉 NULLS LAST，必须词法层拒绝。
                raise SqlNotSupported()
            if token.token_type == TokenType.SEMICOLON and index != len(tokens) - 1:
                # 只允许末尾单个分号；杜绝多语句与空语句拼接触发解析歧义。
                raise SqlNotSupported()

            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following is not None and following.token_type == TokenType.L_PAREN:
                if token.token_type in _FUNCTION_TOKEN_TYPES:
                    name = token.text.strip("`\"").lower()
                    if name not in ALLOWED_FUNCTIONS:
                        raise SqlNotSupported()

    # ---- 解析 ----

    @staticmethod
    def _parse(sql: str) -> exp.Select:
        try:
            statements = parse(sql, read="mysql")
        except (ParseError, TokenError):
            raise SqlSyntaxError() from None
        except RecursionError:
            # 极深括号会在解析器内部触发 Python 递归上限；按“超出支持范围”拒绝。
            raise SqlNotSupported() from None
        if len(statements) != 1 or statements[0] is None:
            raise SqlSyntaxError()
        ast = statements[0]
        if not isinstance(ast, exp.Select):
            raise SqlNotSupported()
        return ast

    # ---- AST 分析 ----

    def _analyze(
        self,
        ast: exp.Select,
        scope: AllowedScope,
        schema: SchemaSummary,
        database: str,
        *,
        allow_db: bool,
    ) -> _Analysis:
        self._check_metrics(ast)
        self._check_select_args(ast)

        source, qualifier, schema_types = self._resolve_table(
            ast, scope, schema, database, allow_db
        )
        allowed_columns = frozenset(scope.lower_columns_for(source))
        base_ctx = _Context(
            qualifier=qualifier,
            allowed_columns=allowed_columns,
            schema_types=schema_types,
        )

        projections = list(ast.expressions)
        if not projections or len(projections) > _MAX_PROJECTIONS:
            raise SqlNotSupported()

        columns: list[OutputColumn] = []
        seen_names: set[str] = set()
        aliases: set[str] = set()

        for projection in projections:
            inner, name = self._projection(projection)
            self._validate_expr(inner, parent=ast, ctx=base_ctx)

            folded = name.casefold()
            if folded in seen_names:
                raise SqlNotSupported()
            seen_names.add(folded)
            if isinstance(projection, exp.Alias):
                aliases.add(folded)

            columns.append(OutputColumn(name=name, data_type=self._infer_type(inner, schema_types)))

        # DISTINCT 只能作用于投影，不能携带 ON/表达式列表。
        distinct = ast.args.get("distinct")
        if distinct is not None:
            if not isinstance(distinct, exp.Distinct) or any(
                value is not None for value in distinct.args.values()
            ):
                raise SqlNotSupported()

        self._validate_where(ast, base_ctx)
        self._validate_group(ast, base_ctx)
        self._validate_having(ast, base_ctx)
        self._validate_order(ast, base_ctx, frozenset(aliases))
        user_limit = self._validate_limit(ast)

        return _Analysis(
            ast=ast,
            source=source,
            columns=tuple(columns),
            user_limit=user_limit,
        )

    def _revalidate_generated(
        self,
        generated_sql: str,
        original: _Analysis,
        scope: AllowedScope,
        schema: SchemaSummary,
        database: str,
        fetch_limit: int,
    ) -> None:
        """重新解析生成 SQL，确认其结构与原分析完全一致后才允许执行。"""
        generated = self._parse(generated_sql)
        again = self._analyze(generated, scope, schema, database, allow_db=True)
        if (
            again.source != original.source
            or again.columns != original.columns
            or again.user_limit != fetch_limit
        ):
            raise SqlNotSupported()

    @staticmethod
    def _check_metrics(ast: exp.Select) -> None:
        """限制 AST 规模，避免深层嵌套或超大投影长期占用解析/遍历资源。"""
        count = 0
        max_depth = 0
        stack: list[tuple[exp.Expression, int]] = [(ast, 1)]
        while stack:
            node, depth = stack.pop()
            count += 1
            max_depth = max(max_depth, depth)
            if count > _MAX_AST_NODES or max_depth > _MAX_AST_DEPTH:
                raise SqlNotSupported()
            for child in node.iter_expressions():
                stack.append((child, depth + 1))

    @staticmethod
    def _check_select_args(ast: exp.Select) -> None:
        """Select 只允许白名单参数；None 槽位视为未出现。"""
        for key, value in ast.args.items():
            if value is not None and key not in _ALLOWED_SELECT_ARGS:
                raise SqlNotSupported()
        if ast.args.get("from") is None:
            raise SqlNotSupported()

    def _resolve_table(
        self,
        ast: exp.Select,
        scope: AllowedScope,
        schema: SchemaSummary,
        database: str,
        allow_db: bool,
    ) -> tuple[str, str, dict[str, str]]:
        """解析唯一来源表，返回 (对象名, 有效限定符, 列类型表)。"""
        from_node = ast.args.get("from")
        if not isinstance(from_node, exp.From):
            raise SqlNotSupported()
        for key, value in from_node.args.items():
            if value is not None and key != "this":
                raise SqlNotSupported()

        table = from_node.this
        if not isinstance(table, exp.Table):
            raise SqlNotSupported()

        for key, value in table.args.items():
            if value is None or key in {"this", "alias"}:
                continue
            if key == "db" and allow_db:
                continue
            if key == "catalog":
                raise SqlNotSupported()
            raise SqlNotSupported()

        db_node = table.args.get("db")
        if db_node is not None:
            if not allow_db or not isinstance(db_node, exp.Identifier) or db_node.name != database:
                raise SqlNotSupported()
        if table.args.get("catalog") is not None:
            raise SqlNotSupported()

        identifier = table.this
        if not isinstance(identifier, exp.Identifier) or not identifier.name:
            raise QueryNotAllowed()
        source = identifier.name

        alias = table.args.get("alias")
        qualifier = source
        if alias is not None:
            if not isinstance(alias, exp.TableAlias):
                raise SqlNotSupported()
            for key, value in alias.args.items():
                if value is not None and key != "this":
                    raise SqlNotSupported()
            alias_identifier = alias.this
            if not isinstance(alias_identifier, exp.Identifier) or not alias_identifier.name:
                raise SqlNotSupported()
            qualifier = alias_identifier.name

        if source not in scope.objects:
            raise QueryNotAllowed()

        schema_object = next((obj for obj in schema.objects if obj.name == source), None)
        if schema_object is None:
            raise QueryNotAllowed()
        schema_types = {column.name.lower(): column.data_type for column in schema_object.columns}
        return source, qualifier, schema_types

    @staticmethod
    def _projection(projection: exp.Expression) -> tuple[exp.Expression, str]:
        """返回 (待校验表达式, 输出列名)；投影别名必须是单个标识符。"""
        if isinstance(projection, exp.Alias):
            alias = projection.args.get("alias")
            if not isinstance(alias, exp.Identifier) or not alias.name:
                raise SqlNotSupported()
            return projection.this, alias.name

        name = projection.output_name
        if not name:
            # COUNT(*) 等表达式没有 output_name；用生成 SQL 作为稳定列标签。
            name = projection.sql(dialect="mysql")
        if not name:
            raise SqlNotSupported()
        return projection, name

    def _validate_expr(
        self,
        node: exp.Expression,
        *,
        parent: exp.Expression | None,
        ctx: _Context,
    ) -> None:
        """递归校验表达式：正白名单 + 列授权 + 子句级聚合限制。"""
        if not isinstance(node, _ALLOWED_NODES):
            raise SqlNotSupported()

        if isinstance(node, _AGGREGATES):
            if not ctx.allow_aggregates:
                raise SqlNotSupported()
            self._check_aggregate(node)
        elif isinstance(node, exp.Round):
            self._check_round(node)
        elif isinstance(node, exp.Coalesce):
            self._check_coalesce(node)
        elif isinstance(node, exp.TsOrDsToDate):
            self._check_single_column(node)
        elif isinstance(node, exp.Year) or isinstance(node, exp.Month):
            self._check_single_column(node)
        elif isinstance(node, exp.TimeToStr):
            self._check_date_format(node)
        elif isinstance(node, exp.Column):
            self._check_column(node, ctx)
        elif isinstance(node, exp.Star):
            if not isinstance(parent, exp.Count):
                raise SqlNotSupported()
        elif isinstance(node, exp.Distinct):
            if not isinstance(parent, exp.Count):
                raise SqlNotSupported()
            expressions = node.args.get("expressions") or []
            if len(expressions) != 1 or not isinstance(expressions[0], exp.Column):
                raise SqlNotSupported()
        elif isinstance(node, exp.In):
            self._check_in(node)
        elif isinstance(node, exp.Literal):
            self._check_literal(node)

        for child in node.iter_expressions():
            self._validate_expr(child, parent=node, ctx=ctx)

    @staticmethod
    def _check_aggregate(node: exp.Expression) -> None:
        """聚合函数只接受普通列；COUNT 额外支持 * 与 DISTINCT 列。"""
        if isinstance(node, exp.Count):
            this = node.args.get("this")
            extras = node.args.get("expressions") or []
            if extras:
                raise SqlNotSupported()
            if isinstance(this, (exp.Star, exp.Column)):
                return
            if isinstance(this, exp.Distinct):
                expressions = this.args.get("expressions") or []
                if len(expressions) == 1 and isinstance(expressions[0], exp.Column):
                    return
            raise SqlNotSupported()

        this = node.args.get("this")
        extras = node.args.get("expressions") or []
        if extras or not isinstance(this, exp.Column):
            raise SqlNotSupported()

    @staticmethod
    def _check_round(node: exp.Round) -> None:
        if not isinstance(node.args.get("this"), exp.Column):
            raise SqlNotSupported()
        decimals = node.args.get("decimals")
        if decimals is None:
            return
        if not isinstance(decimals, exp.Literal) or decimals.is_string:
            raise SqlNotSupported()
        text = decimals.this
        if not _NUMBER_RE.match(text) or not 0 <= int(text) <= 6:
            raise SqlNotSupported()

    @staticmethod
    def _check_coalesce(node: exp.Coalesce) -> None:
        args = [node.args.get("this"), *(node.args.get("expressions") or [])]
        if not 2 <= len(args) <= 5:
            raise SqlNotSupported()
        for arg in args:
            if not isinstance(arg, (exp.Column, exp.Literal, exp.Null)):
                raise SqlNotSupported()

    @staticmethod
    def _check_single_column(node: exp.Expression) -> None:
        this = node.args.get("this")
        if isinstance(this, exp.TsOrDsToDate):
            this = this.args.get("this")
        if not isinstance(this, exp.Column):
            raise SqlNotSupported()

    @staticmethod
    def _check_date_format(node: exp.TimeToStr) -> None:
        if not isinstance(node.args.get("this"), exp.Column):
            raise SqlNotSupported()
        fmt = node.args.get("format")
        if (
            not isinstance(fmt, exp.Literal)
            or not fmt.is_string
            or fmt.this not in _ALLOWED_DATE_FORMATS
        ):
            raise SqlNotSupported()

    @staticmethod
    def _check_in(node: exp.In) -> None:
        if node.args.get("query") is not None or node.args.get("unnest") is not None:
            raise SqlNotSupported()
        expressions = node.args.get("expressions") or []
        if not 1 <= len(expressions) <= _MAX_IN_VALUES:
            raise SqlNotSupported()

    @staticmethod
    def _check_literal(node: exp.Literal) -> None:
        if node.is_string:
            return
        text = node.this
        if _NUMBER_RE.match(text):
            return
        try:
            number = float(text)
        except (TypeError, ValueError):
            raise SqlNotSupported() from None
        if not math.isfinite(number):
            raise SqlNotSupported()

    @staticmethod
    def _check_column(node: exp.Column, ctx: _Context) -> None:
        """列授权：限定符必须是唯一表名/别名，列必须同时出现在授权与 Schema。"""
        if node.args.get("db") is not None or node.args.get("catalog") is not None:
            raise QueryNotAllowed()

        identifier = node.args.get("this")
        if not isinstance(identifier, exp.Identifier) or not identifier.name:
            raise QueryNotAllowed()

        qualifier = node.table
        if qualifier and qualifier.casefold() != ctx.qualifier.casefold():
            raise QueryNotAllowed()

        name = identifier.name.casefold()
        # ORDER BY 允许引用投影别名；其他位置（WHERE/GROUP/HAVING）不允许。
        if not qualifier and ctx.allow_output_aliases and name in ctx.output_aliases:
            return

        if name not in ctx.allowed_columns or name not in ctx.schema_types:
            raise QueryNotAllowed()

    # ---- 子句 ----

    def _validate_where(self, ast: exp.Select, ctx: _Context) -> None:
        where = ast.args.get("where")
        if where is None:
            return
        if not isinstance(where, exp.Where) or where.this is None:
            raise SqlNotSupported()
        self._validate_expr(
            where.this,
            parent=where,
            ctx=_Context(
                qualifier=ctx.qualifier,
                allowed_columns=ctx.allowed_columns,
                schema_types=ctx.schema_types,
                allow_aggregates=False,
            ),
        )

    def _validate_group(self, ast: exp.Select, ctx: _Context) -> None:
        group = ast.args.get("group")
        if group is None:
            return
        if not isinstance(group, exp.Group):
            raise SqlNotSupported()
        for key, value in group.args.items():
            if value is not None and key != "expressions":
                raise SqlNotSupported()
        expressions = group.args.get("expressions") or []
        if not expressions:
            raise SqlNotSupported()
        group_ctx = _Context(
            qualifier=ctx.qualifier,
            allowed_columns=ctx.allowed_columns,
            schema_types=ctx.schema_types,
            allow_aggregates=False,
        )
        for expression in expressions:
            # GROUP BY 1 / (1) 是投影位置引用，语义依赖投影顺序；字符串等裸字面量
            # 作为分组键也无明确意义。与 ORDER BY 一致，直接拒绝。
            if isinstance(_strip_parens(expression), exp.Literal):
                raise SqlNotSupported()
            self._validate_expr(expression, parent=group, ctx=group_ctx)

    def _validate_having(self, ast: exp.Select, ctx: _Context) -> None:
        having = ast.args.get("having")
        if having is None:
            return
        if not isinstance(having, exp.Having) or having.this is None:
            raise SqlNotSupported()
        self._validate_expr(having.this, parent=having, ctx=ctx)

    def _validate_order(self, ast: exp.Select, ctx: _Context, aliases: frozenset[str]) -> None:
        order = ast.args.get("order")
        if order is None:
            return
        if not isinstance(order, exp.Order):
            raise SqlNotSupported()
        for key, value in order.args.items():
            if value is not None and key != "expressions":
                raise SqlNotSupported()
        expressions = order.args.get("expressions") or []
        if not expressions:
            raise SqlNotSupported()

        order_ctx = _Context(
            qualifier=ctx.qualifier,
            allowed_columns=ctx.allowed_columns,
            schema_types=ctx.schema_types,
            allow_aggregates=True,
            allow_output_aliases=True,
            output_aliases=aliases,
        )
        for ordered in expressions:
            if not isinstance(ordered, exp.Ordered) or ordered.this is None:
                raise SqlNotSupported()
            for key, value in ordered.args.items():
                if value is not None and key not in {"this", "desc", "nulls_first"}:
                    raise SqlNotSupported()
            if isinstance(_strip_parens(ordered.this), exp.Literal):
                # ORDER BY 1 / (1) 表示列位置，语义依赖投影顺序，明确拒绝。
                raise SqlNotSupported()
            self._validate_expr(ordered.this, parent=ordered, ctx=order_ctx)

    @staticmethod
    def _validate_limit(ast: exp.Select) -> int | None:
        limit = ast.args.get("limit")
        if limit is None:
            return None
        if not isinstance(limit, exp.Limit):
            raise SqlNotSupported()
        for key, value in limit.args.items():
            if value is not None and key != "expression":
                raise SqlNotSupported()
        expression = limit.args.get("expression")
        if not isinstance(expression, exp.Literal) or expression.is_string:
            raise SqlNotSupported()
        if not _NUMBER_RE.match(expression.this):
            raise SqlNotSupported()
        return int(expression.this)

    # ---- LIMIT 与类型 ----

    def _resolve_limit(self, user_limit: int | None) -> tuple[int, int, bool]:
        """返回 (effective, fetch, clamped)。

        - 未写 LIMIT：取默认行数并多取 1 行判断是否被截断；
        - 用户值 <= 默认上限：按用户值精确读取，不额外多取；
        - 用户值 > 默认上限：压到默认上限并多取 1 行标记 truncated。
        """
        if user_limit is None:
            return self._max_rows, self._max_rows + 1, True
        if user_limit <= self._max_rows:
            return user_limit, user_limit, False
        return self._max_rows, self._max_rows + 1, True

    def _infer_type(self, node: exp.Expression, schema_types: dict[str, str]) -> str:
        """从受限表达式推导展示类型；未知类型保持 unknown，不阻断无损序列化。"""
        if isinstance(node, exp.Alias):
            return self._infer_type(node.this, schema_types)
        if isinstance(node, (exp.Paren, exp.Distinct)):
            expressions = node.args.get("expressions") or []
            inner = node.args.get("this") or (expressions[0] if expressions else None)
            return self._infer_type(inner, schema_types) if inner is not None else _TYPE_UNKNOWN
        if isinstance(node, exp.Column):
            return _SCHEMA_TYPE_MAP.get(
                schema_types.get(node.name.lower(), "").lower(), _TYPE_UNKNOWN
            )
        if isinstance(node, exp.Literal):
            if node.is_string:
                return _TYPE_STRING
            return _TYPE_DECIMAL if any(ch in node.this.lower() for ch in ".e") else _TYPE_INT
        if isinstance(node, exp.Null):
            return _TYPE_NULL
        if isinstance(node, exp.Count):
            return _TYPE_INT
        if isinstance(node, (exp.Sum, exp.Avg)):
            return _TYPE_DECIMAL
        if isinstance(node, (exp.Min, exp.Max)):
            this = node.args.get("this")
            return self._infer_type(this, schema_types) if this is not None else _TYPE_UNKNOWN
        if isinstance(node, exp.Round):
            this = node.args.get("this")
            inferred = self._infer_type(this, schema_types) if this is not None else _TYPE_UNKNOWN
            return inferred if inferred in {_TYPE_INT, _TYPE_DECIMAL, _TYPE_FLOAT} else _TYPE_DECIMAL
        if isinstance(node, exp.Coalesce):
            this = node.args.get("this")
            return self._infer_type(this, schema_types) if this is not None else _TYPE_UNKNOWN
        if isinstance(node, exp.TsOrDsToDate):
            return _TYPE_DATE
        if isinstance(node, (exp.Year, exp.Month)):
            return _TYPE_INT
        if isinstance(node, exp.TimeToStr):
            return _TYPE_STRING
        if isinstance(node, exp.Div):
            return _TYPE_DECIMAL
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Mod)):
            left = self._infer_type(node.this, schema_types) if node.this is not None else _TYPE_UNKNOWN
            right = (
                self._infer_type(node.expression, schema_types)
                if node.expression is not None
                else _TYPE_UNKNOWN
            )
            types = {left, right}
            if _TYPE_FLOAT in types:
                return _TYPE_FLOAT
            if _TYPE_DECIMAL in types:
                return _TYPE_DECIMAL
            if types == {_TYPE_INT}:
                return _TYPE_INT
            return _TYPE_UNKNOWN
        return _TYPE_UNKNOWN
