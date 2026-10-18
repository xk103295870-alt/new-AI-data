"""驱动错误分类：只依据异常类型及有效因果链，避免匹配敏感错误文本。"""
from __future__ import annotations

import ssl


def is_tls_failure(exc: BaseException) -> bool:
    """识别被驱动包装的 TLS 失败，遵循 cause 优先及 from None 的抑制语义。

    aiomysql 会把证书异常包装成通用 2003，因此不能只检查顶层类型；同样不能
    把全部 2003 当 TLS。只沿有效因果链检查类型，身份集合用于终止循环引用。
    """
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return False
