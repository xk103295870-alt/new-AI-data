"""集中配置：所有运行参数只在此定义，测试可传入独立实例。

安全边界：不从文件自动读取 .env，仅使用进程环境变量；
避免后端意外加载仓库根目录的占位符或真实凭据。
数据库密码使用 SecretStr，禁止打印整个 Settings 对象。
"""
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用运行配置。

    字段均有安全默认值；同名环境变量（如 APP_HOST、CORS_ORIGINS）可覆盖。
    cors_origins 使用逗号分隔字符串，便于通过环境变量传递来源列表。
    数据源（Task B 起）默认禁用；未配置数据库时应用与 /health 仍正常运行。
    """

    model_config = SettingsConfigDict(
        env_file=None,   # 不自动读取 .env，配置来源明确
        extra="ignore",  # 忽略未声明变量（例如未来 LLM 占位符）
    )

    app_env: str = "development"  # 环境标识，当前不影响行为
    app_name: str = "ai-data-agent"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    # ---- 数据源（Task B）----
    datasource_enabled: bool = False
    datasource_id: str = "demo_mysql"
    datasource_name: str = "授权测试数据源"

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_password: SecretStr = SecretStr("")
    db_ssl_enabled: bool = False
    db_ssl_ca_path: str = ""
    db_allowed_objects_json: str = "{}"

    schema_cache_ttl_seconds: int = 60
    db_connect_timeout_seconds: float = 5.0
    db_metadata_timeout_seconds: float = 5.0
    db_total_timeout_seconds: float = 15.0
    db_metadata_max_objects: int = 200
    db_metadata_max_columns: int = 2000

    # ---- 受限查询执行（Task C）----
    # 总预算覆盖 Schema 获取 + 排队 + 连接 + 执行 + 读取；服务端执行另有更小上限。
    query_total_timeout_seconds: float = 10.0
    query_server_timeout_seconds: float = 8.0
    query_pool_max_connections: int = 2
    query_pool_max_waiters: int = 4
    query_pool_acquire_timeout_seconds: float = 2.0
    query_max_rows: int = 200
    query_hard_max_rows: int = 1000
    query_max_cell_bytes: int = 65536
    query_max_result_bytes: int = 1048576
    query_max_sql_bytes: int = 8192

    # ---- Agent / LLM（Task D）----
    # LLM 三项全部提供才启用聊天；密钥只来自进程环境变量，禁止写入日志/响应。
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    # gemini-4-argon 单轮完整分析实测 34–47s，偶发单次 19s+，默认留出足够余量。
    llm_timeout_seconds: float = 90.0
    llm_max_tokens: int = 1024

    # 整体分析预算必须覆盖单次模型调用；实测简单问题需 4 次工具调用，默认放宽到 8 次。
    agent_request_timeout_seconds: float = 150.0
    max_tool_calls: int = 8
    sql_auto_retry_max: int = 1
    agent_tool_result_max_rows: int = 50
    agent_context_messages: int = 20
    # 相对日期按该 IANA 时区的自然日换算；非法时区会禁用 Agent，不静默退回 UTC。
    app_timezone: str = "Asia/Shanghai"
    session_db_path: str = "./data/agent_sessions.sqlite3"
    business_terms_path: str = "config/business_terms.yaml"

    @property
    def cors_origin_list(self) -> list[str]:
        """将逗号分隔的 CORS 来源解析为列表，忽略空项。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
