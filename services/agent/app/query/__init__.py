"""Task C：受限 SQL 查询执行模块。

模块边界：
- QueryPolicy 只做解析、白名单校验与生成安全 SQL，不连接数据库；
- QueryPool 只负责有界连接、受控会话与资源回收；
- QueryExecutor 只执行已校验的生成 SQL 并做有界读取/序列化；
- QueryService 是唯一业务入口，串起数据源鉴权、Schema、策略与执行。
"""
